"""Deterministic gate deciding when metadata gaps justify full-text access."""

from __future__ import annotations

from scdata_agent.application.agents.shared import (
    AgentContext,
    candidate_modalities,
    need_discovery,
)
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.resource_pipeline import pointer_batches
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import Modality, PairingLevel
from scdata_agent.domain.resource_pipeline import (
    AssetInventoryDelta,
    CandidateEvidenceEscalation,
    EvidenceEscalationReport,
    PointerBatchStage,
    ScoutPhase,
)


_PAIRED_MODALITIES = {
    Modality.MULTIOME_RNA_ATAC,
    Modality.MATCHED_SCRNA_SCATAC,
    Modality.MULTIOME_ATAC_PROTEIN,
    Modality.MULTIOME_RNA_ATAC_PROTEIN,
    Modality.ATAC_OTHER_MULTIOMICS,
}


class EvidenceEscalationAgent:
    name = AgentName.EVIDENCE_CURATOR

    async def run(self, context: AgentContext):
        discovery = need_discovery(context)
        batches = pointer_batches(context)
        direct_by_candidate = {
            candidate.candidate_id: [
                pointer
                for _, batch in batches
                if batch.stage == PointerBatchStage.DISCOVERY
                for pointer in batch.pointers
                if pointer.candidate_id == candidate.candidate_id
                and pointer.scoutable
            ]
            for candidate in discovery.candidates
        }
        enriched_by_candidate = {
            candidate.candidate_id: [
                pointer
                for _, batch in batches
                if batch.stage != PointerBatchStage.DISCOVERY
                for pointer in batch.pointers
                if pointer.candidate_id == candidate.candidate_id
                and pointer.scoutable
            ]
            for candidate in discovery.candidates
        }
        direct_assets = {
            asset.candidate_id
            for item in context.artifacts
            if item.kind == ArtifactKind.ASSET_INVENTORY_DELTA
            for delta in [AssetInventoryDelta.model_validate(item.payload)]
            if delta.phase == ScoutPhase.DIRECT
            for asset in delta.assets
        }
        records = {
            item.record_id: item for item in discovery.source_records
        }
        decisions: list[CandidateEvidenceEscalation] = []
        for candidate in discovery.candidates:
            unresolved: list[str] = []
            reasons: list[str] = []
            # Pairing evidence is only owed by candidates that actually claim a
            # paired assay. Deriving this from the question's union of
            # modalities asked standalone scATAC studies to prove a multiome
            # relationship they never asserted, and sent them for full text.
            paired_required = bool(
                candidate_modalities(candidate, context.requirement)
                & _PAIRED_MODALITIES
            )
            if not direct_by_candidate[candidate.candidate_id]:
                unresolved.append("repository_pointer")
                reasons.append(
                    "Discovery did not publish a scoutable repository pointer."
                )
            if candidate.candidate_id not in direct_assets:
                unresolved.append("remote_asset_manifest")
                reasons.append(
                    "Direct Asset Scout did not resolve a remote file manifest."
                )
            if paired_required and (
                not candidate.assay_profiles
                or any(
                    profile.pairing_level
                    in {
                        PairingLevel.UNVERIFIED,
                        PairingLevel.STUDY_LEVEL,
                    }
                    for profile in candidate.assay_profiles
                )
            ):
                unresolved.append("multiome_pairing")
                reasons.append(
                    "The required multi-omics pairing level is unresolved."
                )
            if (
                context.requirement
                and context.requirement.minimum_donors
                and candidate.donor_count is None
            ):
                unresolved.append("donor_count")
                reasons.append(
                    "The research contract requires donor-count evidence."
                )
            has_open_article_route = any(
                record_id in records
                and records[record_id].source_name == "europe_pmc"
                for record_id in candidate.source_record_ids
            )
            fulltext_resolvable = bool(
                {
                    "repository_pointer",
                    "multiome_pairing",
                    "donor_count",
                }
                & set(unresolved)
            )
            requires_fulltext = bool(
                unresolved
                and fulltext_resolvable
                and has_open_article_route
                and context.policy.get(
                    "enable_fulltext_enrichment", True
                )
            )
            requires_enriched = bool(
                enriched_by_candidate[candidate.candidate_id]
                or requires_fulltext
            )
            decisions.append(
                CandidateEvidenceEscalation(
                    candidate_id=candidate.candidate_id,
                    requires_fulltext=requires_fulltext,
                    requires_enriched_scout=requires_enriched,
                    unresolved_fields=list(dict.fromkeys(unresolved)),
                    reasons=reasons,
                )
            )
        report = EvidenceEscalationReport(
            report_id=stable_id(
                "evidence_escalation", context.run_id
            ),
            run_id=context.run_id,
            candidates=decisions,
            fulltext_candidate_ids=[
                item.candidate_id
                for item in decisions
                if item.requires_fulltext
            ],
            enriched_scout_candidate_ids=[
                item.candidate_id
                for item in decisions
                if item.requires_enriched_scout
            ],
        )
        parents = [
            item.artifact_id
            for item in context.artifacts
            if item.kind
            in {
                ArtifactKind.EVIDENCE_GRAPH,
                ArtifactKind.ASSET_INVENTORY_DELTA,
                ArtifactKind.RESOURCE_POINTERS,
            }
        ]
        return [
            artifact(
                context.run_id,
                ArtifactKind.EVIDENCE_ESCALATION,
                self.name,
                report.model_dump(mode="json"),
                parents=parents,
            )
        ]

