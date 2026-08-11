"""Source adapter protocol and HTTP utilities."""

from __future__ import annotations

import asyncio
from typing import Protocol

import httpx

from celllore.domain.models import QuerySpec, SourceRecord


class SourceAdapter(Protocol):
    name: str

    async def search(self, query: QuerySpec) -> list[SourceRecord]:
        ...


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, object] | None = None,
    max_attempts: int = 4,
) -> httpx.Response:
    """Retry bounded transient HTTP failures and honor Retry-After."""
    retryable = {429, 500, 502, 503, 504}
    response: httpx.Response | None = None
    for attempt in range(max_attempts):
        response = await client.request(method, url, params=params)
        if response.status_code not in retryable:
            response.raise_for_status()
            return response
        if attempt == max_attempts - 1:
            break
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else min(2 ** attempt, 8)
        except ValueError:
            delay = min(2 ** attempt, 8)
        await asyncio.sleep(max(delay, 0.1))
    assert response is not None
    response.raise_for_status()
    return response


def default_async_client(*, trust_env: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        trust_env=trust_env,
        headers={
            "User-Agent": (
                "CellLore/0.1.0 "
                "(public single-cell epigenomics metadata discovery)"
            )
        },
    )
