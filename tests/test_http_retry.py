import httpx
import pytest

from scdata_agent.infrastructure.sources.base import request_with_retry


@pytest.mark.asyncio
async def test_request_with_retry_recovers_from_rate_limit():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "https://example.test")

    assert response.json() == {"ok": True}
    assert calls == 2
