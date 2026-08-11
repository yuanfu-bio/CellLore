from datetime import UTC, datetime
from unittest.mock import MagicMock

from celllore.application.agents.shared import AgentContext
from celllore.application.agents.transfer_execution import TransferExecutionAgent
from celllore.application.collaboration import AgentName, ArtifactKind, artifact
from celllore.application.dataset_layout import (
    production_run_root,
    raw_stage_for_role,
    readable_pair_unit_id,
    study_accession,
)
from celllore.domain.lifecycle import SemanticBundle
from celllore.domain.models import (
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    Identifier,
    QueryPlan,
    ResearchRequirement,
)
from celllore.domain.pair_units import ProposedPairUnit


def test_production_root_is_isolated_by_date_and_run_id():
    root = production_run_root(
        "/datasets",
        "agent_run_ed26f2d3daf026d0",
        datetime(2026, 8, 11, 6, 58, tzinfo=UTC),
    )

    assert str(root) == (
        "/datasets/production/"
        "2026-08-11_RUN_agent_run_ed26f2d3daf026d0"
    )


def test_reference_style_pair_unit_id_hides_internal_hash():
    assets = [
        DataAsset(
            asset_id="atac", candidate_id="candidate_1", role=DataRole.FRAGMENTS,
            file_name="GSM9196533_atac_fragments.tsv.gz",
        ),
        DataAsset(
            asset_id="rna", candidate_id="candidate_1", role=DataRole.RNA_MATRIX,
            file_name="GSM9196532_rna_counts.h5",
        ),
    ]
    bundle = SemanticBundle(
        bundle_id="bundle_b162", candidate_id="candidate_1",
        pair_unit_id="pair_unit_b162e3990472429d", bundle_type="multiome",
        asset_ids=["atac", "rna"],
    )
    unit = ProposedPairUnit(
        proposed_unit_id="pair_unit_b162e3990472429d", candidate_id="candidate_1",
        member_assets=["atac", "rna"], member_samples=["GSM9196533", "GSM9196532"],
        sample_interpretation="Patient3 + shared 10x barcode", is_resolved=True,
    )

    display = readable_pair_unit_id(bundle, unit, assets)

    assert display == "Patient3_GSM9196533__GSM9196532"
    assert "b162" not in display


def test_production_tree_uses_study_and_semantic_role_directories():
    candidate = DatasetCandidate(
        candidate_id="candidate_1", title="PBMC",
        identifiers=[Identifier(namespace="geo_series", value="GSE158013")],
    )

    assert study_accession(candidate) == "GSE158013"
    assert raw_stage_for_role(DataRole.CELL_METADATA) == "01_metadata"
    assert raw_stage_for_role(DataRole.RNA_MATRIX) == "02_rna_raw_counts"
    assert raw_stage_for_role(DataRole.FRAGMENTS) == "03_atac_fragments"
    assert raw_stage_for_role(DataRole.RAW_READS) == "99_raw_optional"


def test_batch_label_is_preferred_over_generic_assay_text():
    bundle = SemanticBundle(
        bundle_id="bundle_1", candidate_id="candidate_1",
        pair_unit_id="pair_unit_1", bundle_type="multiome", asset_ids=["atac", "rna"],
    )
    assets = [
        DataAsset(
            asset_id="atac", candidate_id="candidate_1", role=DataRole.FRAGMENTS,
            file_name="GSM2_atac.tsv.gz",
        ),
        DataAsset(
            asset_id="rna", candidate_id="candidate_1", role=DataRole.RNA_MATRIX,
            file_name="GSM1_rna.h5",
        ),
    ]
    unit = ProposedPairUnit(
        proposed_unit_id="pair_unit_1", candidate_id="candidate_1",
        sample_interpretation="GEX and ATAC libraries from CAR-T cells batch E",
        donor_interpretation="Likely a single donor; batch E is a unique identifier",
        member_samples=["GSM1", "GSM2"], is_resolved=True,
    )

    assert readable_pair_unit_id(bundle, unit, assets) == "BatchE_GSM2__GSM1"


def test_reference_multiome_name_uses_library_material_and_well():
    bundle = SemanticBundle(
        bundle_id="bundle_1", candidate_id="candidate_1",
        pair_unit_id="pair_unit_1", bundle_type="multiome", asset_ids=["rna"],
    )
    assets = [DataAsset(
        asset_id="rna", candidate_id="candidate_1", role=DataRole.RNA_MATRIX,
        file_name=(
            "GSM5123949_X066-MP0C1W1_leukopak_nuclei_multiome_200M_"
            "cellranger-arc_filtered_feature_bc_matrix.h5"
        ),
    )]
    unit = ProposedPairUnit(
        proposed_unit_id="pair_unit_1", candidate_id="candidate_1",
        sample_interpretation=(
            "10x Multiome library (RNA + ATAC) from leukapheresis-purified nuclei"
        ),
        donor_interpretation="HMN200910", library_interpretation="10x Multiome",
        member_samples=["GSM5123949"], is_resolved=True,
    )

    assert readable_pair_unit_id(bundle, unit, assets) == (
        "Multiome_nuclei_W1_GSM5123949"
    )


def test_discovered_run_layout_matches_production_tree():
    requirement = ResearchRequirement(question="paired PBMC RNA and ATAC")
    candidate = DatasetCandidate(
        candidate_id="candidate_1", title="PBMC",
        identifiers=[Identifier(namespace="geo_series", value="GSE158013")],
    )
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        candidates=[candidate], source_records=[], feasibility_reports=[],
    )
    assets = [
        DataAsset(
            asset_id="atac", candidate_id="candidate_1", role=DataRole.FRAGMENTS,
            file_name="GSM9196533_fragments.tsv.gz",
        ),
        DataAsset(
            asset_id="rna", candidate_id="candidate_1", role=DataRole.RNA_MATRIX,
            file_name="GSM9196532_counts.h5",
        ),
    ]
    bundle = SemanticBundle(
        bundle_id="bundle_1", candidate_id="candidate_1",
        pair_unit_id="pair_unit_internal", bundle_type="multiome",
        asset_ids=["atac", "rna"],
    )
    unit = ProposedPairUnit(
        proposed_unit_id="pair_unit_internal", candidate_id="candidate_1",
        member_assets=["atac", "rna"], member_samples=["GSM9196533", "GSM9196532"],
        sample_interpretation="Patient3 + shared 10x barcode", is_resolved=True,
    )
    context = AgentContext(
        run_id="run_1", question=requirement.question, database_path=":memory:",
        ledger=MagicMock(), artifacts=[
            artifact(
                "run_1", ArtifactKind.DISCOVERY_RESULT, AgentName.RESOURCE_DISCOVERY,
                discovery.model_dump(mode="json"),
            ),
            artifact(
                "run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT,
                {
                    "assets": [item.model_dump(mode="json") for item in assets],
                    "bundles": [bundle.model_dump(mode="json")],
                    "proposed_pair_units": [unit.model_dump(mode="json")],
                },
            ),
        ],
    )

    layout = TransferExecutionAgent._layout(context)

    assert layout == {
        "atac": (
            "GSE158013/Patient3_GSM9196533__GSM9196532/"
            "03_atac_fragments/GSM9196533_fragments.tsv.gz"
        ),
        "rna": (
            "GSE158013/Patient3_GSM9196533__GSM9196532/"
            "02_rna_raw_counts/GSM9196532_counts.h5"
        ),
    }
