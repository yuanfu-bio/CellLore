import httpx
import pytest

from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    TransferProtocol,
)
from scdata_agent.infrastructure.remote_probe import RemoteAvailabilityProbe


def asset(url: str) -> DataAsset:
    return DataAsset(
        asset_id="asset_1", candidate_id="candidate_1",
        role=DataRole.FRAGMENTS, file_name="fragments.tsv.gz",
        locations=[AssetLocation(
            url=url, protocol=TransferProtocol.HTTPS,
            access_level=AccessLevel.PUBLIC_DECLARED,
            availability=AssetAvailability.DECLARED,
        )],
    )


@pytest.mark.asyncio
async def test_head_probe_verifies_access_and_size():
    async def handler(request):
        assert request.method == "HEAD"
        return httpx.Response(
            200, headers={"Content-Length": "42", "Content-Type": "text/tab-separated-values",
                          "Accept-Ranges": "bytes"}, request=request,
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assets, observations = await RemoteAvailabilityProbe(client=client).probe_assets(
            [asset("https://files.test/fragments.tsv.gz")]
        )
    location = assets[0].locations[0]
    assert location.availability == AssetAvailability.OBSERVED
    assert location.access_level == AccessLevel.PUBLIC_VERIFIED
    assert location.size_bytes == 42
    assert observations[0].http_status == 200
    assert observations[0].accept_ranges is True
    assert observations[0].probe_method == "head"


@pytest.mark.asyncio
async def test_head_unsupported_falls_back_to_zero_byte_range():
    methods = []
    async def handler(request):
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405, request=request)
        assert request.headers["Range"] == "bytes=0-0"
        return httpx.Response(206, headers={"Content-Range": "bytes 0-0/123"}, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assets, observations = await RemoteAvailabilityProbe(client=client).probe_assets(
            [asset("https://files.test/range-only")]
        )
    assert methods == ["HEAD", "GET"]
    assert assets[0].locations[0].size_bytes == 123
    assert observations[0].probe_method == "range"


@pytest.mark.asyncio
async def test_missing_url_is_marked_unavailable():
    async def handler(request):
        return httpx.Response(404, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assets, observations = await RemoteAvailabilityProbe(client=client).probe_assets(
            [asset("https://files.test/missing")]
        )
    assert assets[0].locations[0].availability == AssetAvailability.UNAVAILABLE
    assert observations[0].http_status == 404


@pytest.mark.asyncio
async def test_probe_reports_completed_assets_in_real_time():
    async def handler(request):
        return httpx.Response(200, request=request)

    progress = []
    first = asset("https://files.test/first")
    second = asset("https://files.test/second").model_copy(
        update={"asset_id": "asset_2"}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await RemoteAvailabilityProbe(client=client).probe_assets(
            [first, second],
            progress_callback=lambda completed, total: progress.append(
                (completed, total)
            ),
        )
    assert progress == [(1, 2), (2, 2)]
