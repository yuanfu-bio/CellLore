"""Bounded metadata-only availability probes for remote assets."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import RemoteAssetObservation
from scdata_agent.domain.models import AccessLevel, AssetAvailability, AssetLocation, DataAsset


class RemoteAvailabilityProbe:
    """Verify URL metadata without consuming response bodies."""

    def __init__(self, *, client: httpx.AsyncClient | None = None,
                 concurrency: int = 8, timeout_seconds: float = 10.0,
                 max_attempts: int = 2) -> None:
        self._client = client
        self._concurrency = max(1, concurrency)
        self._timeout = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._metadata: dict[str, dict[str, object]] = {}

    async def probe_assets(self, assets: list[DataAsset]) -> tuple[list[DataAsset], list[RemoteAssetObservation]]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            follow_redirects=True, timeout=self._timeout,
            headers={"User-Agent": "scdata-agent/0.1 remote-inventory"},
        )
        semaphore = asyncio.Semaphore(self._concurrency)

        async def probe_asset(asset: DataAsset) -> DataAsset:
            locations = await asyncio.gather(*[
                self._probe_location(client, semaphore, location)
                for location in asset.locations
            ])
            return asset.model_copy(update={"locations": locations})

        try:
            probed = await asyncio.gather(*[probe_asset(asset) for asset in assets])
        finally:
            if owns_client:
                await client.aclose()
        observations = [
            self._observation(asset, location)
            for asset in probed for location in asset.locations
        ]
        return list(probed), observations

    async def _probe_location(self, client: httpx.AsyncClient,
                              semaphore: asyncio.Semaphore,
                              location: AssetLocation) -> AssetLocation:
        parsed = urlparse(location.url)
        if (parsed.scheme not in {"http", "https"}
                or location.availability == AssetAvailability.OBSERVED):
            return location
        async with semaphore:
            response: httpx.Response | None = None
            for attempt in range(self._max_attempts):
                try:
                    response = await client.head(location.url)
                    break
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt + 1 == self._max_attempts:
                        return location
            if response is None:
                return location
            if response.status_code in {403, 405, 501}:
                try:
                    async with client.stream("GET", location.url,
                                             headers={"Range": "bytes=0-0"}) as ranged:
                        return self._updated_location(location, ranged)
                except (httpx.TimeoutException, httpx.TransportError):
                    return self._updated_location(location, response)
            return self._updated_location(location, response)

    def _updated_location(self, location: AssetLocation,
                          response: httpx.Response) -> AssetLocation:
        status = response.status_code
        if 200 <= status < 400:
            availability, access = AssetAvailability.OBSERVED, AccessLevel.PUBLIC_VERIFIED
        elif status in {401, 403}:
            availability, access = AssetAvailability.UNKNOWN, AccessLevel.AUTH_REQUIRED
        elif status in {404, 410}:
            availability, access = AssetAvailability.UNAVAILABLE, location.access_level
        else:
            availability, access = location.availability, location.access_level
        size = RemoteAvailabilityProbe._response_size(response)
        self._metadata[location.url] = {
            "http_status": status,
            "final_url": str(response.url),
            "content_type": response.headers.get("Content-Type"),
            "accept_ranges": response.headers.get("Accept-Ranges", "").casefold() == "bytes",
            "redirected": bool(response.history),
            "probe_method": "range" if response.request.headers.get("Range") else "head",
        }
        return location.model_copy(update={
            "access_level": access,
            "availability": availability,
            "size_bytes": size if size is not None else location.size_bytes,
        })

    @staticmethod
    def _response_size(response: httpx.Response) -> int | None:
        match = re.search(r"/(\d+)$", response.headers.get("Content-Range", ""))
        if match:
            return int(match.group(1))
        value = response.headers.get("Content-Length")
        return int(value) if value and value.isdigit() else None

    def _observation(self, asset: DataAsset,
                     location: AssetLocation) -> RemoteAssetObservation:
        is_http = urlparse(location.url).scheme in {"http", "https"}
        metadata = self._metadata.get(location.url, {})
        return RemoteAssetObservation(
            observation_id=stable_id("remote_observation", f"{asset.asset_id}:{location.url}"),
            asset_id=asset.asset_id,
            candidate_id=asset.candidate_id,
            access_level=location.access_level,
            availability=location.availability,
            size_bytes=location.size_bytes,
            checksum_algorithm=location.checksum_algorithm,
            checksum_declared=location.checksum,
            probe_method=str(metadata.get("probe_method") or (
                "unverified_http" if is_http else "repository_file_report"
            )),
            http_status=metadata.get("http_status"),
            final_url=str(metadata.get("final_url") or location.url),
            content_type=metadata.get("content_type"),
            accept_ranges=metadata.get("accept_ranges"),
            redirected=bool(metadata.get("redirected", False)),
            evidence_ids=location.evidence_ids,
        )
