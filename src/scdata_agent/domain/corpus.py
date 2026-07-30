"""Deterministic corpus admission and Evaluation Vault policies."""

from __future__ import annotations

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import (
    AccessLevel,
    AssetBundle,
    CorpusDecisionRecord,
    CorpusPool,
    CurationDecision,
    DatasetCandidate,
    FeasibilityReport,
)


DISALLOWED_ACCESS = {
    AccessLevel.CONTROLLED,
    AccessLevel.EMBARGOED,
    AccessLevel.WITHDRAWN,
}


class CorpusGovernor:
    """Apply hard gates before any balancing or model-based ranking."""

    def decide(
        self,
        candidate: DatasetCandidate,
        report: FeasibilityReport,
        bundles: list[AssetBundle],
        *,
        lineage_family_ids: list[str] | None = None,
        evaluation_family_ids: set[str] | None = None,
        target_pool: CorpusPool = CorpusPool.PRETRAIN,
    ) -> CorpusDecisionRecord:
        lineage_family_ids = lineage_family_ids or []
        evaluation_family_ids = evaluation_family_ids or set()
        reasons: list[str] = []

        if candidate.access_level in DISALLOWED_ACCESS:
            pool = CorpusPool.EXCLUDE
            reasons.append("access_not_public")
        elif report.decision == CurationDecision.EXCLUDE:
            pool = CorpusPool.EXCLUDE
            reasons.append("feasibility_hard_fail")
        elif target_pool != CorpusPool.EVALUATION_VAULT and (
            set(lineage_family_ids) & evaluation_family_ids
        ):
            pool = CorpusPool.EXCLUDE
            reasons.append("evaluation_lineage_leakage")
        elif not bundles or any(not bundle.complete for bundle in bundles):
            pool = CorpusPool.QUARANTINE
            reasons.append("asset_bundle_incomplete")
        elif report.decision == CurationDecision.MANUAL_REVIEW:
            pool = CorpusPool.QUARANTINE
            reasons.append("manual_review_required")
        else:
            pool = target_pool
            reasons.append("hard_gates_passed")

        return CorpusDecisionRecord(
            decision_id=stable_id(
                "corpus_decision",
                f"{candidate.candidate_id}:{report.report_id}:{pool.value}",
            ),
            candidate_id=candidate.candidate_id,
            pool=pool,
            eligible=pool
            in {
                CorpusPool.PRETRAIN,
                CorpusPool.FINETUNE,
                CorpusPool.EVALUATION_VAULT,
            },
            reason_codes=reasons,
            feasibility_report_id=report.report_id,
            bundle_ids=[bundle.bundle_id for bundle in bundles],
            lineage_family_ids=lineage_family_ids,
        )
