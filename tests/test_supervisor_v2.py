from pathlib import Path

import pytest

from scdata_agent.application.agents.supervisor import MultiAgentSupervisor
from scdata_agent.application.collaboration import (
    ArtifactKind,
    CollaborationLedger,
    RunStatus,
)
from scdata_agent.domain.lifecycle import (
    TransferReceipt,
    TransferReceiptStatus,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    AssetResolutionResult,
    CurationDecision,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    FeasibilityReport,
    Identifier,
    QueryPlan,
    TransferProtocol,
)


class FakeWorkflow:
    compiler = None

    async def run(self, question, max_results=20):
        requirement = self.compiler.compile(question)
        candidate = DatasetCandidate(
            candidate_id="candidate_1",
            title="Human lung scATAC",
            identifiers=[
                Identifier(namespace="geo_series", value="GSE_TEST")
            ],
            access_level=AccessLevel.PUBLIC_VERIFIED,
        )
        return DiscoveryRunResult(
            run_id="discovery_1",
            requirement=requirement,
            query_plan=QueryPlan(requirement=requirement, queries=[]),
            source_records=[],
            candidates=[candidate],
            feasibility_reports=[
                FeasibilityReport(
                    report_id="feasibility_1",
                    candidate_id=candidate.candidate_id,
                    dimensions={},
                    decision=CurationDecision.MANUAL_REVIEW,
                )
            ],
        )


class FakeResolver:
    def __init__(self, assets):
        self.assets = assets

    async def resolve(self, candidate):
        return AssetResolutionResult(
            candidate_id=candidate.candidate_id,
            assets=self.assets,
        )


class FakeTransferExecutor:
    name = "fake"

    def __init__(self, paths):
        self.paths = paths

    async def execute(
        self,
        plan,
        bundles,
        assets,
        selected_bundle_ids,
        **kwargs,
    ):
        bundle_id = next(iter(selected_bundle_ids))
        return [
            TransferReceipt(
                receipt_id=f"receipt_{asset.asset_id}",
                plan_id=plan.plan_id,
                bundle_id=bundle_id,
                asset_id=asset.asset_id,
                source_url=asset.locations[0].url,
                local_path=str(self.paths[asset.asset_id]),
                protocol=TransferProtocol.HTTPS,
                status=TransferReceiptStatus.COMPLETED,
                bytes_written=self.paths[asset.asset_id].stat().st_size,
            )
            for asset in assets
            if asset.asset_id in {
                item.asset_id
                for bundle in bundles
                if bundle.bundle_id in selected_bundle_ids
                for item in assets
                if item.asset_id in bundle.asset_ids
            }
        ]


@pytest.mark.asyncio
async def test_supervisor_requires_download_then_corpus_approval(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "scdata_agent.application.agents.discovery.default_workflow",
        lambda database_path, use_llm=False: FakeWorkflow(),
    )
    roles = [
        DataRole.FRAGMENTS,
        DataRole.CELL_METADATA,
        DataRole.SAMPLE_METADATA,
    ]
    paths: dict[str, Path] = {}
    assets: list[DataAsset] = []
    for index, role in enumerate(roles):
        name = (
            "fragments.tsv"
            if role == DataRole.FRAGMENTS
            else f"{role.value}.tsv"
        )
        path = tmp_path / name
        path.write_text(
            "chr1\t1\t2\tcell-1\t1\n"
            if role == DataRole.FRAGMENTS
            else "id\tvalue\n1\ttest\n",
            encoding="utf-8",
        )
        asset_id = f"asset_{index}"
        paths[asset_id] = path
        assets.append(
            DataAsset(
                asset_id=asset_id,
                candidate_id="candidate_1",
                role=role,
                file_name=name,
                locations=[
                    AssetLocation(
                        url=f"https://files.test/{name}",
                        protocol=TransferProtocol.HTTPS,
                        access_level=AccessLevel.PUBLIC_VERIFIED,
                        availability=AssetAvailability.OBSERVED,
                        size_bytes=path.stat().st_size,
                    )
                ],
            )
        )
    monkeypatch.setattr(
        "scdata_agent.application.agents.progressive_asset_scout.AssetResolverRegistry",
        lambda **kwargs: FakeResolver(assets),
    )
    monkeypatch.setattr(
        "scdata_agent.application.agents.transfer_execution.select_transfer_backend",
        lambda policy: FakeTransferExecutor(paths),
    )

    database = tmp_path / "system.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run(
        "human lung scATAC-seq",
        {
            "use_llm": False,
            "data_root": str(tmp_path / "data"),
            "approved_license_candidate_ids": ["candidate_1"],
        },
    )
    supervisor = MultiAgentSupervisor(ledger, str(database))
    await supervisor.execute(run.run_id)
    assert ledger.get_run(run.run_id).status == RunStatus.WAITING_FOR_DECISION
    download = ledger.list_checkpoints(run.run_id)[0]
    assert download.kind == "download_selection"
    assert not any(
        item.kind == ArtifactKind.TRANSFER_RECEIPTS
        for item in ledger.list_artifacts(run.run_id)
    )
    artifacts = ledger.list_artifacts(run.run_id)
    assert len(
        [
            item
            for item in artifacts
            if item.kind == ArtifactKind.ASSET_INVENTORY_DELTA
        ]
    ) == 2
    assert not any(
        item.kind == ArtifactKind.EVIDENCE_REVIEW
        and item.payload.get("phase") == "fulltext"
        for item in artifacts
    )

    await supervisor.resume_checkpoint(
        download.checkpoint_id, {"option": "recommended"}
    )
    checkpoints = ledger.list_checkpoints(run.run_id)
    corpus = next(item for item in checkpoints if item.kind == "corpus_snapshot")
    assert ledger.get_run(run.run_id).status == RunStatus.WAITING_FOR_DECISION
    kinds = {item.kind for item in ledger.list_artifacts(run.run_id)}
    assert ArtifactKind.TRANSFER_RECEIPTS in kinds
    assert ArtifactKind.MATERIALIZATION_MANIFEST in kinds
    assert ArtifactKind.MATERIALIZED_QC in kinds
    assert ArtifactKind.CORPUS_SNAPSHOT in kinds

    await supervisor.resume_checkpoint(
        corpus.checkpoint_id, {"option": "approve_snapshot"}
    )
    assert ledger.get_run(run.run_id).status == RunStatus.COMPLETED
    snapshots = supervisor.catalog.list_records(
        record_type="corpus_snapshot", run_id=run.run_id
    )
    assert snapshots[-1]["payload"]["approved"] is True
    # The point of this whole branch (S4): a native fragments/cell-metadata
    # package with no AnnData/MuData object is no longer hard-quarantined for
    # its format, so it reaches "pretrain" on its actual QC and licence
    # standing. Before that fix this assertion would have failed silently
    # unnoticed -- the test above only checked that a snapshot exists.
    assert snapshots[-1]["payload"]["admitted_bundle_ids"]
