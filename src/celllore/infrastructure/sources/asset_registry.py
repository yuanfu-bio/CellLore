"""Capability registry for repository-specific remote asset manifests."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from celllore.domain.assets import infer_data_role
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
from celllore.infrastructure.sources.base import (
    default_async_client,
    request_with_retry,
)
from celllore.infrastructure.sources.ena import ENAAssetResolver


class AssetResolverRegistry:
    """Route typed identifiers to bounded, deterministic adapters."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        expand_bioproject: bool = False,
        max_assets: int = 500,
    ) -> None:
        self.max_assets = max_assets
        # Processed-data resolvers run before raw reads. A study's matrices are
        # what decide which bundle schema it can satisfy and are orders of
        # magnitude smaller than its FASTQ set, so when a budget binds they are
        # the ones worth spending it on.
        self.resolvers = [
            GEOSupplementaryAssetResolver(client, max_assets=max_assets),
            ZenodoAssetResolver(client, max_assets=max_assets),
            FigshareAssetResolver(client, max_assets=max_assets),
            ENAAssetResolver(
                client,
                expand_bioproject=expand_bioproject,
                max_assets=max_assets,
            ),
        ]

    async def resolve(self, candidate: DatasetCandidate) -> AssetResolutionResult:
        assets: dict[str, DataAsset] = {}
        queried: list[str] = []
        errors: list[str] = []
        truncated: list[str] = []
        for resolver in self.resolvers:
            result = await resolver.resolve(candidate)
            queried.extend(result.queried_accessions)
            errors.extend(
                f"{resolver.name}:{message}" for message in result.errors
            )
            # Each resolver gets its own budget. Sharing one meant a study with
            # thousands of ENA runs consumed it entirely and the repositories
            # holding its processed matrices were never queried at all.
            accepted = 0
            for asset in result.assets:
                if accepted >= self.max_assets:
                    truncated.append(resolver.name)
                    break
                assets[asset.asset_id] = asset
                accepted += 1
            if len(result.assets) >= resolver.max_assets:
                # The adapter stopped at its own cap, so its listing is partial
                # even though this loop never had to trim it.
                truncated.append(resolver.name)
        return AssetResolutionResult(
            candidate_id=candidate.candidate_id,
            queried_accessions=list(dict.fromkeys(queried)),
            assets=list(assets.values()),
            errors=errors,
            truncated_resolvers=list(dict.fromkeys(truncated)),
        )


class _HTTPResolver:
    name = "http_asset_resolver"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_assets: int = 500,
    ) -> None:
        self._client = client
        self.max_assets = max_assets

    @staticmethod
    def _asset(
        candidate: DatasetCandidate,
        *,
        file_name: str,
        url: str,
        source: str,
        size_bytes: int | None = None,
        checksum: str | None = None,
        media_type: str | None = None,
        license_id: str | None = None,
        license_url: str | None = None,
    ) -> DataAsset:
        evidence_ids = [
            evidence.evidence_id for evidence in candidate.evidence
        ]
        return DataAsset(
            asset_id=stable_id(
                "asset", f"{candidate.candidate_id}:{source}:{url}"
            ),
            candidate_id=candidate.candidate_id,
            role=infer_data_role(file_name) or DataRole.OTHER,
            file_name=file_name,
            media_type=media_type,
            compression="gzip" if file_name.endswith(".gz") else None,
            license_id=license_id,
            license_url=license_url,
            locations=[
                AssetLocation(
                    url=url,
                    protocol=TransferProtocol.HTTPS,
                    access_level=AccessLevel.PUBLIC_DECLARED,
                    size_bytes=size_bytes,
                    checksum_algorithm="md5" if checksum else None,
                    checksum=checksum,
                    availability=AssetAvailability.DECLARED,
                    evidence_ids=evidence_ids,
                )
            ],
            evidence_ids=evidence_ids,
        )


class GEOSupplementaryAssetResolver(_HTTPResolver):
    name = "geo_supplementary"
    base_url = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"

    async def resolve(self, candidate: DatasetCandidate) -> AssetResolutionResult:
        accessions = _identifier_values(candidate, "geo_series")
        if not accessions:
            return AssetResolutionResult(candidate_id=candidate.candidate_id)
        owns_client = self._client is None
        client = self._client or default_async_client()
        assets: list[DataAsset] = []
        errors: list[str] = []
        try:
            for accession in accessions:
                try:
                    response = await request_with_retry(
                        client,
                        "GET",
                        self.base_url,
                        params={
                            "acc": accession,
                            "targ": "self",
                            "form": "text",
                            "view": "full",
                        },
                    )
                    urls = re.findall(
                        r"!Series_supplementary_file\s*=\s*(\S+)",
                        response.text,
                    )
                    for url in urls:
                        assets.append(
                            self._asset(
                                candidate,
                                file_name=_url_file_name(url),
                                url=_https_url(url),
                                source=self.name,
                            )
                        )
                        if len(assets) >= self.max_assets:
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
            queried_accessions=accessions,
            assets=assets,
            errors=errors,
        )


