"""Layered acquisition decisions over inventory, preflight, and lineage."""

from __future__ import annotations

from collections import defaultdict

from celllore.application.agents.shared import AgentContext, latest_artifact
from celllore.application.collaboration import AgentName, ArtifactKind, artifact
from celllore.application.dataset_layout import production_run_root
from celllore.domain.identifiers import stable_id
from celllore.domain.lifecycle import (
    AcquisitionBundle,
    AcquisitionPlan,
    BundleLineageAssessment,
    BundleStatus,
    LicenseStatus,
    LineageGraphSnapshot,
    PreflightBundleReport,
    SemanticBundle,
)
from celllore.domain.models import GateStatus
from celllore.domain.resource_pipeline import BundleAccessStatus, RemoteBundleInventorySummary


class AcquisitionStrategistAgent:
    """Apply deterministic hard gates, then rank reviewable bundles."""

    name = AgentName.ACQUISITION_STRATEGIST

    async def run(self, context: AgentContext):
        inventory = latest_artifact(context.artifacts, ArtifactKind.ASSET_INVENTORY.value)
        preflight = latest_artifact(context.artifacts, ArtifactKind.PREFLIGHT_REPORT.value)
        lineage_artifacts = [item for item in context.artifacts if item.kind == ArtifactKind.LINEAGE_REPORT]
        lineage_artifact = lineage_artifacts[-1] if lineage_artifacts else None
        bundles = [SemanticBundle.model_validate(item) for item in (inventory.payload.get("bundles", []) if inventory else [])]
        summaries = {
            item.bundle_id: item for item in (
                RemoteBundleInventorySummary.model_validate(raw)
                for raw in (inventory.payload.get("bundle_summaries", []) if inventory else [])
            )
        }
        reports = {
            item.bundle_id: item for item in (
                PreflightBundleReport.model_validate(raw)
                for raw in (preflight.payload.get("reports", []) if preflight else [])
            )
        }
        lineage = (
            LineageGraphSnapshot.model_validate(lineage_artifact.payload)
            if lineage_artifact else LineageGraphSnapshot(
                snapshot_id="missing", run_id=context.run_id, phase="early"
            )
        )
        assessments = {item.bundle_id: item for item in lineage.bundle_assessments}
        # A family is only a duplicate-coverage signal when it spans more than
        # one candidate. A dataset with several samples produces one family per
        # candidate with several bundles in it -- collapsing those to a single
        # representative would reject every sample but one from a study that
        # was never duplicated at all.
        family_candidates = {item.family_id: set(item.candidate_ids) for item in lineage.families}
        family_groups: dict[str, list[SemanticBundle]] = defaultdict(list)
        for bundle in bundles:
            assessment = assessments.get(bundle.bundle_id)
            key = self._grouping_key(bundle, assessment, family_candidates)
            family_groups[key].append(bundle)
        representatives = {
            key: self._representative(items, reports, summaries).bundle_id
            for key, items in family_groups.items()
        }

        decisions: list[AcquisitionBundle] = []
        for bundle in bundles:
            report = reports.get(bundle.bundle_id)
            summary = summaries.get(bundle.bundle_id)
            assessment = assessments.get(bundle.bundle_id, BundleLineageAssessment(
                bundle_id=bundle.bundle_id, candidate_id=bundle.candidate_id
            ))
            key = self._grouping_key(bundle, assessment, family_candidates)
            representative = representatives[key]
            hard_blockers = list(report.hard_blockers if report else ["preflight:missing"])
            if report and report.decision == BundleStatus.REJECTED:
                hard_blockers.append("preflight:rejected")
            if assessment.evaluation_leakage:
                hard_blockers.append("lineage:evaluation_leakage")
            if summary and summary.access_status in {
                BundleAccessStatus.AUTH_REQUIRED, BundleAccessStatus.UNAVAILABLE,
            }:
                hard_blockers.append(f"access:{summary.access_status.value}")
            hard_blockers = list(dict.fromkeys(hard_blockers))
            manual_reasons: list[str] = []
            if report and report.decision == BundleStatus.NEEDS_EVIDENCE:
                manual_reasons.append("preflight:needs_evidence")
            if report and "archive:uninspected" in report.warnings:
                manual_reasons.append("archive:uninspected")
            # Pairing is inferred from text, so a shortfall sends the bundle to
            # a human rather than excluding it: the phrasing may simply not
            # state a relationship the study actually has.
            for pairing_warning in ("pairing:unverified", "pairing:unknown"):
                if report and pairing_warning in report.warnings:
                    manual_reasons.append(pairing_warning)
            # A truncated listing means the bundle is not the whole deposition,
            # so a human decides whether to widen the budget before committing.
            if summary is not None and summary.inventory_truncated:
                manual_reasons.append("inventory:truncated")
            if summary is None or summary.access_status != BundleAccessStatus.VERIFIED:
                manual_reasons.append(
                    f"access:{summary.access_status.value}" if summary else "access:unknown"
                )
            if summary is None or summary.total_bytes is None:
                manual_reasons.append("size:unknown")
            # An archive-policy licence is a sufficient basis to recommend; it
            # is surfaced in `reasons` so the decision stays auditable rather
            # than being folded into "approved".
            if (
                (report is None or report.license_status == LicenseStatus.UNKNOWN)
                and not context.policy.get("allow_unknown_license", False)
            ):
                manual_reasons.append("license:unknown")
            if bundle.bundle_id != representative:
                manual_reasons.append("lineage:non_representative")
            if hard_blockers:
                tier = "excluded"
            elif manual_reasons:
                tier = "manual_review"
            else:
                tier = "recommended"
            components = self._score_components(bundle, report, summary)
            score = 0.0 if hard_blockers else sum(components.values())
            if bundle.bundle_id != representative:
                score = max(0.0, score - 25.0)
            decisions.append(AcquisitionBundle(
                bundle_id=bundle.bundle_id,
                candidate_id=bundle.candidate_id,
                asset_ids=bundle.asset_ids,
                priority=1,
                score=score,
                expected_bytes=summary.total_bytes if summary else None,
                requires_size_override=summary is None or summary.total_bytes is None,
                decision_tier=tier,
                lineage_family_ids=assessment.family_ids,
                representative_bundle_id=representative,
                score_components=components,
                hard_blockers=hard_blockers,
                reasons=[
                    "Remote inventory, preflight, and lineage evidence reconciled.",
                    *(
                        [f"Licence basis: {report.license_basis}"]
                        if report is not None and report.license_basis
                        else []
                    ),
                ],
                risks=[*manual_reasons, *hard_blockers],
            ))
        tier_order = {"recommended": 0, "manual_review": 1, "excluded": 2}
        decisions.sort(key=lambda item: (tier_order[item.decision_tier], -item.score, item.bundle_id))
        decisions = [item.model_copy(update={"priority": index}) for index, item in enumerate(decisions, 1)]
        recommended = [item.bundle_id for item in decisions if item.decision_tier == "recommended"]
        manual = [item.bundle_id for item in decisions if item.decision_tier == "manual_review"]
        excluded = [item.bundle_id for item in decisions if item.decision_tier == "excluded"]
        plan = AcquisitionPlan(
            plan_id=stable_id("acquisition", context.run_id),
            run_id=context.run_id,
            bundles=decisions,
            recommended_bundle_ids=recommended,
            manual_review_bundle_ids=manual,
            excluded_bundle_ids=excluded,
            total_expected_bytes=sum(item.expected_bytes or 0 for item in decisions if item.bundle_id in recommended),
            destination_root=str(production_run_root(
                context.policy.get("data_root", "data"),
                context.run_id,
                context.created_at,
            )),
        )
        if context.catalog is not None:
            context.catalog.append(record_id=plan.plan_id, record_type="acquisition_plan",
                                   run_id=context.run_id, value=plan)
            for bundle_id in recommended:
                context.catalog.transition_bundle(bundle_id, BundleStatus.PROPOSED, run_id=context.run_id)
        parents = [item.artifact_id for item in (inventory, preflight, lineage_artifact) if item is not None]
        return [artifact(context.run_id, ArtifactKind.ACQUISITION_PLAN, self.name,
                         plan.model_dump(mode="json"), parents=parents)]

    @staticmethod
    def _score_components(bundle, report, summary) -> dict[str, float]:
        # Each satisfiable schema is scored on its own terms. A bundle that
        # fully satisfies standalone scATAC scores as well as one that
        # satisfies multiome; breadth is rewarded separately and modestly.
        satisfied = len(bundle.satisfied_schema_ids)
        return {
            "bundle_completeness": 30.0 if bundle.complete else 0.0,
            "schema_breadth": min(satisfied, 2) * 5.0 if satisfied > 1 else 0.0,
            # A licence that was read outranks one inferred from archive terms.
            "license": (
                20.0 if report and report.license_status == LicenseStatus.APPROVED
                else 15.0 if report and report.license_status == LicenseStatus.REPOSITORY_POLICY
                else 0.0
            ),
            "verified_access": 20.0 if summary and summary.access_status == BundleAccessStatus.VERIFIED else 0.0,
            "preflight": 15.0 if report and report.decision == BundleStatus.PREFLIGHT_READY else 0.0,
            "modality_pairing": 10.0 if report and report.dimensions.get("layer_authenticity") == GateStatus.PASS else 0.0,
            "known_size": 5.0 if summary and summary.total_bytes is not None else 0.0,
        }

    @staticmethod
    def _grouping_key(bundle, assessment, family_candidates: dict[str, set[str]]) -> str:
        family_id = assessment.family_ids[0] if assessment and assessment.family_ids else None
        if family_id and len(family_candidates.get(family_id, ())) > 1:
            return family_id
        return f"bundle:{bundle.bundle_id}"

    @classmethod
    def _representative(cls, bundles, reports, summaries):
        ordered = sorted(bundles, key=lambda item: item.bundle_id)
        def rank(bundle):
            report, summary = reports.get(bundle.bundle_id), summaries.get(bundle.bundle_id)
            scale = report.biological_scale if report else {}
            return (
                report is not None and report.decision == BundleStatus.PREFLIGHT_READY,
                report is not None and report.license_status == LicenseStatus.APPROVED,
                summary is not None and summary.access_status == BundleAccessStatus.VERIFIED,
                bundle.complete,
                scale.get("cell_count") or 0,
                scale.get("donor_count") or 0,
                scale.get("sample_count") or 0,
                summary is not None and summary.total_bytes is not None,
            )
        return max(ordered, key=rank)
