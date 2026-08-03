"""Approved transfer execution as a thin wrapper over deterministic workers."""

from __future__ import annotations

from scdata_agent.application.agents.shared import AgentContext, latest_artifact
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.domain.lifecycle import (
    AcquisitionPlan,
    BundleStatus,
    PreflightBundleReport,
    SemanticBundle,
)
from scdata_agent.domain.models import DataAsset
from scdata_agent.infrastructure.transfer_executor import TransferExecutor


class TransferExecutionAgent:
    """Owns execution policy; byte transfer remains deterministic."""

    name = AgentName.ASSET_TRANSFER

    async def run(self, context: AgentContext):
        plan_artifact = latest_artifact(
            context.artifacts, ArtifactKind.ACQUISITION_PLAN.value
        )
        inventory = latest_artifact(
            context.artifacts, ArtifactKind.ASSET_INVENTORY.value
        )
        if plan_artifact is None or inventory is None:
            raise RuntimeError("Acquisition plan and asset inventory are required.")
        plan = AcquisitionPlan.model_validate(plan_artifact.payload)
        assets = [
            DataAsset.model_validate(item)
            for item in inventory.payload.get("assets", [])
        ]
        bundles = [
            SemanticBundle.model_validate(item)
            for item in inventory.payload.get("bundles", [])
        ]
        preflight_artifact = latest_artifact(
            context.artifacts, ArtifactKind.PREFLIGHT_REPORT.value
        )
        ready_ids = {
            report.bundle_id
            for report in (
                PreflightBundleReport.model_validate(item)
                for item in (
                    preflight_artifact.payload.get("reports", [])
                    if preflight_artifact else []
                )
            )
            if report.decision == BundleStatus.PREFLIGHT_READY
        }
        resolution = context.resolution or {}
        selected_ids: set[str] = set()
        if resolution.get("option") == "recommended":
            selected_ids = set(plan.recommended_bundle_ids) & ready_ids
        elif resolution.get("option") == "all_with_unknown_size":
            selected_ids = (
                set(plan.recommended_bundle_ids)
                | set(plan.manual_review_bundle_ids)
            ) & ready_ids
        if context.catalog is not None:
            for bundle_id in selected_ids:
                current = context.catalog.bundle_status(bundle_id, run_id=context.run_id)
                if current == BundleStatus.PREFLIGHT_READY:
                    context.catalog.transition_bundle(
                        bundle_id, BundleStatus.PROPOSED, run_id=context.run_id
                    )
                context.catalog.transition_bundle(
                    bundle_id, BundleStatus.APPROVED, run_id=context.run_id
                )
        receipts = await TransferExecutor(
            concurrency=int(context.policy.get("transfer_concurrency", 3))
        ).execute(
            plan,
            bundles,
            assets,
            selected_ids,
            allow_unknown_size=bool(
                resolution.get("allow_unknown_size")
                or resolution.get("option") == "all_with_unknown_size"
            ),
            max_download_bytes=context.policy.get("max_download_bytes"),
        )
        if context.catalog is not None:
            for receipt in receipts:
                context.catalog.append(
                    record_id=receipt.receipt_id,
                    record_type="transfer_receipt",
                    run_id=context.run_id,
                    value=receipt,
                )
            for bundle_id in selected_ids:
                bundle_receipts = [
                    item for item in receipts if item.bundle_id == bundle_id
                ]
                target = (
                    BundleStatus.TRANSFERRED
                    if bundle_receipts
                    and all(item.status.value == "completed" for item in bundle_receipts)
                    else BundleStatus.TRANSFER_FAILED
                )
                context.catalog.transition_bundle(
                    bundle_id, target, run_id=context.run_id
                )
        return [
            artifact(
                context.run_id,
                ArtifactKind.TRANSFER_RECEIPTS,
                self.name,
                {
                    "plan_id": plan.plan_id,
                    "selected_bundle_ids": sorted(selected_ids),
                    "receipts": [
                        item.model_dump(mode="json") for item in receipts
                    ],
                },
                parents=[plan_artifact.artifact_id, inventory.artifact_id],
            )
        ]
