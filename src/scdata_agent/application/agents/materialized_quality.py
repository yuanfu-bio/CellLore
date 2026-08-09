"""Independent QC over local, materialized file content."""

from __future__ import annotations

from scdata_agent.application.agents.shared import AgentContext, latest_artifact
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.resource_pipeline import barcode_overlap_pairing_claim
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    EvidenceClaim,
    MaterializationManifest,
    TransferReceipt,
)
from scdata_agent.domain.models import GateStatus
from scdata_agent.infrastructure.materialization import MaterializedQualityInspector


class MaterializedQualityAssessmentAgent:
    name = AgentName.QUALITY_ASSESSMENT

    async def run(self, context: AgentContext):
        materialization = latest_artifact(
            context.artifacts, ArtifactKind.MATERIALIZATION_MANIFEST.value
        )
        transfers = latest_artifact(
            context.artifacts, ArtifactKind.TRANSFER_RECEIPTS.value
        )
        if materialization is None or transfers is None:
            raise RuntimeError(
                "Materialization manifests and transfer receipts are required."
            )
        manifests = [
            MaterializationManifest.model_validate(item)
            for item in materialization.payload.get("manifests", [])
        ]
        receipts = [
            TransferReceipt.model_validate(item)
            for item in transfers.payload.get("receipts", [])
        ]
        inspector = MaterializedQualityInspector()
        reports = [
            inspector.inspect(manifest, receipts) for manifest in manifests
        ]

        # Phase 5B: a FILE_OBSERVED/RECOMPUTED-level `has_pair_relationship`
        # claim, recomputed from the materialized RNA/ATAC barcode overlap
        # (see resource_pipeline.barcode_overlap_pairing_claim), for the
        # CORPUS_READY gate's stronger acceptable-evidence-class bar --
        # separate from and stronger than the declared-level claim
        # PairingVerifier already produced for the ACQUISITION gate.
        inventory = latest_artifact(context.artifacts, ArtifactKind.ASSET_INVENTORY.value)
        bundle_pair_units = {
            item.get("bundle_id"): item.get("pair_unit_id")
            for item in (inventory.payload.get("bundles", []) if inventory else [])
        }
        declared_pairing_by_pair_unit = {
            claim.subject_id: claim
            for claim in (
                EvidenceClaim.model_validate(raw)
                for raw in (inventory.payload.get("pairing_claims", []) if inventory else [])
            )
            if claim.predicate == "has_pair_relationship"
        }
        pairing_claims: list[EvidenceClaim] = []
        for manifest, report in zip(manifests, reports, strict=True):
            pair_unit_id = bundle_pair_units.get(manifest.bundle_id)
            if pair_unit_id is None:
                continue
            claim = barcode_overlap_pairing_claim(
                pair_unit_id,
                declared_pairing_by_pair_unit.get(pair_unit_id),
                report.metrics,
                report.structural_checks,
            )
            if claim is not None:
                pairing_claims.append(claim)

        if context.catalog is not None:
            for report in reports:
                context.catalog.append(
                    record_id=report.report_id,
                    record_type="materialized_qc",
                    run_id=context.run_id,
                    value=report,
                )
                target = {
                    GateStatus.PASS: BundleStatus.QC_PASSED,
                    GateStatus.WARN: BundleStatus.QC_WARN,
                    GateStatus.UNKNOWN: BundleStatus.QC_WARN,
                    GateStatus.FAIL: BundleStatus.QUARANTINED,
                }[report.status]
                current = context.catalog.bundle_status(report.bundle_id, run_id=context.run_id)
                if current == BundleStatus.RAW_ONLY:
                    target = BundleStatus.QUARANTINED
                context.catalog.transition_bundle(
                    report.bundle_id, target, run_id=context.run_id
                )
        return [
            artifact(
                context.run_id,
                ArtifactKind.MATERIALIZED_QC,
                self.name,
                {
                    "reports": [
                        item.model_dump(mode="json") for item in reports
                    ],
                    "evidence_boundary": "local_file_content",
                    "pairing_claims": [
                        item.model_dump(mode="json") for item in pairing_claims
                    ],
                },
                parents=[
                    materialization.artifact_id,
                    transfers.artifact_id,
                ],
            )
        ]
