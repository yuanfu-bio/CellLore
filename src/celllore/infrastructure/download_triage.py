"""Classify a failed download without fetching its payload.

The validated prototype shelled out to ``curl`` for this. httpx does the
same probe (a ``Range: bytes=0-0`` request -- metadata, not bytes) without a
new executable dependency, and it is what lets this module be tested with
``httpx.MockTransport`` instead of a real network call.
"""

from __future__ import annotations

from typing import Any

import httpx

from celllore.domain.transfer_state import FailureClass, classify_http_outcome


async def probe_and_classify(
    client: httpx.AsyncClient, url: str
) -> tuple[dict[str, Any], FailureClass]:
    try:
        response = await client.get(url, headers={"Range": "bytes=0-0"})
    except httpx.TransportError as error:
        probe = {
            "status": None,
            "effective_url": url,
            "transport_error": f"{type(error).__name__}: {error}",
        }
        return probe, classify_http_outcome(None, probe["transport_error"])
    probe = {
        "status": response.status_code,
        "effective_url": str(response.url),
        "transport_error": None,
    }
    return probe, classify_http_outcome(response.status_code, None)
