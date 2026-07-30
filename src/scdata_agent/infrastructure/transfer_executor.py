"""Deterministic, resumable transfer worker executed only after approval."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
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


class TransferExecutor:
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
    ) -> list[TransferReceipt]:
        selected = [
            bundle for bundle in bundles if bundle.bundle_id in selected_bundle_ids
        ]
        selected_asset_ids = {
            asset_id for bundle in selected for asset_id in bundle.asset_ids
        }
        by_asset = {
            asset.asset_id: asset
            for asset in assets
            if asset.asset_id in selected_asset_ids
        }
        expected_sizes = [
            location.size_bytes
            for asset in by_asset.values()
            for location in asset.locations[:1]
        ]
        if any(size is None for size in expected_sizes) and not allow_unknown_size:
            raise ValueError(
                "Selected assets include unknown sizes; explicit override required."
            )
        known_total = sum(size or 0 for size in expected_sizes)
        if max_download_bytes is not None and known_total > max_download_bytes:
            raise ValueError("Selected assets exceed max_download_bytes.")
        destination = Path(plan.destination_root).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(destination).free
        if known_total > free_bytes:
            raise ValueError("Insufficient free disk space for selected assets.")

        owns_client = self._client is None
        client = self._client or default_async_client()
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
        safe_name = Path(asset.file_name).name
        if safe_name != asset.file_name or safe_name in {"", ".", ".."}:
            return self._failed(
                receipt_id,
                plan,
                bundle_id,
                asset,
                location.url,
                "Unsafe asset file name.",
            )
        bundle_dir = destination / bundle_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        final_path = bundle_dir / safe_name
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