class ZenodoAssetResolver(_HTTPResolver):
    name = "zenodo"
    base_url = "https://zenodo.org/api/records"

    async def resolve(self, candidate: DatasetCandidate) -> AssetResolutionResult:
        records = _identifier_values(candidate, "zenodo_record")
        if not records:
            return AssetResolutionResult(candidate_id=candidate.candidate_id)
        owns_client = self._client is None
        client = self._client or default_async_client()
        assets: list[DataAsset] = []
        errors: list[str] = []
        try:
            for record in records:
                try:
                    response = await request_with_retry(
                        client, "GET", f"{self.base_url}/{record}"
                    )
                    payload = response.json()
                    license_id, license_url = _license_fields(
                        payload.get("metadata", {}).get("license")
                    )
                    for item in payload.get("files", []):
                        url = (
                            item.get("links", {}).get("content")
                            or item.get("links", {}).get("self")
                        )
                        if not url:
                            continue
                        checksum = str(item.get("checksum") or "")
                        if checksum.startswith("md5:"):
                            checksum = checksum.removeprefix("md5:")
                        assets.append(
                            self._asset(
                                candidate,
                                file_name=item.get("key")
                                or _url_file_name(url),
                                url=url,
                                source=self.name,
                                size_bytes=_int_or_none(item.get("size")),
                                checksum=checksum or None,
                                license_id=license_id,
                                license_url=license_url,
                            )
                        )
                        if len(assets) >= self.max_assets:
                            break
                except Exception as error:
                    errors.append(
                        f"{record}: {type(error).__name__}: {error}"
                    )
        finally:
            if owns_client:
                await client.aclose()
        return AssetResolutionResult(
            candidate_id=candidate.candidate_id,
            queried_accessions=records,
            assets=assets,
            errors=errors,
        )


class FigshareAssetResolver(_HTTPResolver):
    name = "figshare"
    base_url = "https://api.figshare.com/v2/articles"

    async def resolve(self, candidate: DatasetCandidate) -> AssetResolutionResult:
        articles = _identifier_values(candidate, "figshare_article")
        if not articles:
            return AssetResolutionResult(candidate_id=candidate.candidate_id)
        owns_client = self._client is None
        client = self._client or default_async_client()
        assets: list[DataAsset] = []
        errors: list[str] = []
        try:
            for article in articles:
                try:
                    response = await request_with_retry(
                        client, "GET", f"{self.base_url}/{article}"
                    )
                    payload = response.json()
                    license_id, license_url = _license_fields(
                        payload.get("license")
                    )
                    for item in payload.get("files", []):
                        url = item.get("download_url")
                        if not url:
                            continue
                        assets.append(
                            self._asset(
                                candidate,
                                file_name=item.get("name")
                                or _url_file_name(url),
                                url=url,
                                source=self.name,
                                size_bytes=_int_or_none(item.get("size")),
                                checksum=item.get("supplied_md5"),
                                license_id=license_id,
                                license_url=license_url,
                            )
                        )
                        if len(assets) >= self.max_assets:
                            break
                except Exception as error:
                    errors.append(
                        f"{article}: {type(error).__name__}: {error}"
                    )
        finally:
            if owns_client:
                await client.aclose()
        return AssetResolutionResult(
            candidate_id=candidate.candidate_id,
            queried_accessions=articles,
            assets=assets,
            errors=errors,
        )


def _identifier_values(
    candidate: DatasetCandidate, namespace: str
) -> list[str]:
    return [
        item.value
        for item in candidate.identifiers
        if item.namespace.casefold() == namespace
    ]


def _license_fields(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        identifier = value.get("id") or value.get("title")
        url = value.get("url")
        return (
            str(identifier) if identifier is not None else None,
            str(url) if url is not None else None,
        )
    if value is not None:
        return str(value), None
    return None, None


def _url_file_name(url: str) -> str:
    return (
        PurePosixPath(unquote(urlparse(url).path)).name
        or stable_id("remote_file", url)
    )


def _https_url(url: str) -> str:
    if url.startswith("ftp://"):
        return f"https://{url.removeprefix('ftp://')}"
    return url


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
