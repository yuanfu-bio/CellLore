from scdata_agent.domain.models import DataRole, Modality, PairingLevel
from scdata_agent.domain.requirements import (
    QueryPlanner,
    RuleBasedRequirementCompiler,
    restrict_modalities_to_pairing,
    roles_for_modalities,
)


def test_compile_chinese_research_question():
    question = (
        "研究人类肺癌免疫微环境的scATAC-seq和multiome数据，"
        "用于跨数据集泛化和细胞类型注释，至少5个供体"
    )
    requirement = RuleBasedRequirementCompiler().compile(question)

    assert requirement.organisms == ["Homo sapiens"]
    assert "lung" in requirement.tissues
    assert "lung cancer" in requirement.diseases
    assert Modality.SCATAC in requirement.modalities
    assert Modality.MULTIOME_RNA_ATAC in requirement.modalities
    assert requirement.minimum_donors == 5
    assert DataRole.FRAGMENTS in requirement.required_data_roles
    assert DataRole.RNA_MATRIX in requirement.required_data_roles
    assert "cross_dataset_generalization" in requirement.downstream_tasks


def test_query_plan_has_three_initial_sources():
    requirement = RuleBasedRequirementCompiler().compile("human brain scATAC-seq")
    plan = QueryPlanner().plan(requirement, max_results=7)

    assert [query.source for query in plan.queries] == [
        "europe_pmc",
        "ncbi_geo",
        "ncbi_sra",
    ]
    assert all(query.max_results == 7 for query in plan.queries)



def test_complex_chinese_requirement_preserves_executable_constraints():
    question = (
        "人类非小细胞肺癌NSCLC肺肿瘤的CD8 T细胞，优先multiome也接受scATAC，"
        "至少需要30位供体，排除细胞系和受控访问数据，保留独立研究作为泛化测试集"
    )
    requirement = RuleBasedRequirementCompiler().compile(question)

    assert "CD8 T cell" in requirement.cell_types
    assert requirement.minimum_donors == 30
    assert Modality.MULTIOME_RNA_ATAC in requirement.preferred_modalities
    assert "cell_line" in requirement.excluded_biological_sources
    assert "controlled_access" in requirement.exclusions
    assert requirement.evaluation_policy.reserve_independent_studies
    assert requirement.evaluation_policy.holdout_unit == "study"


def test_query_plan_ors_in_supplementary_terms():
    """LLM-identified technologies that no Modality alias covers yet still
    widen the search, appended alongside the deterministic modality terms."""
    requirement = RuleBasedRequirementCompiler().compile("human brain scATAC-seq")
    requirement = requirement.model_copy(
        update={"supplementary_search_terms": ["NicheAssay-seq", "FutureTech-seq"]}
    )
    query = QueryPlanner().plan(requirement).queries[0].query

    assert '"NicheAssay-seq"' in query
    assert '"FutureTech-seq"' in query
    assert "single-cell ATAC-seq" in query  # deterministic terms still present


def test_query_plan_caps_supplementary_terms_at_eight():
    requirement = RuleBasedRequirementCompiler().compile("human brain scATAC-seq")
    requirement = requirement.model_copy(
        update={"supplementary_search_terms": [f"Tech{i}" for i in range(20)]}
    )
    query = QueryPlanner().plan(requirement).queries[0].query

    assert sum(f'"Tech{i}"' in query for i in range(20)) == 8


def test_query_plan_unaffected_when_no_supplementary_terms():
    requirement = RuleBasedRequirementCompiler().compile("human brain scATAC-seq")
    assert requirement.supplementary_search_terms == []
    query = QueryPlanner().plan(requirement).queries[0].query
    assert " OR )" not in query and "OR )" not in query


class TestRolesForModalities:
    def test_protein_modality_requires_protein_matrix(self):
        """Regression: research_planner.py's own copy of this logic lacked
        the PROTEIN_MATRIX branch that this, the canonical version, has."""
        required, _ = roles_for_modalities([Modality.MULTIOME_RNA_ATAC_PROTEIN])
        assert DataRole.PROTEIN_MATRIX in required

    def test_standalone_atac_does_not_require_rna_or_protein(self):
        required, _ = roles_for_modalities([Modality.SCATAC])
        assert DataRole.RNA_MATRIX not in required
        assert DataRole.PROTEIN_MATRIX not in required


class TestRestrictModalitiesToPairing:
    def test_no_requirement_leaves_modalities_untouched(self):
        modalities = [Modality.SCATAC, Modality.MULTIOME_RNA_ATAC]
        assert restrict_modalities_to_pairing(modalities, None) == modalities

    def test_same_cell_requirement_drops_standalone_scatac(self):
        result = restrict_modalities_to_pairing(
            [Modality.SCATAC, Modality.MULTIOME_RNA_ATAC], PairingLevel.SAME_CELL
        )
        assert Modality.SCATAC not in result
        assert Modality.MULTIOME_RNA_ATAC in result

    def test_same_cell_requirement_keeps_trimodal(self):
        result = restrict_modalities_to_pairing(
            [Modality.MULTIOME_RNA_ATAC_PROTEIN], PairingLevel.SAME_CELL
        )
        assert result == [Modality.MULTIOME_RNA_ATAC_PROTEIN]

    def test_no_same_cell_modality_present_falls_back_to_both(self):
        """Nothing survives the filter, so both same-cell modalities are
        offered rather than leaving the requirement with an empty list."""
        result = restrict_modalities_to_pairing(
            [Modality.SCATAC], PairingLevel.SAME_CELL
        )
        assert set(result) == {Modality.MULTIOME_RNA_ATAC, Modality.MULTIOME_RNA_ATAC_PROTEIN}
