"""Resolve public ENA read files from canonical repository accessions."""

from __future__ import annotations

from itertools import zip_longest
from pathlib import PurePosixPath

import httpx

from celllore.domain.identifiers import stable_id
from celllore.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    AssetResolutionResult,
    DataAsset,
    DataRole,
    DatasetCandidate,
    TransferProtocol,
)
from celllore.infrastructure.sources.base import default_async_client, request_with_retry

SUPPORTED_NAMESPACES = {"sra_project", "sra_run"}


class ENAAssetResolver:
    name = "ena_file_report"
    base_url = "https://www.ebi.ac.uk/ena/portal/api/filereport"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        expand_bioproject: bool = False,
        max_assets: int = 500,
    ) -> None:
        self._client = client
        self.expand_bioproject = expand_bioproject
        self.max_assets = max_assets

    async def resolve(self, candidate: DatasetCandidate) -> AssetResolutionResult:
        supported = set(SUPPORTED_NAMESPACES)
        if self.expand_bioproject:
            supported.add("bioproject")
        accessions = [
            identifier.value
            for identifier in candidate.identifiers
            if identifier.namespace.lower() in supported
        ]
        owns_client = self._client is None
        client = self._client or default_async_client()
        assets: dict[str, DataAsset] = {}
        errors: list[str] = []
        queried: list[str] = []
        try:
            for accession in accessions:
                queried.append(accession)
                try:
                    response = await request_with_retry(
                        client,
                        "GET",
                        self.base_url,
                        params={
                            "accession": accession,
                            "result": "read_run",
                            "fields": (
                                "run_accession,fastq_ftp,fastq_bytes,fastq_md5"
                            ),
                            "format": "json",
                            "download": "false",
                        },
                    )
                    for asset in self._parse_rows(candidate, response.json()):
                        assets[asset.asset_id] = asset
                        if len(assets) >= self.max_assets:
                            break
                    if assets:
                        break
                except Exception as error:
                    errors.append(
                        f"{accession}: {type(error).__name__}: {error}"
                    )
        finally:
            if owns_client:
                await client.aclose()
        return AssetResolutionResult(
            candidate_id=candidate.candidate_id,
            queried_accessions=queried,
            assets=list(assets.values()),
            errors=errors,
        )

    def _parse_rows(
        self, candidate: DatasetCandidate, rows: list[dict]
    ) -> list[DataAsset]:
        assets: list[DataAsset] = []
        evidence_ids = [evidence.evidence_id for evidence in candidate.evidence]
        for row in rows:
            run = row.get("run_accession") or "unknown_run"
            paths = _split(row.get("fastq_ftp"))
            sizes = _split(row.get("fastq_bytes"))
            checksums = _split(row.get("fastq_md5"))
            for path, size, checksum in zip_longest(
                paths, sizes, checksums, fillvalue=None
            ):
                if not path:
                    continue
                url = path if "://" in path else f"https://{path}"
                file_name = PurePosixPath(path).name
                asset_id = stable_id(
                    "asset",
                    f"{candidate.candidate_id}:{run}:{file_name}",
                )
                assets.append(
                    DataAsset(
                        asset_id=asset_id,
                        candidate_id=candidate.candidate_id,
                        role=DataRole.RAW_READS,
                        file_name=file_name,
                        compression="gzip" if file_name.endswith(".gz") else None,
                        locations=[
                            AssetLocation(
                                url=url,
                                protocol=TransferProtocol.HTTPS,
                                access_level=AccessLevel.PUBLIC_DECLARED,
                                size_bytes=_int_or_none(size),
                                checksum_algorithm="md5" if checksum else None,
                                checksum=checksum,
                                availability=AssetAvailability.DECLARED,
                                evidence_ids=evidence_ids,
                            )
                        ],
                        evidence_ids=evidence_ids,
                    )
                )
                if len(assets) >= self.max_assets:
                    return assets
        return assets


def _split(value: object) -> list[str]:
    if value is None:
        return []
    return [item for item in str(value).split(";") if item]


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
