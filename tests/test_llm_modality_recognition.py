"""Layer recognition in the LLM-derived research-planner path.

Regression cover for a live-run defect: a question explicitly asking for
"TEA-seq/DOGMA-seq trimodal data" compiled to a role/layer set missing the
protein layer entirely, which excluded two genuinely trimodal candidates.
The cause was `_known_modality`, a narrow, independently maintained
re-implementation of the alias table the rule-based compiler already scans --
it recognised only 3 of the 8 known modalities and returned at most one match
per string. `_known_layers` now reuses the same open-vocabulary layer/
layer-combination tables used everywhere else, matching every applicable
combination.
"""

import pytest

from scdata_agent.domain.models import OmicsLayer
from scdata_agent.domain.planning import (
    DataNeed,
    ExecutionStatus,
    NeedPriority,
    PlanReadiness,
    ResearchIntent,
    ResearchPlanDraft,
)
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler, _known_layers
from scdata_agent.infrastructure.llm.research_planner import _compile_contracts


class TestKnownLayers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("TEA-seq and DOGMA-seq trimodal data", OmicsLayer.PROTEIN),
            ("ASAP-seq", OmicsLayer.PROTEIN),
            ("10x Multiome RNA+ATAC", OmicsLayer.TRANSCRIPTOME),
            ("single nucleus ATAC-seq", OmicsLayer.CHROMATIN_ACCESSIBILITY),
            ("scATAC-seq", OmicsLayer.CHROMATIN_ACCESSIBILITY),
        ],
    )
    def test_recognises_every_layer_the_alias_tables_cover(self, text, expected):
        """The old matcher recognised only 3 of these 5 -- notably never the
        trimodal case that caused the live-run regression."""
        assert expected in _known_layers(text)

    def test_a_string_may_match_more_than_one_technology(self):
        """The old function returned at most one match per string; a data
        need can legitimately name more than one technology at once -- the
        union of layers must reflect all of them."""
        layers = _known_layers("scATAC-seq or RNA+ATAC multiome")
        assert OmicsLayer.CHROMATIN_ACCESSIBILITY in layers
        assert OmicsLayer.TRANSCRIPTOME in layers

    def test_unrecognisable_text_returns_nothing(self):
        assert _known_layers("CRISPR screen perturbation atlas") == []


def _draft(question: str, data_needs: list[DataNeed]) -> ResearchPlanDraft:
    return ResearchPlanDraft(
        intent=ResearchIntent(
            original_request=question, intent_summary=question, primary_question=question
        ),
        data_needs=data_needs,
        readiness=PlanReadiness.CONDITIONAL,
        readiness_rationale="test fixture",
    )


def _need(modalities: list[str], data_type: str = "single-cell data") -> DataNeed:
    return DataNeed(
        need_id=f"need_{'_'.join(modalities) or 'x'}",
        name="need",
        data_type=data_type,
        role="discovery",
        priority=NeedPriority.ESSENTIAL,
        rationale="x",
        modalities=modalities,
        execution_status=ExecutionStatus.SUPPORTED,
    )


class TestCompileContracts:
    def test_trimodal_data_need_survives_into_the_contract(self):
        """The exact live-run regression: TEA-seq/DOGMA-seq's protein layer
        must appear in required_omics_layers, not be silently dropped."""
        question = "TEA-seq and DOGMA-seq trimodal single-cell data (human)"
        draft = _draft(question, [_need(["TEA-seq", "DOGMA-seq"], "trimodal data")])
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert OmicsLayer.PROTEIN in contract.required_omics_layers

    def test_organism_is_untouched_by_the_llm_merge(self):
        """organisms/tissues/diseases/cell_types come entirely from the
        rule-based baseline in this path; the LLM draft never overrides them,
        so a species requirement cannot be dropped by this merge step."""
        question = "human paired multiome data"
        draft = _draft(question, [_need(["10x Multiome"])])
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert contract.organisms == ["Homo sapiens"]

    def test_pairing_requirement_survives_the_llm_merge(self):
        """A same-cell requirement detected by the rule-based baseline must
        survive into the merged contract even though the LLM's own data needs
        say nothing about pairing themselves."""
        question = "配对的单细胞多组学数据（human）：同细胞同时测量ATAC和RNA"
        draft = _draft(
            question,
            [_need(["scATAC-seq"]), _need(["10x Multiome"])],
        )
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert contract.pairing_requirement is not None
        assert OmicsLayer.CHROMATIN_ACCESSIBILITY in contract.required_omics_layers
        assert OmicsLayer.TRANSCRIPTOME in contract.required_omics_layers

    def test_unclassifiable_technology_becomes_a_supplementary_term(self):
        question = "single-cell data using a novel co-assay technology"
        draft = _draft(
            question,
            [_need(["10x Multiome", "NicheAssay-seq (novel protocol)"])],
        )
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert "NicheAssay-seq (novel protocol)" in contract.supplementary_search_terms

    def test_supplementary_terms_are_not_harvested_from_full_sentences(self):
        """Only the short `modalities` list is harvested; `data_type` is a
        full sentence and would inject a low-precision phrase if quoted whole."""
        question = "single-cell data"
        draft = _draft(
            question,
            [_need(
                ["10x Multiome"],
                data_type="A long descriptive sentence about the study design.",
            )],
        )
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert not any(
            "long descriptive sentence" in term.casefold()
            for term in contract.supplementary_search_terms
        )

    def test_protein_matrix_role_is_required_for_trimodal(self):
        """Regression: the old research_planner.py role derivation lacked
        the PROTEIN_MATRIX branch that the shared roles_for_layers has."""
        from scdata_agent.domain.models import DataRole

        question = "TEA-seq trimodal data"
        draft = _draft(question, [_need(["TEA-seq"])])
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert DataRole.PROTEIN_MATRIX in contract.required_data_roles
