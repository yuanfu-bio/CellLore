"""Final governance over materialized QC and late-lineage evidence."""

from __future__ import annotations

import hashlib
import json

from scdata_agent.application.agents.shared import AgentContext, latest_artifact
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    CorpusBundleDecision,
    CorpusSnapshot,
    LicenseStatus,
    LineageGraphSnapshot,
    MaterializationManifest,
    MaterializationStatus,
    MaterializedQCReport,
    PreflightBundleReport,
)
from scdata_agent.domain.models import GateStatus


class SnapshotCorpusGovernanceAgent:
    name = AgentName.CORPUS_GOVERNANCE

    async def run(self, context: AgentContext):
        preflight_artifact = latest_artifact(
            context.artifacts, ArtifactKind.PREFLIGHT_REPORT.value
        )
        materialization_artifact = latest_artifact(
            context.artifacts, ArtifactKind.MATERIALIZATION_MANIFEST.value
        )
        quality_artifact = latest_artifact(
            context.artifacts, ArtifactKind.MATERIALIZED_QC.value
        )
        lineage_artifact = latest_artifact(
            context.artifacts, ArtifactKind.LINEAGE_REPORT.value
        )
        preflight = {
            item.bundle_id: item
            for item in (
                PreflightBundleReport.model_validate(raw)
                for raw in (
                    preflight_artifact.payload.get("reports", [])
                    if preflight_artifact
                    else []
                )
            )
        }
        manifests = {
            item.bundle_id: item
            for item in (
                MaterializationManifest.model_validate(raw)
                for raw in (
                    materialization_artifact.payload.get("manifests", [])
                    if materialization_artifact
                    else []
                )
            )
        }
        quality = {
            item.bundle_id: item
            for item in (
                MaterializedQCReport.model_validate(raw)
                for raw in (
                    quality_artifact.payload.get("reports", [])
                    if quality_artifact
                    else []
                )
            )
        }
        lineage = (
            LineageGraphSnapshot.model_validate(lineage_artifact.payload)
            if lineage_artifact
            else None
        )
        evaluation_bundle_ids = {
            bundle_id
            for family in (lineage.families if lineage else [])
            if family.family_id in set(
                lineage.evaluation_family_ids if lineage else []
            )
            for bundle_id in family.bundle_ids
        }
        all_bundle_ids = sorted(
            set(preflight) | set(manifests) | set(quality)
        )
        decisions: list[CorpusBundleDecision] = []
        for bundle_id in all_bundle_ids:
            report = preflight.get(bundle_id)
            manifest = manifests.get(bundle_id)
            qc = quality.get(bundle_id)
            candidate_id = (
                report.candidate_id
                if report
                else manifest.candidate_id
                if manifest
                else qc.candidate_id
            )
            reasons: list[str] = []
            if bundle_id in evaluation_bundle_ids:
                pool = "exclude"
                reasons.append("evaluation_lineage_leakage")
            elif report and report.license_status == LicenseStatus.RESTRICTED:
                pool = "exclude"
                reasons.append("license_restricted")
            elif report is None or report.license_status not in {
                LicenseStatus.APPROVED,
                LicenseStatus.REPOSITORY_POLICY,
            }:
                pool = "quarantine"
                reasons.append("license_unknown")
            elif manifest is None or manifest.status != MaterializationStatus.MATERIALIZED:
                pool = "quarantine"
                reasons.append("standard_package_incomplete")
            # A native mtx/tsv.gz/fragments package with a manifest, a data
            # card, provenance and passing checksums is a legitimate training
            # input. Requiring AnnData here made file format the admission
            # gate and quality an afterthought; format is now a dimension the
            # snapshot records rather than a hard gate, unless the caller
            # opts into requiring it.
            elif manifest.standard_object_path is None and context.policy.get(
                "require_standard_object", False
            ):
                pool = "quarantine"
                reasons.append("anndata_or_mudata_missing")
            elif qc is None or qc.status == GateStatus.FAIL:
                pool = "quarantine"
                reasons.append("materialized_qc_failed_or_missing")
            elif qc.status == GateStatus.WARN and not context.policy.get(
                "allow_qc_warn", False
            ):
                pool = "quarantine"
                reasons.append("materialized_qc_warning")
            else:
                pool = "pretrain"
                reasons.append("hard_gates_passed")
                if manifest.standard_object_path is None:
                    # Travels with the snapshot rather than being silently
                    # dropped: the data card and provenance must be able to
                    # say what format the admitted bundle is actually in.
                    reasons.append("standard_object:absent")
                # Admitting on archive terms rather than a read licence is a
                # weaker basis; it must travel with the snapshot, not be lost.
                if report.license_status == LicenseStatus.REPOSITORY_POLICY:
                    reasons.extend(
                        f"license_repository_policy:{item}"
                        for item in report.license_policy_ids
                    )
            decisions.append(
                CorpusBundleDecision(
                    bundle_id=bundle_id,
                    candidate_id=candidate_id,
                    pool=pool,
                    eligible=pool == "pretrain",
                    reasons=reasons,
                )
            )
        rendered = json.dumps(
            [item.model_dump(mode="json") for item in decisions],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot = CorpusSnapshot(
            snapshot_id=stable_id("corpus_snapshot", context.run_id),
            run_id=context.run_id,
            policy_version="corpus_policy_v2",
            decisions=decisions,
            admitted_bundle_ids=[
                item.bundle_id for item in decisions if item.pool == "pretrain"
            ],
            quarantined_bundle_ids=[
                item.bundle_id for item in decisions if item.pool == "quarantine"
            ],
            excluded_bundle_ids=[
                item.bundle_id for item in decisions if item.pool == "exclude"
            ],
            manifest_hash=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )
        if context.catalog is not None:
            context.catalog.append(
                record_id=snapshot.snapshot_id,
                record_type="corpus_snapshot",
                run_id=context.run_id,
                value=snapshot,
            )
            for decision in decisions:
                target = {
                    "pretrain": BundleStatus.ADMITTED,
                    "quarantine": BundleStatus.QUARANTINED,
                    "exclude": BundleStatus.EXCLUDED,
                }[decision.pool]
                current = context.catalog.bundle_status(decision.bundle_id, run_id=context.run_id)
                # Terminal lifecycle states preserve prior audit outcomes.
                # Governance records its decision in the snapshot without
                # attempting an invalid rewrite of the catalog state.
                if current in {
                    BundleStatus.REJECTED,
                    BundleStatus.ADMITTED,
                    BundleStatus.EXCLUDED,
                }:
                    continue
                if current is not None and current != target:
                    context.catalog.transition_bundle(
                        decision.bundle_id, target, run_id=context.run_id
                    )
        parents = [
            item.artifact_id
            for item in (
                preflight_artifact,
                materialization_artifact,
                quality_artifact,
                lineage_artifact,
            )
            if item is not None
        ]
        return [
            artifact(
                context.run_id,
                ArtifactKind.CORPUS_REPORT,
                self.name,
                {
                    "decisions": [
                        item.model_dump(mode="json") for item in decisions
                    ],
                    "recommendation": (
                        "Approve an immutable snapshot only after reviewing "
                        "quarantine and exclusion reasons."
                    ),
                },
                parents=parents,
            ),
            artifact(
                context.run_id,
                ArtifactKind.CORPUS_SNAPSHOT,
                self.name,
                snapshot.model_dump(mode="json"),
                parents=parents,
            ),
        ]
