"""Deterministic, resumable transfer worker executed only after approval."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    AcquisitionPlan,
    SemanticBundle,
    TransferReceipt,
    TransferReceiptStatus,
)
from scdata_agent.domain.models import DataAsset, TransferProtocol
from scdata_agent.infrastructure.sources.base import default_async_client
from scdata_agent.infrastructure.transfer_backend import prepare_transfer, safe_destination_path


class TransferExecutor:
    name = "httpx"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        concurrency: int = 3,
    ) -> None:
        self._client = client
        self.concurrency = max(1, concurrency)

    async def execute(
        self,
        plan: AcquisitionPlan,
        bundles: list[SemanticBundle],
        assets: list[DataAsset],
        selected_bundle_ids: set[str],
        *,
        allow_unknown_size: bool = False,
        max_download_bytes: int | None = None,
        layout: dict[str, str] | None = None,
    ) -> list[TransferReceipt]:
        selected, by_asset, destination = prepare_transfer(
            plan, bundles, assets, selected_bundle_ids,
            allow_unknown_size=allow_unknown_size,
            max_download_bytes=max_download_bytes,
        )
        layout = layout or {}

        owns_client = self._client is None
        # Downloads must not inherit a developer/workstation proxy. Source
        # discovery may still use the normal environment-aware client, while
        # the byte-transfer path connects directly and leaves the parent
        # process environment untouched.
        client = self._client or default_async_client(trust_env=False)
        semaphore = asyncio.Semaphore(self.concurrency)
        bundle_by_asset = {
            asset_id: bundle.bundle_id
            for bundle in selected
            for asset_id in bundle.asset_ids
        }

        async def transfer(asset: DataAsset) -> TransferReceipt:
            async with semaphore:
                return await self._transfer_one(
                    client,
                    plan,
                    bundle_by_asset[asset.asset_id],
                    asset,
                    destination,
                    layout.get(asset.asset_id),
                )

        try:
            return await asyncio.gather(
                *(transfer(asset) for asset in by_asset.values())
            )
        finally:
            if owns_client:
                await client.aclose()

    async def _transfer_one(
        self,
        client: httpx.AsyncClient,
        plan: AcquisitionPlan,
        bundle_id: str,
        asset: DataAsset,
        destination: Path,
        relative_path: str | None = None,
    ) -> TransferReceipt:
        receipt_id = stable_id(
            "receipt", f"{plan.plan_id}:{bundle_id}:{asset.asset_id}"
        )
        if not asset.locations:
            return self._failed(
                receipt_id, plan, bundle_id, asset, "", "No asset location."
            )
        location = asset.locations[0]
        if location.protocol not in {
            TransferProtocol.HTTPS,
            TransferProtocol.S3,
        }:
            return self._failed(
                receipt_id,
                plan,
                bundle_id,
                asset,
                location.url,
                f"Unsupported protocol: {location.protocol.value}",
            )
        try:
            final_path = safe_destination_path(
                destination,
                bundle_id=bundle_id,
                file_name=asset.file_name,
                relative_path=relative_path,
            )
        except ValueError as error:
            return self._failed(receipt_id, plan, bundle_id, asset, location.url, str(error))
        final_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = final_path.with_suffix(final_path.suffix + ".part")
        resumed = partial_path.exists() and partial_path.stat().st_size > 0
        offset = partial_path.stat().st_size if resumed else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            async with client.stream(
                "GET", location.url, headers=headers, follow_redirects=True
            ) as response:
                response.raise_for_status()
                append = offset > 0 and response.status_code == 206
                mode = "ab" if append else "wb"
                if offset and not append:
                    resumed = False
                    offset = 0
                with partial_path.open(mode) as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
            partial_path.replace(final_path)
            algorithm = location.checksum_algorithm
            observed = (
                _file_digest(final_path, algorithm) if algorithm else None
            )
            valid = (
                observed.casefold() == location.checksum.casefold()
                if observed and location.checksum
                else None
            )
            status = (
                TransferReceiptStatus.COMPLETED
                if valid is not False
                else TransferReceiptStatus.FAILED
            )
            return TransferReceipt(
                receipt_id=receipt_id,
                plan_id=plan.plan_id,
                bundle_id=bundle_id,
                asset_id=asset.asset_id,
                source_url=location.url,
                local_path=str(final_path),
                protocol=location.protocol,
                status=status,
                bytes_written=final_path.stat().st_size,
                checksum_algorithm=algorithm,
                expected_checksum=location.checksum,
                observed_checksum=observed,
                checksum_valid=valid,
                resumed=resumed,
                error="Checksum mismatch." if valid is False else None,
            )
        except Exception as error:
            return self._failed(
                receipt_id,
                plan,
                bundle_id,
                asset,
                location.url,
                f"{type(error).__name__}: {error}",
                local_path=str(partial_path),
                resumed=resumed,
            )

    @staticmethod
    def _failed(
        receipt_id: str,
        plan: AcquisitionPlan,
        bundle_id: str,
        asset: DataAsset,
        source_url: str,
        error: str,
        *,
        local_path: str = "",
        resumed: bool = False,
    ) -> TransferReceipt:
        protocol = (
            asset.locations[0].protocol
            if asset.locations
            else TransferProtocol.MANUAL
        )
        return TransferReceipt(
            receipt_id=receipt_id,
            plan_id=plan.plan_id,
            bundle_id=bundle_id,
            asset_id=asset.asset_id,
            source_url=source_url,
            local_path=local_path,
            protocol=protocol,
            status=TransferReceiptStatus.FAILED,
            error=error,
            resumed=resumed,
        )


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm.casefold())
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
