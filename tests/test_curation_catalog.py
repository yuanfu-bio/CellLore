"""The catalog must retain incomplete GEO discoveries as actionable holds."""

from openpyxl import load_workbook

from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.curation_catalog import (
    CurationCatalog,
    build_curation_catalog,
    write_curation_catalog_xlsx,
)
from scdata_agent.domain.lifecycle import SemanticBundle
from scdata_agent.domain.models import (
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    Identifier,
    OmicsLayer,
    QueryPlan,
    ResearchRequirement,
    SourceRecord,
)
from scdata_agent.domain.pair_units import ProposedPairUnit


def test_discovered_study_without_pair_unit_is_retained_as_a_hold():
    requirement = ResearchRequirement(
        question="human same-cell RNA and ATAC multiome",
        profile_id="paired_atac_rna_human",
    )
    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="GEO study with unresolved sample manifest",
        identifiers=[Identifier(namespace="geo_series", value="GSE123456")],
    )
    discovery = DiscoveryRunResult(
        run_id="run_1",
        requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        candidates=[candidate],
        source_records=[],
        feasibility_reports=[],
    )

    catalog = build_curation_catalog(
        "run_1",
        [
            artifact(
                "run_1",
                ArtifactKind.DISCOVERY_RESULT,
                AgentName.RESOURCE_DISCOVERY,
                discovery.model_dump(mode="json"),
            )
        ],
    )

    assert len(catalog.studies) == 1
    study = catalog.studies[0]
    assert study["study_accession"] == "GSE123456"
    assert study["catalog_status"] == "HOLD"
    assert "No pair unit" in study["catalog_status_detail"]
    assert len(catalog.audit_hold) == 1
    audit = catalog.audit_hold[0]
    assert audit["blocking_category"] == "pair_unit:unresolved"
    assert audit["audit_method"]
    assert audit["pass_criteria"]


def test_exported_catalog_urls_are_real_clickable_hyperlinks(tmp_path):
    output = tmp_path / "catalog.xlsx"
    catalog = CurationCatalog(
        studies=[{
            "study_accession": "GSE123456",
            "source_url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE123456",
        }],
        files=[{
            "filename": "matrix.mtx.gz",
            "download_url": "https://example.test/matrix.mtx.gz",
        }],
    )

    write_curation_catalog_xlsx(catalog, str(output))

    workbook = load_workbook(output)
    study_link = workbook["01_Studies"]["B2"]
    file_link = workbook["03_Files"]["B2"]
    assert study_link.hyperlink.target == study_link.value
    assert file_link.hyperlink.target == file_link.value
    assert study_link.style == "Hyperlink"
    assert file_link.style == "Hyperlink"


def test_catalog_only_lists_bundles_covering_required_omics_and_full_gsm_url():
    requirement = ResearchRequirement(
        question="paired RNA and ATAC",
        required_omics_layers=[
            OmicsLayer.CHROMATIN_ACCESSIBILITY,
            OmicsLayer.TRANSCRIPTOME,
        ],
    )
    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="PBMC multiome",
        identifiers=[Identifier(namespace="geo_series", value="GSE1")],
        source_record_ids=["source_1"],
    )
    source = SourceRecord(
        record_id="source_1", source_name="ncbi_geo", external_id="GSE1",
        title="PBMC multiome", raw_metadata={"pdat": "2024/06/12"},
    )
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        candidates=[candidate], source_records=[source], feasibility_reports=[],
    )
    assets = [
        DataAsset(
            asset_id="atac", candidate_id="candidate_1",
            role=DataRole.FRAGMENTS, file_name="GSM9729518_fragments.tsv.gz",
        ),
        DataAsset(
            asset_id="rna", candidate_id="candidate_1",
            role=DataRole.RNA_MATRIX, file_name="GSM9729517_RNA_counts.h5",
        ),
    ]
    bundles = [
        SemanticBundle(
            bundle_id="paired", candidate_id="candidate_1",
            pair_unit_id="paired", bundle_type="multiome",
            asset_ids=["atac", "rna"],
        ),
        SemanticBundle(
            bundle_id="atac_only", candidate_id="candidate_1",
            pair_unit_id="atac_only", bundle_type="single",
            asset_ids=["atac"],
        ),
    ]

    catalog = build_curation_catalog(
        "run_1",
        [
            artifact(
                "run_1", ArtifactKind.DISCOVERY_RESULT,
                AgentName.RESOURCE_DISCOVERY, discovery.model_dump(mode="json"),
            ),
            artifact(
                "run_1", ArtifactKind.ASSET_INVENTORY,
                AgentName.ASSET_SCOUT,
                {
                    "assets": [item.model_dump(mode="json") for item in assets],
                    "bundles": [item.model_dump(mode="json") for item in bundles],
                    "proposed_pair_units": [ProposedPairUnit(
                        proposed_unit_id="paired", candidate_id="candidate_1",
                        member_assets=["atac", "rna"],
                        member_samples=["GSM9729518", "GSM9729517"],
                        sample_interpretation="Patient3 + shared 10x barcode",
                        is_resolved=True,
                    ).model_dump(mode="json")],
                },
            ),
        ],
    )

    assert [item["pair_unit_id"] for item in catalog.pair_units] == [
        "Patient3_GSM9729518__GSM9729517"
    ]
    assert {item["pair_unit_id"] for item in catalog.files} == {
        "Patient3_GSM9729518__GSM9729517"
    }
    assert catalog.studies[0]["public_year"] == 2024
    assert catalog.studies[0]["evidence_url"].endswith("acc=GSM9729518")
