from celllore.domain.models import DataRole, OmicsLayer, PairingLevel
from celllore.domain.requirements import (
    QueryPlanner,
    RuleBasedRequirementCompiler,
    applicable_bundle_schemas_by_layers,
    roles_for_layers,
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
    assert OmicsLayer.CHROMATIN_ACCESSIBILITY in requirement.required_omics_layers
    assert OmicsLayer.TRANSCRIPTOME in requirement.required_omics_layers
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
    assert "cell_line" in requirement.excluded_biological_sources
    assert "controlled_access" in requirement.exclusions
    assert requirement.evaluation_policy.reserve_independent_studies
    assert requirement.evaluation_policy.holdout_unit == "study"


def test_query_plan_ors_in_supplementary_terms():
    """LLM-identified technologies that no layer/combination alias covers yet
    still widen the search, appended alongside the deterministic terms."""
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


class TestNoSilentLayerFallback:
    """The CITE-seq defect: a question naming only a layer this table has no
    named-technology/combination alias for used to silently default to an
    ATAC-scoped guess, which is why a CITE-seq search recalled mostly ATAC
    studies. The fallback is gone; `required_omics_layers` is the
    open-vocabulary replacement, and it must never invent a chromatin-
    accessibility layer nobody asked for."""

    def test_unmatched_technology_does_not_guess_a_chromatin_layer(self):
        requirement = RuleBasedRequirementCompiler().compile(
            "sepsis CITE-seq surface protein and gene expression profiling"
        )
        assert OmicsLayer.CHROMATIN_ACCESSIBILITY not in requirement.required_omics_layers

    def test_unmatched_technology_still_surfaces_its_layers(self):
        requirement = RuleBasedRequirementCompiler().compile(
            "sepsis CITE-seq surface protein and gene expression profiling"
        )
        assert OmicsLayer.PROTEIN in requirement.required_omics_layers

    def test_known_technology_question_is_unaffected(self):
        requirement = RuleBasedRequirementCompiler().compile("human brain scATAC-seq")
        assert OmicsLayer.CHROMATIN_ACCESSIBILITY in requirement.required_omics_layers


class TestSingleCellRequirement:
    def test_single_cell_phrasing_sets_the_requirement(self):
        requirement = RuleBasedRequirementCompiler().compile("single-cell ATAC-seq atlas")
        assert requirement.single_cell_requirement is True

    def test_bulk_only_phrasing_sets_it_false(self):
        requirement = RuleBasedRequirementCompiler().compile("bulk RNA-seq cohort")
        assert requirement.single_cell_requirement is False

    def test_neither_phrasing_leaves_it_unset(self):
        requirement = RuleBasedRequirementCompiler().compile("human lung scATAC-seq")
        assert requirement.single_cell_requirement is None


class TestApplicableBundleSchemasByLayers:
    """The layer/relationship-driven successor to `applicable_bundle_schemas`.

    Modality used to encode "which layers" and "what relationship" as one
    combinatorial label -- multiome_same_cell and matched_scrna_scatac have
    the *identical* {ATAC, RNA} layer set and only differ in relationship, so
    exact layer match alone cannot disambiguate them; `required_relationship`
    is what does.
    """

    def test_same_cell_relationship_selects_the_multiome_schema_only(self):
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME},
                PairingLevel.SAME_CELL,
            )
        }
        assert "multiome_same_cell" in ids
        assert "matched_scrna_scatac" not in ids

    def test_same_sample_relationship_selects_the_matched_schema_only(self):
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME},
                PairingLevel.SAME_SAMPLE,
            )
        }
        assert "matched_scrna_scatac" in ids
        assert "multiome_same_cell" not in ids

    def test_ambiguous_relationship_selects_neither_paired_schema(self):
        """Study-level co-reporting (no stated pairing) must not silently
        satisfy either a same-cell or a matched-samples claim it never made."""
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME},
                PairingLevel.STUDY_LEVEL,
            )
        }
        assert "multiome_same_cell" not in ids
        assert "matched_scrna_scatac" not in ids
        assert "raw_reads" in ids  # the only schema with no layer/relationship gate

    def test_atac_only_layer_selects_standalone_schema_not_multiome(self):
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {OmicsLayer.CHROMATIN_ACCESSIBILITY}, PairingLevel.NOT_APPLICABLE
            )
        }
        assert "scatac_standalone" in ids
        assert "multiome_same_cell" not in ids

    def test_atac_and_rna_layers_do_not_offer_the_standalone_atac_schema(self):
        """A candidate that actually has both layers must not be judged
        against the standalone-ATAC shape, even if pairing is unresolved."""
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME}, None
            )
        }
        assert "scatac_standalone" not in ids

    def test_no_observed_layers_offers_every_schema(self):
        assert len(applicable_bundle_schemas_by_layers(set(), None)) == 6

    def test_unmatched_layer_combination_offers_only_the_layer_agnostic_schema(self):
        """A layer combination no schema declares (e.g. methylation alone)
        must not crash or silently match an unrelated shape."""
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {OmicsLayer.DNA_METHYLATION}, None
            )
        }
        assert ids == {"raw_reads"}

    def test_relationship_gated_schemas_match_by_subset_extra_layers_allowed(self):
        """A same-cell candidate with an extra, unrelated layer (e.g. a novel
        RNA+ATAC+methylation co-assay no named technology or trimodal schema
        declares) must still satisfy multiome_same_cell on its RNA+ATAC
        subset. Rejecting it for the extra layer would silently reintroduce
        closed-combination enumeration one level down, over layer
        combinations instead of over Modality -- exactly what this system
        exists to avoid."""
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {
                    OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME,
                    OmicsLayer.DNA_METHYLATION,
                },
                PairingLevel.SAME_CELL,
            )
        }
        assert "multiome_same_cell" in ids

    def test_dogma_seq_shaped_candidate_matches_both_trimodal_and_same_cell(self):
        """A DOGMA-seq/TEA-seq candidate (ATAC+RNA+protein, same-cell) proves
        the RNA+ATAC same-cell relationship regardless of the extra protein
        layer, so it should be usable as either bundle shape."""
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {
                    OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME,
                    OmicsLayer.PROTEIN,
                },
                PairingLevel.SAME_CELL,
            )
        }
        assert {"multiome_same_cell", "multiome_trimodal"}.issubset(ids)

    def test_ungated_schemas_still_require_an_exact_match(self):
        """scatac_standalone has no relationship gate, so subset matching
        would let it silently absorb a richer candidate (e.g. a declared
        multiome study) as if it were merely standalone ATAC. It must only
        match when ATAC is the *entire* observed layer set."""
        ids = {
            schema.schema_id
            for schema in applicable_bundle_schemas_by_layers(
                {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME},
                PairingLevel.SAME_CELL,
            )
        }
        assert "scatac_standalone" not in ids


class TestRolesForLayers:
    def test_protein_layer_combination_requires_protein_matrix(self):
        """Regression: research_planner.py's own copy of this logic lacked
        the PROTEIN_MATRIX branch that this, the canonical version, has."""
        required, _ = roles_for_layers(
            [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN]
        )
        assert DataRole.PROTEIN_MATRIX in required

    def test_standalone_atac_does_not_require_rna_or_protein(self):
        required, _ = roles_for_layers([OmicsLayer.CHROMATIN_ACCESSIBILITY])
        assert DataRole.RNA_MATRIX not in required
        assert DataRole.PROTEIN_MATRIX not in required
