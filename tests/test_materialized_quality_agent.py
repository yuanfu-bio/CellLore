import json
from unittest.mock import MagicMock

import pytest

from celllore.application.agents.materialized_quality import (
    MaterializedQualityAssessmentAgent,
)
from celllore.application.agents.shared import AgentContext
from celllore.application.collaboration import AgentName, ArtifactKind, artifact
from celllore.domain.lifecycle import (
    BundleStatus,
    MaterializationManifest,
    MaterializationStatus,
    MaterializedQCReport,
    TransferReceipt,
    TransferReceiptStatus,
)
from celllore.domain.models import GateStatus, TransferProtocol
from celllore.infrastructure.materialization import MaterializedQualityInspector
from celllore.infrastructure.persistence.catalog import Catalog


@pytest.mark.asyncio
async def test_quality_skips_transfer_failed_bundle(tmp_path, monkeypatch):
    run_id = "run_partial_transfer"
    database = tmp_path / "catalog.sqlite"
    catalog = Catalog(database)
    catalog.transition_bundle(
        "bundle_ready", BundleStatus.MATERIALIZED, run_id=run_id
    )
    catalog.transition_bundle(
        "bundle_failed", BundleStatus.TRANSFER_FAILED, run_id=run_id
    )

    ready_package = tmp_path / "ready"
    failed_package = tmp_path / "failed"
    manifests = [
        MaterializationManifest(
            manifest_id="manifest_ready",
            bundle_id="bundle_ready",
            candidate_id="candidate_ready",
            status=MaterializationStatus.MATERIALIZED,
            package_path=str(ready_package),
        ),
        MaterializationManifest(
            manifest_id="manifest_failed",
            bundle_id="bundle_failed",
            candidate_id="candidate_failed",
            status=MaterializationStatus.MATERIALIZED,
            package_path=str(failed_package),
        ),
    ]
    receipts = [
        TransferReceipt(
            receipt_id="receipt_ready",
            plan_id="plan_1",
            bundle_id="bundle_ready",
            asset_id="asset_ready",
            source_url="https://example.test/ready",
            local_path=str(tmp_path / "ready.bin"),
            protocol=TransferProtocol.HTTPS,
            status=TransferReceiptStatus.COMPLETED,
        ),
        TransferReceipt(
            receipt_id="receipt_failed",
            plan_id="plan_1",
            bundle_id="bundle_failed",
            asset_id="asset_failed",
            source_url="https://example.test/failed",
            local_path=str(tmp_path / "failed.bin.part"),
            protocol=TransferProtocol.HTTPS,
            status=TransferReceiptStatus.FAILED,
            error="Disk quota exceeded",
        ),
    ]
    inspected: list[str] = []

    def inspect(_self, manifest, _receipts):
        inspected.append(manifest.bundle_id)
        return MaterializedQCReport(
            report_id=f"qc_{manifest.bundle_id}",
            bundle_id=manifest.bundle_id,
            candidate_id=manifest.candidate_id,
            status=GateStatus.PASS,
        )

    monkeypatch.setattr(MaterializedQualityInspector, "inspect", inspect)
    materialization = artifact(
        run_id,
        ArtifactKind.MATERIALIZATION_MANIFEST,
        AgentName.DATASET_CURATOR,
        {"manifests": [item.model_dump(mode="json") for item in manifests]},
    )
    transfers = artifact(
        run_id,
        ArtifactKind.TRANSFER_RECEIPTS,
        AgentName.ASSET_TRANSFER,
        {
            "selected_bundle_ids": ["bundle_ready", "bundle_failed"],
            "receipts": [item.model_dump(mode="json") for item in receipts],
        },
    )
    context = AgentContext(
        run_id=run_id,
        question="test partial transfer",
        database_path=str(database),
        ledger=MagicMock(),
        artifacts=[transfers, materialization],
        catalog=catalog,
    )

    outputs = await MaterializedQualityAssessmentAgent().run(context)

    assert inspected == ["bundle_ready"]
    assert [item["bundle_id"] for item in outputs[0].payload["reports"]] == [
        "bundle_ready"
    ]
    assert outputs[0].payload["skipped_bundles"] == [
        {
            "bundle_id": "bundle_failed",
            "reason": "lifecycle_status:transfer_failed",
        }
    ]
    assert (
        catalog.bundle_status("bundle_ready", run_id=run_id)
        == BundleStatus.QC_PASSED
    )
    assert (
        catalog.bundle_status("bundle_failed", run_id=run_id)
        == BundleStatus.TRANSFER_FAILED
    )
    assert (
        json.loads((ready_package / "status.json").read_text())["qc_status"]
        == "pass"
    )
    assert not (failed_package / "status.json").exists()
