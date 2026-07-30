from scdata_agent.domain.models import DataRole, Modality
from scdata_agent.domain.requirements import QueryPlanner, RuleBasedRequirementCompiler


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
