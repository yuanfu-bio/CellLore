"""NCBI Entrez adapters for GEO DataSets and SRA."""

from __future__ import annotations

import hashlib
import json
import re

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


class NCBIEntrezAdapter:
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(
        self,
        database: str,
        name: str,
        client: httpx.AsyncClient | None = None,
        email: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if database not in {"gds", "sra"}:
            raise ValueError("Only the gds and sra Entrez databases are supported.")
        self.database = database
        self.name = name
        self._client = client
        self.email = email
        self.api_key = api_key

    async def search(self, query: QuerySpec) -> list[SourceRecord]:
        owns_client = self._client is None
        client = self._client or default_async_client()
        common = {"retmode": "json"}
        if self.email:
            common["email"] = self.email
        if self.api_key:
            common["api_key"] = self.api_key
        try:
            search_response = await request_with_retry(
                client,
                "GET",
                f"{self.base_url}/esearch.fcgi",
                params={
                    **common,
                    "db": self.database,
                    "term": query.query,
                    "retmax": query.max_results,
                },
            )
            ids = search_response.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return []
            summary_response = await request_with_retry(
                client,
                "GET",
                f"{self.base_url}/esummary.fcgi",
                params={
                    **common,
                    "db": self.database,
                    "id": ",".join(ids),
                },
            )
            result = summary_response.json().get("result", {})
            return [
                self._parse_summary(uid, result[uid])
                for uid in result.get("uids", [])
                if uid in result
            ]
        finally:
            if owns_client:
                await client.aclose()

    def _parse_summary(self, uid: str, item: dict) -> SourceRecord:
        title = item.get("title") or item.get("studytitle") or f"NCBI record {uid}"
        description = (
            item.get("summary")
            or item.get("expxml")
            or item.get("studyabstract")
            or item.get("runs")
        )
        text = " ".join(
            str(value)
            for value in (
                title,
                description,
                item.get("accession"),
                item.get("gse"),
                item.get("bioproject"),
                item.get("runs"),
                item.get("expxml"),
            )
            if value
        )
        identifiers = extract_identifiers(text)
        accession = item.get("accession")
        if accession and re.fullmatch(r"GSE\d+", str(accession), re.IGNORECASE):
            identifiers.append(
                Identifier(namespace="geo_series", value=str(accession).upper())
            )
        identifiers = list(
            {identifier.canonical: identifier for identifier in identifiers}.values()
        )
        source_url = (
            f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}"
            if self.database == "gds" and accession
            else f"https://www.ncbi.nlm.nih.gov/sra/?term={uid}"
        )
        raw_json = json.dumps(item, sort_keys=True, ensure_ascii=False)
        snapshot_hash = f"sha256:{hashlib.sha256(raw_json.encode()).hexdigest()}"
        organisms = _as_list(item.get("taxon") or item.get("organism"))
        assay_profiles = infer_assay_profiles((title, str(description)))
        modalities = list(dict.fromkeys(
            modality for profile in assay_profiles for modality in profile.modalities
        ))
        evidence = [
            Evidence(
                evidence_id=stable_id("ev", f"{self.name}:{uid}:summary"),
                source_name=self.name,
                source_url=source_url,
                locator=f"NCBI Entrez {self.database} ESummary uid={uid}",
                raw_value=item,
                evidence_class=EvidenceClass.REPOSITORY_REPORTED,
                snapshot_hash=snapshot_hash,
                validation_status="validated",
            )
        ]
        return SourceRecord(
            record_id=stable_id("src", f"{self.name}:{uid}"),
            source_name=self.name,
            external_id=str(accession or uid),
            title=title,
            description=str(description) if description else None,
            source_url=source_url,
            identifiers=identifiers,
            organisms=organisms,
            modalities=modalities,
            assay_profiles=assay_profiles,
            access_level=AccessLevel.PUBLIC_DECLARED,
            raw_metadata=item,
            evidence=evidence,
        )


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        parts = re.split(r"[;,|]", value)
        return [part.strip() for part in parts if part.strip()]
    return [str(value)]

