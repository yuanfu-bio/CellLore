"""Classify and optionally retry failed transfers for a run -- without
ever deciding, on its own, what should be transferred.

A repair pass only touches what a human already approved at the
``download_selection`` checkpoint. Requiring that checkpoint to be RESOLVED
with a download option, before this module does anything else, is what
stops "repair" from becoming a second, unaudited approval gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from scdata_agent.application.agents.shared import latest_artifact
from scdata_agent.application.collaboration import (
    ArtifactKind,
    CheckpointStatus,
    CollaborationLedger,
)
from scdata_agent.domain.lifecycle import (
    AcquisitionPlan,
    SemanticBundle,
    TransferReceipt,
    TransferReceiptStatus,
)
from scdata_agent.domain.models import DataAsset
from scdata_agent.domain.transfer_state import RETRYABLE_CLASSES, FailureClass
from scdata_agent.infrastructure.download_triage import probe_and_classify
from scdata_agent.infrastructure.sources.base import default_async_client
from scdata_agent.infrastructure.transfer_backend import select_transfer_backend


class RepairNotAuthorized(RuntimeError):
    """The run's download_selection checkpoint was not resolved to a download."""


@dataclass(frozen=True)
class RepairCandidate:
    asset_id: str
    bundle_id: str
    url: str
    local_status: str
    classification: FailureClass
    http_status: int | None


@dataclass
class RepairReport:
    run_id: str
    candidates: list[RepairCandidate] = field(default_factory=list)

    @property
    def histogram(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.candidates:
            counts[item.classification.value] = counts.get(item.classification.value, 0) + 1
        return counts

    @property
    def retryable(self) -> list[RepairCandidate]:
        return [item for item in self.candidates if item.classification in RETRYABLE_CLASSES]

    @property
    def manifest_audit_required(self) -> list[RepairCandidate]:
        return [
            item for item in self.candidates
            if item.classification == FailureClass.MANIFEST_ERROR_404
        ]


def require_download_approved(ledger: CollaborationLedger, run_id: str) -> dict[str, Any]:
    """The single most important check in this module: refuse to run at all
    unless a human already resolved download_selection to a download option.
    """
    checkpoints = [
        item for item in ledger.list_checkpoints(run_id) if item.kind == "download_selection"
    ]
    if not checkpoints or checkpoints[-1].status != CheckpointStatus.RESOLVED:
        raise RepairNotAuthorized(
            f"Run {run_id}'s download_selection checkpoint is not resolved; "
            "repair only retries what was already approved for transfer."
        )
    resolution = checkpoints[-1].resolution or {}
    if resolution.get("option") not in {"recommended", "all_with_unknown_size"}:
        raise RepairNotAuthorized(
            f"download_selection was resolved with option={resolution.get('option')!r}, "
            "not a download option; nothing was approved for transfer."
        )
    return resolution


def _plan_context(
    ledger: CollaborationLedger, run_id: str
) -> tuple[AcquisitionPlan, list[SemanticBundle], list[DataAsset], dict[str, TransferReceipt], set[str]]:
    artifacts = ledger.list_artifacts(run_id)
    inventory = latest_artifact(artifacts, ArtifactKind.ASSET_INVENTORY.value)
    plan_artifact = latest_artifact(artifacts, ArtifactKind.ACQUISITION_PLAN.value)
    receipts_artifact = latest_artifact(artifacts, ArtifactKind.TRANSFER_RECEIPTS.value)
    if inventory is None or plan_artifact is None:
        raise RepairNotAuthorized(f"Run {run_id} has no acquisition plan or asset inventory yet.")
    assets = [DataAsset.model_validate(item) for item in inventory.payload.get("assets", [])]
    bundles = [SemanticBundle.model_validate(item) for item in inventory.payload.get("bundles", [])]
    plan = AcquisitionPlan.model_validate(plan_artifact.payload)
    receipts_by_asset: dict[str, TransferReceipt] = {}
    selected_bundle_ids: set[str] = set()
    if receipts_artifact is not None:
        selected_bundle_ids = set(receipts_artifact.payload.get("selected_bundle_ids", []))
        receipts_by_asset = {
            item["asset_id"]: TransferReceipt.model_validate(item)
            for item in receipts_artifact.payload.get("receipts", [])
        }
    return plan, bundles, assets, receipts_by_asset, selected_bundle_ids


def _incomplete_assets(
    bundles: list[SemanticBundle],
    assets: list[DataAsset],
    receipts_by_asset: dict[str, TransferReceipt],
    selected_bundle_ids: set[str],
) -> list[DataAsset]:
    selected_asset_ids = {
        asset_id for bundle in bundles if bundle.bundle_id in selected_bundle_ids
        for asset_id in bundle.asset_ids
    }

    def is_incomplete(asset: DataAsset) -> bool:
        if asset.asset_id not in selected_asset_ids:
            return False
        receipt = receipts_by_asset.get(asset.asset_id)
        return receipt is None or receipt.status != TransferReceiptStatus.COMPLETED

    return [asset for asset in assets if is_incomplete(asset)]


def _bundle_for_asset(bundles: list[SemanticBundle], asset_id: str) -> str:
    return next(
        (bundle.bundle_id for bundle in bundles if asset_id in bundle.asset_ids), "unknown"
    )


async def classify_repair_candidates(
    ledger: CollaborationLedger,
    run_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> RepairReport:
    """Probe every incomplete asset's URL and classify why it is incomplete.

    Refuses to run at all unless the run's download was already approved.
    """
    require_download_approved(ledger, run_id)
    plan, bundles, assets, receipts_by_asset, selected_bundle_ids = _plan_context(ledger, run_id)
    incomplete = _incomplete_assets(bundles, assets, receipts_by_asset, selected_bundle_ids)

    owns_client = client is None
    active_client = client or default_async_client()
    report = RepairReport(run_id=run_id)
    try:
        for asset in incomplete:
            if not asset.locations:
                continue
            url = asset.locations[0].url
            probe, classification = await probe_and_classify(active_client, url)
            report.candidates.append(
                RepairCandidate(
                    asset_id=asset.asset_id,
                    bundle_id=_bundle_for_asset(bundles, asset.asset_id),
                    url=url,
                    local_status=(
                        receipts_by_asset[asset.asset_id].status.value
                        if asset.asset_id in receipts_by_asset
                        else "never_attempted"
                    ),
                    classification=classification,
                    http_status=probe.get("status"),
                )
            )
    finally:
        if owns_client:
            await active_client.aclose()
    return report


async def execute_repair(
    ledger: CollaborationLedger,
    run_id: str,
    report: RepairReport,
    *,
    policy: dict[str, Any],
) -> list[TransferReceipt]:
    """Retry only the RETRYABLE_CLASSES subset. Never touches MANIFEST_ERROR_404."""
    require_download_approved(ledger, run_id)
    if not report.retryable:
        return []
    plan, bundles, assets, _receipts_by_asset, _selected = _plan_context(ledger, run_id)
    retry_ids = {item.asset_id for item in report.retryable}
    retry_bundle_ids = {item.bundle_id for item in report.retryable}
    backend = select_transfer_backend(policy)
    return await backend.execute(
        plan,
        [bundle for bundle in bundles if bundle.bundle_id in retry_bundle_ids],
        [asset for asset in assets if asset.asset_id in retry_ids],
        retry_bundle_ids,
        # These assets already cleared the size/budget/disk gate once, at
        # download_approval -- repair retries an approved selection, it does
        # not re-litigate whether it should have been approved.
        allow_unknown_size=True,
        max_download_bytes=policy.get("max_download_bytes"),
    )
