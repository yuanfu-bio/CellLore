"""Independent QC over local, materialized file content."""

from __future__ import annotations

from scdata_agent.application.agents.shared import AgentContext, latest_artifact
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.domain.lifecycle import (
    BundleStatus,
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
                },
                parents=[
                    materialization.artifact_id,
                    transfers.artifact_id,
                ],
            )
        ]
