"""Metadata-first evidence curation with separately gated full-text access."""

from __future__ import annotations

import json

from celllore.application.agents.evidence_advice import evidence_advice
from celllore.application.agents.shared import (
    AgentContext,
    latest_artifact,
    need_discovery,
)
from celllore.application.collaboration import AgentName, ArtifactKind, artifact
from celllore.application.resource_pipeline import (
    merge_pointers,
    pointer_batches,
    pointer_key,
    pointer_kind,
)
from celllore.domain.identifiers import extract_identifiers, stable_id
from celllore.domain.lifecycle import (
    EvidenceClaim,
    PointerStatus,
    ResourcePointer,
)
from celllore.domain.models import (
    Evidence,
    EvidenceClass,
    ValidationStatus,
)
from celllore.domain.resource_pipeline import (
    EvidenceEscalationReport,
    PointerBatchStage,
    ResourcePointerBatch,
)
from celllore.infrastructure.sources.article_access import (
    OpenArticleAccessService,
)


class ProgressiveEvidenceCuratorAgent:
    """Publish metadata or full-text evidence as explicit pointer deltas."""

    name = AgentName.EVIDENCE_CURATOR

    def __init__(self, phase: str) -> None:
        if phase not in {"metadata", "fulltext"}:
            raise ValueError("Evidence phase must be metadata or fulltext.")
        self.phase = phase

    async def run(self, context: AgentContext):
        if self.phase == "metadata":
            return await self._metadata(context)
        return await self._fulltext(context)

    async def _metadata(self, context: AgentContext):
        discovery = need_discovery(context)
        direct_batches = [
            (item, batch)
            for item, batch in pointer_batches(context)
            if batch.stage == PointerBatchStage.DISCOVERY
        ]
        direct_pointers = merge_pointers(
            [
                pointer
                for _, batch in direct_batches
                for pointer in batch.pointers
            ]
        )
        existing_keys = {pointer_key(item) for item in direct_pointers}
        claims = [
            EvidenceClaim(
                claim_id=stable_id(
                    "claim",
                    f"{pointer.candidate_id}:has_identifier:"
                    f"{pointer.namespace}:{pointer.locator}",
                ),
                subject_id=pointer.candidate_id,
                predicate="has_repository_identifier",
                value={
                    "namespace": pointer.namespace,
                    "value": pointer.locator,
                },
                evidence_ids=pointer.evidence_ids,
                evidence_class=EvidenceClass.REPOSITORY_REPORTED,
                validation_status=ValidationStatus.UNVALIDATED,
            )
            for pointer in direct_pointers
        ]
        records = {
            record.record_id: record for record in discovery.source_records
        }
        metadata_pointers: list[ResourcePointer] = []
        for candidate in discovery.candidates:
            for record_id in candidate.source_record_ids:
                record = records.get(record_id)
                if record is None:
                    continue
                text = " ".join(
                    part
                    for part in (
                        record.title,
                        record.description,
                        json.dumps(
                            record.raw_metadata,
                            ensure_ascii=False,
                            default=str,
                        )[:100_000],
                    )
                    if part
                )
                for identifier in extract_identifiers(text):
                    pointer = ResourcePointer(
                        pointer_id=stable_id(
                            "pointer",
                            f"{candidate.candidate_id}:"
                            f"{identifier.canonical}",
                        ),
                        candidate_id=candidate.candidate_id,
                        namespace=identifier.namespace,
                        locator=identifier.value,
                        source_name=record.source_name,
                        status=PointerStatus.REPORTED,
                        pointer_kind=pointer_kind(
                            identifier.namespace, identifier.value
                        ),
                        origin_stage=(
                            PointerBatchStage.EVIDENCE_METADATA.value
                        ),
                        source_record_id=record.record_id,
                        evidence_ids=[
                            evidence.evidence_id
                            for evidence in record.evidence
                        ],
                        schema_version="2.1",
                    )
                    if pointer_key(pointer) not in existing_keys:
                        metadata_pointers.append(pointer)
                        existing_keys.add(pointer_key(pointer))

        batch = ResourcePointerBatch(
            batch_id=stable_id(
                "pointer_batch", f"{context.run_id}:evidence_metadata"
            ),
            run_id=context.run_id,
            stage=PointerBatchStage.EVIDENCE_METADATA,
            pointers=merge_pointers(metadata_pointers),
            parent_batch_ids=[
                item.artifact_id
                for item, _ in direct_batches
                if item is not None
            ],
        )
        candidates = [
            {
                "candidate_id": candidate.candidate_id,
                "title": candidate.title,
                "referenced_pointer_ids": [
                    pointer.pointer_id
                    for pointer in direct_pointers
                    if pointer.candidate_id == candidate.candidate_id
                ],
            }
            for candidate in discovery.candidates
        ]
        advice = await evidence_advice(context, candidates)
        evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for pointer in [*direct_pointers, *batch.pointers]
                for evidence_id in pointer.evidence_ids
            )
        )
        graph_payload = {
            "phase": "metadata",
            "claims": [
                item.model_dump(mode="json") for item in claims
            ],
            "resource_pointers": [
                item.model_dump(mode="json") for item in batch.pointers
            ],
            "referenced_pointer_ids": [
                item.pointer_id for item in direct_pointers
            ],
            "conflicts": [],
            "enrichment_policy": {
                "strategy": "metadata_then_gap_escalation",
                "fulltext_performed": False,
            },
        }
        outputs = [
            artifact(
                context.run_id,
                ArtifactKind.EVIDENCE_REVIEW,
                self.name,
                {
                    "phase": "metadata",
                    "candidate_count": len(candidates),
                    "candidates": candidates,
                    "conflicts": [],
                    "next_action": advice.next_action,
                    "llm_summary": advice.summary,
                },
                evidence_ids=evidence_ids,
            ),
            artifact(
                context.run_id,
                ArtifactKind.EVIDENCE_GRAPH,
                self.name,
                graph_payload,
                evidence_ids=evidence_ids,
            ),
            artifact(
                context.run_id,
                ArtifactKind.RESOURCE_POINTERS,
                self.name,
                batch.model_dump(mode="json"),
                evidence_ids=evidence_ids,
                parents=batch.parent_batch_ids,
            ),
        ]
        self._persist(context, claims, batch.pointers, [])
        return outputs

    async def _fulltext(self, context: AgentContext):
        discovery = need_discovery(context)
        escalation_artifact = latest_artifact(
            context.artifacts, ArtifactKind.EVIDENCE_ESCALATION.value
        )
        report = (
            EvidenceEscalationReport.model_validate(
                escalation_artifact.payload
            )
            if escalation_artifact is not None
            else EvidenceEscalationReport(
                report_id=stable_id(
                    "evidence_escalation", context.run_id
                ),
                run_id=context.run_id,
            )
        )
        requested = set(report.fulltext_candidate_ids)
        records = {
            record.record_id: record for record in discovery.source_records
        }
        claims: list[EvidenceClaim] = []
        pointers: list[ResourcePointer] = []
        evidence_items: list[Evidence] = []
        attempted: list[str] = []
        resolved: list[str] = []
        remaining = int(context.policy.get("max_fulltexts", 5))
        access = OpenArticleAccessService()
        for candidate in discovery.candidates:
            if candidate.candidate_id not in requested or remaining <= 0:
                continue
            for record_id in candidate.source_record_ids:
                record = records.get(record_id)
                if record is None or record.source_name != "europe_pmc":
                    continue
                attempted.append(candidate.candidate_id)
                content = await access.fetch(record)
                if content is None:
                    continue
                remaining -= 1
                extracted = extract_identifiers(content.text)
                evidence = Evidence(
                    evidence_id=stable_id(
                        "ev", f"{record_id}:{content.snapshot_hash}"
                    ),
                    source_name="europe_pmc_open_fulltext",
                    source_url=content.source_url,
                    locator="PMC open full-text XML; gap-triggered",
                    raw_value={
                        "data_identifiers": [
                            item.model_dump() for item in extracted
                        ]
                    },
                    evidence_class=EvidenceClass.ARTICLE_REPORTED,
                    snapshot_hash=content.snapshot_hash,
                )
                evidence_items.append(evidence)
                for identifier in extracted:
                    pointer = ResourcePointer(
                        pointer_id=stable_id(
                            "pointer",
                            f"{candidate.candidate_id}:"
                            f"{identifier.canonical}",
                        ),
                        candidate_id=candidate.candidate_id,
                        namespace=identifier.namespace,
                        locator=identifier.value,
                        source_name="europe_pmc_open_fulltext",
                        status=PointerStatus.REPORTED,
                        pointer_kind=pointer_kind(
                            identifier.namespace, identifier.value
                        ),
                        origin_stage=(
                            PointerBatchStage.EVIDENCE_FULLTEXT.value
                        ),
                        source_record_id=record.record_id,
                        origin_artifact_id=(
                            escalation_artifact.artifact_id
                            if escalation_artifact is not None
                            else None
                        ),
                        evidence_ids=[evidence.evidence_id],
                        schema_version="2.1",
                    )
                    pointers.append(pointer)
                    claims.append(
                        EvidenceClaim(
                            claim_id=stable_id(
                                "claim",
                                f"{candidate.candidate_id}:fulltext:"
                                f"{identifier.canonical}",
                            ),
                            subject_id=candidate.candidate_id,
                            predicate=(
                                "article_reports_repository_identifier"
                            ),
                            value=identifier.model_dump(),
                            evidence_ids=[evidence.evidence_id],
                            evidence_class=EvidenceClass.ARTICLE_REPORTED,
                        )
                    )
                if extracted:
                    resolved.append(candidate.candidate_id)
                break

        batch = ResourcePointerBatch(
            batch_id=stable_id(
                "pointer_batch", f"{context.run_id}:evidence_fulltext"
            ),
            run_id=context.run_id,
            stage=PointerBatchStage.EVIDENCE_FULLTEXT,
            pointers=merge_pointers(pointers),
        )
        unresolved = sorted(requested - set(resolved))
        evidence_ids = [
            item.evidence_id for item in evidence_items
        ]
        self._persist(context, claims, batch.pointers, evidence_items)
        return [
            artifact(
                context.run_id,
                ArtifactKind.EVIDENCE_REVIEW,
                self.name,
                {
                    "phase": "fulltext",
                    "requested_candidate_ids": sorted(requested),
                    "attempted_candidate_ids": attempted,
                    "resolved_candidate_ids": sorted(resolved),
                    "unresolved_candidate_ids": unresolved,
                    "max_fulltexts": int(
                        context.policy.get("max_fulltexts", 5)
                    ),
                    "paywall_bypass": False,
                },
                evidence_ids=evidence_ids,
                parents=(
                    [escalation_artifact.artifact_id]
                    if escalation_artifact is not None
                    else []
                ),
            ),
            artifact(
                context.run_id,
                ArtifactKind.EVIDENCE_GRAPH,
                self.name,
                {
                    "phase": "fulltext",
                    "claims": [
                        item.model_dump(mode="json") for item in claims
                    ],
                    "new_evidence": [
                        item.model_dump(mode="json")
                        for item in evidence_items
                    ],
                    "resource_pointers": [
                        item.model_dump(mode="json")
                        for item in batch.pointers
                    ],
                    "unresolved_candidate_ids": unresolved,
                },
                evidence_ids=evidence_ids,
            ),
            artifact(
                context.run_id,
                ArtifactKind.RESOURCE_POINTERS,
                self.name,
                batch.model_dump(mode="json"),
                evidence_ids=evidence_ids,
                parents=(
                    [escalation_artifact.artifact_id]
                    if escalation_artifact is not None
                    else []
                ),
            ),
        ]

    @staticmethod
    def _persist(
        context: AgentContext,
        claims: list[EvidenceClaim],
        pointers: list[ResourcePointer],
        evidence_items: list[Evidence],
    ) -> None:
        if context.catalog is None:
            return
        for item in evidence_items:
            context.catalog.append(
                record_id=item.evidence_id,
                record_type="evidence",
                run_id=context.run_id,
                value=item,
            )
        for item in claims:
            context.catalog.append(
                record_id=item.claim_id,
                record_type="evidence_claim",
                run_id=context.run_id,
                value=item,
            )
        for item in pointers:
            context.catalog.append(
                record_id=item.pointer_id,
                record_type="resource_pointer",
                run_id=context.run_id,
                value=item,
            )

