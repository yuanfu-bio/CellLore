"""End-to-end: a curated manifest reaches admitted bundles through both
human approval gates, entirely offline. Mirrors test_supervisor_v2.py's
discovery-path test, but the entry is `ManifestRunSupervisor` and the
candidate/assets come from a real manifest fixture, not a fake resolver.

This is also where C3 is pinned: GSE178707_NEATseq has two pair units in the
fixture (CD4_memory_lane1 and CD4_memory_lane2), which is one candidate with
two bundles, not two candidates seen twice. Before the fix, lineage grouped
both bundles into one family and suppressed all but a "representative" --
rejecting a real sample from a study that was never duplicated at all.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from scdata_agent.application.collaboration import (
    ArtifactKind,
    CollaborationLedger,
    RunStatus,
)
from scdata_agent.application.manifest_ingestion import ingest_manifest
from scdata_agent.application.orchestration_manifest import ManifestRunSupervisor
from scdata_agent.domain.lifecycle import TransferReceipt, TransferReceiptStatus
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    DataRole,
    TransferProtocol,
)
from scdata_agent.infrastructure.manifest_reader import read_manifest

FIXTURE = Path(__file__).parent / "data" / "manifest_paired_multiome_sample.csv"


class _FakeProbe:
    """Stands in for a live HEAD probe resolving every declared URL with 200.

    Milestone 0 relies on the real `RemoteAvailabilityProbe` doing exactly
    this against NCBI; faking it here is what lets `--option recommended`
    (not `all_with_unknown_size`) be exercised without touching the network.
    """

    def __init__(self, **kwargs) -> None:
        pass

    async def probe_assets(self, assets):
        upgraded = []
        for asset in assets:
            locations = [
                location.model_copy(
                    update={
                        "availability": AssetAvailability.OBSERVED,
                        "access_level": AccessLevel.PUBLIC_VERIFIED,
                    }
                )
                for location in asset.locations
            ]
            upgraded.append(asset.model_copy(update={"locations": locations}))
        return upgraded, []


class _FakeTransferExecutor:
    """Writes real local bytes instead of fetching the (real) GEO URLs."""

    name = "fake"

    def __init__(self, paths: dict[str, Path], **kwargs) -> None:
        self.paths = paths

    async def execute(self, plan, bundles, assets, selected_bundle_ids, **kwargs):
        bundle_by_asset = {
            asset_id: bundle.bundle_id
            for bundle in bundles
            if bundle.bundle_id in selected_bundle_ids
            for asset_id in bundle.asset_ids
        }
        return [
            TransferReceipt(
                receipt_id=f"receipt_{asset.asset_id}",
                plan_id=plan.plan_id,
                bundle_id=bundle_by_asset[asset.asset_id],
                asset_id=asset.asset_id,
                source_url=asset.locations[0].url,
                local_path=str(self.paths[asset.asset_id]),
                protocol=TransferProtocol.HTTPS,
                status=TransferReceiptStatus.COMPLETED,
                bytes_written=self.paths[asset.asset_id].stat().st_size,
            )
            for asset in assets
            if asset.asset_id in bundle_by_asset
        ]


def _write_fake_bytes(path: Path, role: DataRole) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if role == DataRole.FRAGMENTS:
        with gzip.open(path, "wt") as handle:
            handle.write("chr1\t10\t20\tcell-1\t1\nchr1\t30\t40\tcell-2\t1\n")
    else:
        with gzip.open(path, "wt") as handle:
            handle.write("id\tvalue\n1\ttest\n")


@pytest.mark.asyncio
async def test_manifest_run_admits_both_pair_units_through_both_gates(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "scdata_agent.application.agents.manifest_ingest.RemoteAvailabilityProbe",
        _FakeProbe,
    )

    document = read_manifest(FIXTURE)
    filters = {"dataset": ["GSE178707_NEATseq"]}
    preview = ingest_manifest(document, run_id="preview", filters=filters, policy={})
    assert len(preview.bundles) == 2  # C3 fixture: one dataset, two pair units

    paths: dict[str, Path] = {}
    for asset in preview.assets:
        path = tmp_path / "raw" / f"{asset.asset_id}_{asset.file_name}"
        _write_fake_bytes(path, asset.role)
        paths[asset.asset_id] = path

    monkeypatch.setattr(
        "scdata_agent.application.agents.transfer_execution.select_transfer_backend",
        lambda policy: _FakeTransferExecutor(paths),
    )

    database = tmp_path / "system.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run(
        "Curated manifest ingestion: GSE178707_NEATseq",
        {
            "source": "manifest",
            "manifest_path": str(FIXTURE),
            "manifest_filters": filters,
            "data_root": str(tmp_path / "data"),
            "remote_probe_enabled": True,
        },
    )
    supervisor = ManifestRunSupervisor(ledger, str(database))
    await supervisor.execute(run.run_id)

    assert ledger.get_run(run.run_id).status == RunStatus.WAITING_FOR_DECISION
    download = next(
        item for item in ledger.list_checkpoints(run.run_id)
        if item.kind == "download_selection"
    )
    # The approval gate: nothing has been fetched before the human decides.
    assert not any(
        item.kind == ArtifactKind.TRANSFER_RECEIPTS
        for item in ledger.list_artifacts(run.run_id)
    )

    await supervisor.resume_checkpoint(download.checkpoint_id, {"option": "recommended"})
    kinds = {item.kind for item in ledger.list_artifacts(run.run_id)}
    assert ArtifactKind.TRANSFER_RECEIPTS in kinds
    assert ArtifactKind.MATERIALIZATION_MANIFEST in kinds
    assert ArtifactKind.MATERIALIZED_QC in kinds
    assert ArtifactKind.CORPUS_SNAPSHOT in kinds

    corpus = next(
        item for item in ledger.list_checkpoints(run.run_id)
        if item.kind == "corpus_snapshot"
    )
    await supervisor.resume_checkpoint(corpus.checkpoint_id, {"option": "approve_snapshot"})
    assert ledger.get_run(run.run_id).status == RunStatus.COMPLETED

    snapshots = supervisor.catalog.list_records(
        record_type="corpus_snapshot", run_id=run.run_id
    )
    admitted = snapshots[-1]["payload"]["admitted_bundle_ids"]
    assert admitted
    # This is the C3 pin: both pair units are admitted, not one representative
    # with the other silently pushed into manual_review.
    assert len(admitted) == 2
