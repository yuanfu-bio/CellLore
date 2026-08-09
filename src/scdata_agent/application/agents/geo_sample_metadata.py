"""Fetch per-GSM sample metadata for candidates with a GEO series accession.

The raw material PairUnitResolver needs: sample title/characteristics/
source_name/organism/supplementary files, fetched in one request per series
(``infrastructure.sources.geo_samples``). Every sample's full text block is
also stored as an ``Evidence`` (``REPOSITORY_REPORTED``), so a grouping claim
PairUnitResolver makes from this text can be traced back to exactly what it
read -- consistent with how every other evidence-producing agent in this
pipeline works.
"""

from __future__ import annotations

from scdata_agent.application.agents.shared import AgentContext, need_discovery
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import Evidence, EvidenceClass, GeoSampleRecord
from scdata_agent.infrastructure.sources.geo_samples import fetch_series_samples


class GeoSampleMetadataAgent:
    name = AgentName.GEO_SAMPLE_METADATA

    async def run(self, context: AgentContext):
        discovery = need_discovery(context)
        by_candidate: dict[str, list[GeoSampleRecord]] = {}
        evidence_items: list[Evidence] = []
        errors: list[str] = []

        for candidate in discovery.candidates:
            series_accessions = [
                item.value
                for item in candidate.identifiers
                if item.namespace.casefold() == "geo_series"
            ]
            if not series_accessions:
                continue
            records: list[GeoSampleRecord] = []
            for accession in series_accessions:
                try:
                    fetched = await fetch_series_samples(accession)
                except Exception as error:
                    errors.append(
                        f"{candidate.candidate_id}:{accession}: "
                        f"{type(error).__name__}: {error}"
                    )
                    continue
                for record in fetched:
                    evidence = Evidence(
                        evidence_id=stable_id(
                            "ev", f"geo_sample:{record.gsm_accession}:{record.raw_text}"
                        ),
                        source_name="ncbi_geo",
                        locator=(
                            f"acc.cgi?acc={record.series_accession}&targ=gsm"
                            f"#{record.gsm_accession}"
                        ),
                        raw_value=record.raw_text,
                        evidence_class=EvidenceClass.REPOSITORY_REPORTED,
                    )
                    evidence_items.append(evidence)
                    records.append(
                        record.model_copy(
                            update={"evidence_ids": [evidence.evidence_id]}
                        )
                    )
            if records:
                by_candidate[candidate.candidate_id] = records

        if context.catalog is not None:
            for item in evidence_items:
                context.catalog.append(
                    record_id=item.evidence_id,
                    record_type="evidence",
                    run_id=context.run_id,
                    value=item,
                )

        return [
            artifact(
                context.run_id,
                ArtifactKind.GEO_SAMPLE_METADATA,
                self.name,
                {
                    "samples_by_candidate": {
                        candidate_id: [item.model_dump(mode="json") for item in records]
                        for candidate_id, records in by_candidate.items()
                    },
                    "errors": errors,
                },
                evidence_ids=[item.evidence_id for item in evidence_items],
            )
        ]
