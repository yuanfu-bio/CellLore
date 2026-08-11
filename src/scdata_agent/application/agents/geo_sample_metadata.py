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

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from scdata_agent.application.agents.shared import AgentContext, need_discovery
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.domain.assets import infer_data_role
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import (
    DataRole,
    DatasetCandidate,
    Evidence,
    EvidenceClass,
    GeoSampleRecord,
    OmicsLayer,
    ResearchRequirement,
)
from scdata_agent.infrastructure.sources.common import infer_assay_profiles
from scdata_agent.infrastructure.sources.geo_samples import fetch_series_samples

_ATAC_ROLES = {
    DataRole.FRAGMENTS,
    DataRole.FRAGMENT_INDEX,
    DataRole.PEAKS,
    DataRole.PEAK_MATRIX,
}
_RNA_ROLES = {DataRole.RNA_MATRIX}
_PROTEIN_ROLES = {DataRole.PROTEIN_MATRIX}
_PBMC_RE = re.compile(r"\bpbmcs?\b|peripheral blood mononuclear", re.I)


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

        kept_candidates, filter_report = _refine_candidates_from_geo_samples(
            discovery.candidates,
            context.requirement,
            by_candidate,
        )
        kept_ids = {item.candidate_id for item in kept_candidates}
        context.discovery = discovery.model_copy(
            update={
                "candidates": kept_candidates,
                "feasibility_reports": [
                    item
                    for item in discovery.feasibility_reports
                    if item.candidate_id in kept_ids
                ],
            }
        )
        refined_payload = context.discovery.model_dump(mode="json")
        refined_payload["sample_scope_filter"] = filter_report

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
            ),
            artifact(
                context.run_id,
                ArtifactKind.DISCOVERY_RESULT,
                self.name,
                refined_payload,
                evidence_ids=[item.evidence_id for item in evidence_items],
            ),
        ]


def _refine_candidates_from_geo_samples(
    candidates: list[DatasetCandidate],
    requirement: ResearchRequirement | None,
    samples_by_candidate: dict[str, list[GeoSampleRecord]],
) -> tuple[list[DatasetCandidate], dict[str, object]]:
    """Apply hard biological/layer constraints once full GSM metadata exists.

    Discovery is deliberately recall-oriented. After every GEO Sample block
    has been fetched, however, sending studies with no PBMC mention or no
    evidence of both required layers into remote probing and LLM pair-unit
    resolution is wasteful and creates misleading candidate rows.
    """
    if requirement is None:
        return candidates, {"kept_candidate_ids": [item.candidate_id for item in candidates]}

    pbmc_required = any(
        _PBMC_RE.search(value or "")
        for value in [requirement.question, *requirement.cell_types]
    )
    required_layers = set(requirement.required_omics_layers)
    kept: list[DatasetCandidate] = []
    excluded: list[dict[str, object]] = []
    for candidate in candidates:
        samples = samples_by_candidate.get(candidate.candidate_id, [])
        # This is a post-GEO refinement, not a universal source gate. A
        # non-GEO candidate has no GSM records by design, while a GEO fetch
        # failure is missing evidence rather than evidence of absence. Keep
        # both for the existing downstream audit path.
        if not samples:
            kept.append(candidate)
            continue
        reasons: list[str] = []
        if pbmc_required and not _candidate_mentions_pbmc(candidate, samples):
            reasons.append("pbmc_scope_not_observed")
        observed_layers = _observed_sample_layers(candidate, samples)
        missing_layers = sorted(layer.value for layer in required_layers - observed_layers)
        if missing_layers:
            reasons.append("missing_required_layers:" + ",".join(missing_layers))
        if reasons:
            excluded.append({
                "candidate_id": candidate.candidate_id,
                "title": candidate.title,
                "reasons": reasons,
                "observed_layers": sorted(layer.value for layer in observed_layers),
                "sample_count": len(samples),
            })
        else:
            kept.append(candidate)
    return kept, {
        "kept_candidate_ids": [item.candidate_id for item in kept],
        "excluded_candidates": excluded,
    }


def _candidate_mentions_pbmc(
    candidate: DatasetCandidate, samples: list[GeoSampleRecord]
) -> bool:
    texts = [candidate.title, candidate.description]
    for sample in samples:
        texts.extend([
            sample.title,
            sample.source_name,
            *sample.characteristics,
            sample.raw_text,
        ])
    return any(_PBMC_RE.search(value or "") for value in texts)


def _observed_sample_layers(
    candidate: DatasetCandidate, samples: list[GeoSampleRecord]
) -> set[OmicsLayer]:
    layers = {
        layer
        for profile in candidate.assay_profiles
        for layer in profile.omics_layers
    }
    for sample in samples:
        profiles = infer_assay_profiles([
            sample.title,
            sample.source_name,
            *sample.characteristics,
            sample.raw_text,
        ])
        layers.update(
            layer for profile in profiles for layer in profile.omics_layers
        )
        for locator in sample.supplementary_files:
            file_name = PurePosixPath(unquote(urlparse(locator).path)).name or locator
            role = infer_data_role(file_name)
            if role in _ATAC_ROLES:
                layers.add(OmicsLayer.CHROMATIN_ACCESSIBILITY)
            elif role in _RNA_ROLES:
                layers.add(OmicsLayer.TRANSCRIPTOME)
            elif role in _PROTEIN_ROLES:
                layers.add(OmicsLayer.PROTEIN)
            lowered = file_name.casefold()
            if re.search(r"(?:^|[_./-])(?:atac|chromatin|peak)(?:[_./-]|$)", lowered):
                layers.add(OmicsLayer.CHROMATIN_ACCESSIBILITY)
            if re.search(
                r"(?:^|[_./-])(?:rna|gex|gene[_-]?expression|soupx)(?:[_./-]|$)",
                lowered,
            ):
                layers.add(OmicsLayer.TRANSCRIPTOME)
            if re.search(
                r"(?:^|[_./-])(?:adt|protein|antibody|epitope)(?:[_./-]|$)",
                lowered,
            ):
                layers.add(OmicsLayer.PROTEIN)
    return layers
