"""Independent QC over local, materialized file content."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from celllore.application.agents.shared import AgentContext, latest_artifact
from celllore.application.collaboration import AgentName, ArtifactKind, artifact
from celllore.application.resource_pipeline import barcode_overlap_pairing_claim
from celllore.domain.lifecycle import (
    BundleStatus,
    EvidenceClaim,
    MaterializationManifest,
    MaterializationStatus,
    TransferReceipt,
    TransferReceiptStatus,
)
from celllore.domain.models import GateStatus
from celllore.infrastructure.materialization import MaterializedQualityInspector


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
        failed_receipt_bundle_ids = {
            item.bundle_id
            for item in receipts
            if item.status != TransferReceiptStatus.COMPLETED
        }
        qc_manifests: list[MaterializationManifest] = []
        skipped_bundles: list[dict[str, str]] = []
        for manifest in manifests:
            current = (
                context.catalog.bundle_status(
                    manifest.bundle_id, run_id=context.run_id
                )
                if context.catalog is not None
                else None
            )
            reason = None
            if current is not None and current not in {
                BundleStatus.MATERIALIZED,
                BundleStatus.RAW_ONLY,
            }:
                reason = f"lifecycle_status:{current.value}"
            elif manifest.status not in {
                MaterializationStatus.MATERIALIZED,
                MaterializationStatus.RAW_ONLY,
            }:
                reason = f"materialization_status:{manifest.status.value}"
            elif manifest.bundle_id in failed_receipt_bundle_ids:
                reason = "incomplete_transfer"
            if reason is not None:
                skipped_bundles.append(
                    {"bundle_id": manifest.bundle_id, "reason": reason}
                )
                continue
            qc_manifests.append(manifest)
        manifests = qc_manifests
        inspector = MaterializedQualityInspector()
        reports = []
        for manifest in manifests:
            # Full gzip validation and role-aware QC can scan multi-gigabyte
            # files. Keep it sequential for predictable disk pressure, but
            # move the blocking work off the ASGI event loop so the control
            # surface remains responsive while QC is running.
            reports.append(
                await asyncio.to_thread(inspector.inspect, manifest, receipts)
            )
        for manifest, report in zip(manifests, reports, strict=True):
            package = Path(manifest.package_path)
            qc_root = package / "qc"
            qc_root.mkdir(parents=True, exist_ok=True)
            (qc_root / "validation_report.json").write_text(
                json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            status_path = package / "status.json"
            status_payload = (
                json.loads(status_path.read_text(encoding="utf-8"))
                if status_path.is_file()
                else {}
            )
            status_payload.update({
                "qc_status": report.status.value,
                "unavailable_metrics": report.unavailable_metrics,
                "qc_reasons": report.reasons,
            })
            status_path.write_text(
                json.dumps(status_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # Emit a FILE_OBSERVED/RECOMPUTED-level `has_pair_relationship`
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
                    "skipped_bundles": skipped_bundles,
                },
                parents=[
                    materialization.artifact_id,
                    transfers.artifact_id,
                ],
            )
        ]
