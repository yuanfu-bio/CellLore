"""InventoryReconcileAgent judges each candidate by its own modality.

End-to-end cover for the live-run defect: a research question naming both
scATAC and multiome produced a unioned required-role list that was applied to
every candidate, so standalone scATAC depositions were reported incomplete for
lacking an RNA matrix they were never expected to have.
"""

import pytest

from scdata_agent.application.agents.inventory_reconcile import (
    InventoryReconcileAgent,
)
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import (
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    Modality,
    QueryPlan,
    ResearchRequirement,
    TransferProtocol,
)
from scdata_agent.domain.resource_pipeline import AssetInventoryDelta, ScoutPhase


def _asset(candidate_id: str, role: DataRole, file_name: str) -> DataAsset:
    return DataAsset(
        asset_id=f"asset_{candidate_id}_{role.value}",
        candidate_id=candidate_id,
        role=role,
        file_name=file_name,
        locations=[
            AssetLocation(
                url=f"https://example.test/{file_name}",
                protocol=TransferProtocol.HTTPS,
                access_level=AccessLevel.PUBLIC_VERIFIED,
                availability=AssetAvailability.OBSERVED,
                size_bytes=1024,
            )
        ],
    )


def _candidate(candidate_id: str, modalities: list[Modality]) -> DatasetCandidate:
    return DatasetCandidate(
        candidate_id=candidate_id,
        title=f"Study {candidate_id}",
        modalities=modalities,
    )


async def _reconcile(tmp_path, candidates, assets):
    """Run the agent over a fixed inventory and return bundles by candidate."""
    # The question names both modalities, which is what unioned the role list.
    requirement = ResearchRequirement(
        question="scATAC-seq and multiome for representation learning",
        modalities=[Modality.SCATAC, Modality.MULTIOME_RNA_ATAC],
        acceptable_modalities=[Modality.SCATAC, Modality.MULTIOME_RNA_ATAC],
        required_data_roles=[
            DataRole.BARCODE_LINKAGE,
            DataRole.CELL_METADATA,
            DataRole.FRAGMENTS,
            DataRole.RNA_MATRIX,
            DataRole.SAMPLE_METADATA,
        ],
    )
    discovery = DiscoveryRunResult(
        run_id="run_1",
        requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        candidates=candidates,
        source_records=[],
        feasibility_reports=[],
    )
    database = tmp_path / "reconcile.sqlite"
    context = AgentContext(
        run_id="run_1",
        question=requirement.question,
        database_path=str(database),
        ledger=CollaborationLedger(database),
        policy={"use_llm": False, "remote_probe_enabled": False},
        requirement=requirement,
        discovery=discovery,
    )
    context.artifacts.append(
        artifact(
            "run_1",
            ArtifactKind.ASSET_INVENTORY_DELTA,
            InventoryReconcileAgent.name,
            AssetInventoryDelta(
                inventory_id="delta_1",
                run_id="run_1",
                phase=ScoutPhase.DIRECT,
                assets=assets,
            ).model_dump(mode="json"),
        )
    )
    outputs = await InventoryReconcileAgent().run(context)
    payload = outputs[0].payload
    return {item["candidate_id"]: item for item in payload["bundles"]}


@pytest.mark.asyncio
async def test_standalone_scatac_is_not_judged_against_multiome(tmp_path):
    candidate = _candidate("ds_scatac", [Modality.SCATAC])
    bundles = await _reconcile(
        tmp_path,
        [candidate],
        [
            _asset("ds_scatac", DataRole.FRAGMENTS, "atac_fragments.tsv.gz"),
            _asset("ds_scatac", DataRole.CELL_METADATA, "barcodes.tsv.gz"),
        ],
    )
    bundle = bundles["ds_scatac"]

    assert "scatac_standalone" in bundle["satisfied_schema_ids"]
    # The multiome schema is not even applicable to a scATAC-only candidate.
    assert "multiome_same_cell" not in bundle["applicable_schema_ids"]
    assert DataRole.RNA_MATRIX not in bundle["missing_roles"]
    assert DataRole.BARCODE_LINKAGE not in bundle["missing_roles"]


@pytest.mark.asyncio
async def test_multiome_candidate_still_needs_rna(tmp_path):
    candidate = _candidate("ds_multiome", [Modality.MULTIOME_RNA_ATAC])
    bundles = await _reconcile(
        tmp_path,
        [candidate],
        [
            _asset("ds_multiome", DataRole.FRAGMENTS, "atac_fragments.tsv.gz"),
            _asset("ds_multiome", DataRole.CELL_METADATA, "barcodes.tsv.gz"),
        ],
    )
    bundle = bundles["ds_multiome"]

    assert bundle["satisfied_schema_ids"] == []
    assert "multiome_same_cell" in bundle["applicable_schema_ids"]


@pytest.mark.asyncio
async def test_complete_multiome_bundle_is_satisfied(tmp_path):
    candidate = _candidate("ds_multiome", [Modality.MULTIOME_RNA_ATAC])
    bundles = await _reconcile(
        tmp_path,
        [candidate],
        [
            _asset("ds_multiome", DataRole.FRAGMENTS, "atac_fragments.tsv.gz"),
            _asset("ds_multiome", DataRole.RNA_MATRIX, "filtered_feature_bc_matrix.h5"),
            _asset("ds_multiome", DataRole.CELL_METADATA, "barcodes.tsv.gz"),
        ],
    )
    assert "multiome_same_cell" in bundles["ds_multiome"]["satisfied_schema_ids"]


@pytest.mark.asyncio
async def test_fastq_only_bundle_satisfies_the_raw_reads_schema(tmp_path):
    candidate = _candidate("ds_raw", [Modality.SCATAC])
    bundles = await _reconcile(
        tmp_path,
        [candidate],
        [_asset("ds_raw", DataRole.RAW_READS, "SRR1_1.fastq.gz")],
    )
    assert bundles["ds_raw"]["satisfied_schema_ids"] == ["raw_reads"]
