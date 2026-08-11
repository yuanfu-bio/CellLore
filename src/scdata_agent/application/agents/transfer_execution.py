"""Approved transfer execution as a thin wrapper over deterministic workers."""

from __future__ import annotations

from pathlib import PurePosixPath

from scdata_agent.application.agents.shared import AgentContext, latest_artifact
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.dataset_layout import (
    raw_stage_for_role,
    readable_pair_unit_id,
    study_accession,
)
from scdata_agent.domain.lifecycle import (
    AcquisitionPlan,
    BundleStatus,
    PreflightBundleReport,
    SemanticBundle,
)
from scdata_agent.domain.models import DataAsset, DatasetCandidate
from scdata_agent.domain.pair_units import ProposedPairUnit
from scdata_agent.infrastructure.transfer_backend import select_transfer_backend


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
        elif resolution.get("option") == "selected_recommended":
            # The supervisor validates this against the checkpoint before resuming.
            selected_ids = (
                set(resolution.get("bundle_ids", []))
                & set(plan.recommended_bundle_ids)
                & ready_ids
            )
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
        backend = select_transfer_backend(context.policy)
        receipts = await backend.execute(
            plan,
            bundles,
            assets,
            selected_ids,
            allow_unknown_size=bool(
                resolution.get("allow_unknown_size")
                or resolution.get("option") == "all_with_unknown_size"
            ),
            max_download_bytes=context.policy.get("max_download_bytes"),
            layout=(
                self._layout(context)
                if context.policy.get("honour_manifest_layout", True)
                else None
            ),
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
                    "downloader": backend.name,
                    "receipts": [
                        item.model_dump(mode="json") for item in receipts
                    ],
                },
                parents=[plan_artifact.artifact_id, inventory.artifact_id],
            )
        ]

    @staticmethod
    def _layout(context: AgentContext) -> dict[str, str]:
        """Map assets into a study/pair-unit/role production tree."""
        ingestion = latest_artifact(context.artifacts, ArtifactKind.MANIFEST_INGESTION.value)
        if ingestion is not None:
            return {
                item["asset_id"]: item["relative_path"]
                for item in ingestion.payload.get("sidecars", [])
            }

        discovery = latest_artifact(context.artifacts, ArtifactKind.DISCOVERY_RESULT.value)
        inventory = latest_artifact(context.artifacts, ArtifactKind.ASSET_INVENTORY.value)
        if discovery is None or inventory is None:
            return {}
        candidates = {
            item.candidate_id: item
            for raw in discovery.payload.get("candidates", [])
            for item in [DatasetCandidate.model_validate({
                key: value for key, value in raw.items()
                if key in DatasetCandidate.model_fields
            })]
        }
        assets = {
            item.asset_id: item
            for item in (
                DataAsset.model_validate(raw)
                for raw in inventory.payload.get("assets", [])
            )
        }
        units = {
            item.proposed_unit_id: item
            for item in (
                ProposedPairUnit.model_validate(raw)
                for raw in inventory.payload.get("proposed_pair_units", [])
            )
        }
        layout: dict[str, str] = {}
        for bundle in (
            SemanticBundle.model_validate(raw)
            for raw in inventory.payload.get("bundles", [])
        ):
            candidate = candidates.get(bundle.candidate_id)
            if candidate is None:
                continue
            bundle_assets = [assets[item] for item in bundle.asset_ids if item in assets]
            pair_id = readable_pair_unit_id(
                bundle,
                units.get(bundle.pair_unit_id) if bundle.pair_unit_id else None,
                bundle_assets,
            )
            for asset in bundle_assets:
                layout.setdefault(
                    asset.asset_id,
                    PurePosixPath(
                        study_accession(candidate),
                        pair_id,
                        raw_stage_for_role(asset.role),
                        asset.file_name,
                    ).as_posix(),
                )
        return layout
