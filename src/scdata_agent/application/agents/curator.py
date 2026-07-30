"""Create source-preserving, processed-first dataset packages."""

from __future__ import annotations

from pathlib import Path

from scdata_agent.application.agents.shared import AgentContext, latest_artifact
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    MaterializationStatus,
    SemanticBundle,
    TransferReceipt,
)
from scdata_agent.domain.models import DataAsset
from scdata_agent.infrastructure.materialization import DatasetMaterializer


class DatasetCuratorAgent:
    name = AgentName.DATASET_CURATOR

    async def run(self, context: AgentContext):
        inventory = latest_artifact(
            context.artifacts, ArtifactKind.ASSET_INVENTORY.value
        )
        transfers = latest_artifact(
            context.artifacts, ArtifactKind.TRANSFER_RECEIPTS.value
        )
        if inventory is None or transfers is None:
            raise RuntimeError("Inventory and transfer receipts are required.")
        assets = [
            DataAsset.model_validate(item)
            for item in inventory.payload.get("assets", [])
        ]
        bundles = [
            SemanticBundle.model_validate(item)
            for item in inventory.payload.get("bundles", [])
        ]
        receipts = [
            TransferReceipt.model_validate(item)
            for item in transfers.payload.get("receipts", [])
        ]
        selected = set(transfers.payload.get("selected_bundle_ids", []))
        root = Path(context.policy.get("data_root", "data")) / "curated"
        materializer = DatasetMaterializer()
        manifests = [
            materializer.materialize(
                bundle=bundle,
                assets=assets,
                receipts=receipts,
                curated_root=root,
            )
            for bundle in bundles
            if bundle.bundle_id in selected
        ]
        if context.catalog is not None:
            for item in manifests:
                context.catalog.append(
                    record_id=item.manifest_id,
                    record_type="materialization_manifest",
                    run_id=context.run_id,
                    value=item,
                )
                target = {
                    MaterializationStatus.MATERIALIZED: BundleStatus.MATERIALIZED,
                    MaterializationStatus.RAW_ONLY: BundleStatus.RAW_ONLY,
                    MaterializationStatus.FAILED: BundleStatus.MATERIALIZATION_FAILED,
                }[item.status]
                if context.catalog.bundle_status(item.bundle_id) == BundleStatus.TRANSFER_FAILED:
                    continue
                context.catalog.transition_bundle(
                    item.bundle_id, target, run_id=context.run_id
                )
        return [
            artifact(
                context.run_id,
                ArtifactKind.MATERIALIZATION_MANIFEST,
                self.name,
                {
                    "manifests": [
                        item.model_dump(mode="json") for item in manifests
                    ]
                },
                parents=[inventory.artifact_id, transfers.artifact_id],
            )
        ]
