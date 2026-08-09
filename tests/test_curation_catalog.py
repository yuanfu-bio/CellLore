"""The catalog must retain incomplete GEO discoveries as actionable holds."""

from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.curation_catalog import build_curation_catalog
from scdata_agent.domain.models import (
    DatasetCandidate,
    DiscoveryRunResult,
    Identifier,
    QueryPlan,
    ResearchRequirement,
)


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
