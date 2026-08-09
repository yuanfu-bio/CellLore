"""Europe PMC publication and preprint discovery adapter."""

from __future__ import annotations

import hashlib
import json

import httpx

from scdata_agent.domain.identifiers import extract_identifiers, stable_id
from scdata_agent.domain.models import (
    AccessLevel,
    Evidence,
    EvidenceClass,
    Identifier,
    QuerySpec,
    SourceRecord,
)
from scdata_agent.infrastructure.sources.base import default_async_client, request_with_retry
from scdata_agent.infrastructure.sources.common import infer_assay_profiles


class EuropePMCAdapter:
    name = "europe_pmc"
    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def search(self, query: QuerySpec) -> list[SourceRecord]:
        owns_client = self._client is None
        client = self._client or default_async_client()
        try:
            response = await request_with_retry(
                client,
                "GET",
                self.base_url,
                params={
                    "query": query.query,
                    "format": "json",
                    "resultType": "core",
                    "pageSize": query.max_results,
                },
            )
            payload = response.json()
            return [
                self._parse_result(item)
                for item in payload.get("resultList", {}).get("result", [])
            ]
        finally:
            if owns_client:
                await client.aclose()

    def _parse_result(self, item: dict) -> SourceRecord:
        title = item.get("title") or "Untitled publication"
        abstract = item.get("abstractText")
        text = " ".join(part for part in (title, abstract) if part)
        identifiers = extract_identifiers(text)
        publication_identifiers = []
        for namespace, key in (
            ("pmid", "pmid"),
            ("pmcid", "pmcid"),
            ("doi", "doi"),
        ):
            if item.get(key):
                publication_identifiers.append(
                    Identifier(namespace=namespace, value=str(item[key]))
                )

        external_id = (
            item.get("pmid")
            or item.get("pmcid")
            or item.get("doi")
            or item.get("id")
            or stable_id("publication", title)
        )
        source_url = (
            f"https://europepmc.org/article/MED/{item['pmid']}"
            if item.get("pmid")
            else None
        )
        assay_profiles = infer_assay_profiles((title, abstract))
        raw_json = json.dumps(item, sort_keys=True, ensure_ascii=False)
        snapshot_hash = f"sha256:{hashlib.sha256(raw_json.encode()).hexdigest()}"
        evidence = [
            Evidence(
                evidence_id=stable_id("ev", f"europe_pmc:{external_id}:record"),
                source_name=self.name,
                source_url=source_url,
                locator="Europe PMC core result",
                raw_value={
                    "title": title,
                    "abstract": abstract,
                    "data_identifiers": [
                        identifier.model_dump() for identifier in identifiers
                    ],
                },
                evidence_class=EvidenceClass.ARTICLE_REPORTED,
                snapshot_hash=snapshot_hash,
            )
        ]
        return SourceRecord(
            record_id=stable_id("src", f"europe_pmc:{external_id}"),
            source_name=self.name,
            external_id=str(external_id),
            title=title,
            description=abstract,
            source_url=source_url,
            identifiers=identifiers,
            publication_identifiers=publication_identifiers,
            assay_profiles=assay_profiles,
            access_level=AccessLevel.PUBLIC_DECLARED,
            raw_metadata=item,
            evidence=evidence,
        )

