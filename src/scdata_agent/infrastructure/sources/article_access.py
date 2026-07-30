"""Open-access article retrieval used only for unresolved evidence gaps."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from xml.etree import ElementTree

import httpx

from scdata_agent.domain.models import SourceRecord
from scdata_agent.infrastructure.sources.base import (
    default_async_client,
    request_with_retry,
)


@dataclass(frozen=True)
class ArticleContent:
    source_record_id: str
    source_url: str
    text: str
    snapshot_hash: str
    access_stage: str = "open_fulltext"


class OpenArticleAccessService:
    """Fetch PMC XML only; never bypass authentication or a paywall."""

    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_text_bytes: int = 5_000_000,
    ) -> None:
        self._client = client
        self.max_text_bytes = max_text_bytes

    async def fetch(self, record: SourceRecord) -> ArticleContent | None:
        pmcid = next(
            (
                item.value
                for item in record.publication_identifiers
                if item.namespace.casefold() == "pmcid"
            ),
            None,
        )
        if pmcid is None:
            return None
        url = f"{self.base_url}/{pmcid}/fullTextXML"
        owns_client = self._client is None
        client = self._client or default_async_client()
        try:
            response = await request_with_retry(client, "GET", url)
            raw = response.content
            root = ElementTree.fromstring(raw)
            text = " ".join(
                value.strip()
                for value in root.itertext()
                if value and value.strip()
            )
            text = text[: self.max_text_bytes]
            digest = hashlib.sha256(raw).hexdigest()
            return ArticleContent(
                source_record_id=record.record_id,
                source_url=url,
                text=text,
                snapshot_hash=f"sha256:{digest}",
            )
        except (httpx.HTTPError, ElementTree.ParseError):
            return None
        finally:
            if owns_client:
                await client.aclose()
