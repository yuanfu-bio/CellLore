"""Modality recognition in the LLM-derived research-planner path.

Regression cover for a live-run defect: a question explicitly asking for
"TEA-seq/DOGMA-seq trimodal data" compiled to acceptable_modalities missing
trimodal entirely, which excluded two genuinely trimodal candidates as
modality:mismatch. The cause was `_known_modality`, a narrow, independently
maintained re-implementation of the alias table the rule-based compiler
already scans -- it recognised only 3 of the 8 known modalities and returned
at most one match per string. `_known_modalities` now reuses the same table
used everywhere else, matching every applicable modality.
"""

import pytest

from scdata_agent.domain.models import Modality
from scdata_agent.domain.planning import (
    DataNeed,
    ExecutionStatus,
    NeedPriority,
    PlanReadiness,
    ResearchIntent,
    ResearchPlanDraft,
)
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler
from scdata_agent.infrastructure.llm.research_planner import (
    _compile_contracts,
    _known_modalities,
)


class TestKnownModalities:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("TEA-seq and DOGMA-seq trimodal data", Modality.MULTIOME_RNA_ATAC_PROTEIN),
            ("ASAP-seq", Modality.MULTIOME_ATAC_PROTEIN),
            ("10x Multiome RNA+ATAC", Modality.MULTIOME_RNA_ATAC),
            ("single nucleus ATAC-seq", Modality.SNATAC),
            ("scATAC-seq", Modality.SCATAC),
        ],
    )
    def test_recognises_every_modality_the_alias_table_covers(self, text, expected):
        """The old matcher recognised only 3 of these 5 -- notably never the
        trimodal case that caused the live-run regression."""
        assert expected in _known_modalities(text)

    def test_a_string_may_match_more_than_one_modality(self):
        """The old function returned at most one match per string; a data
        need can legitimately name more than one modality at once."""
        matches = _known_modalities("scATAC-seq or RNA+ATAC multiome")
        assert Modality.SCATAC in matches
        assert Modality.MULTIOME_RNA_ATAC in matches

    def test_unrecognisable_text_returns_nothing(self):
        assert _known_modalities("CRISPR screen perturbation atlas") == []


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
        """The exact live-run regression: TEA-seq/DOGMA-seq must appear in
        acceptable_modalities, not be silently dropped."""
        question = "TEA-seq and DOGMA-seq trimodal single-cell data (human)"
        draft = _draft(question, [_need(["TEA-seq", "DOGMA-seq"], "trimodal data")])
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert Modality.MULTIOME_RNA_ATAC_PROTEIN in contract.acceptable_modalities

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
        """A same-cell requirement narrows the LLM's own modality list too,
        not only the rule-based baseline's."""
        question = "配对的单细胞多组学数据（human）：同细胞同时测量ATAC和RNA"
        draft = _draft(
            question,
            [_need(["scATAC-seq"]), _need(["10x Multiome"])],
        )
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert Modality.SCATAC not in contract.acceptable_modalities
        assert Modality.MULTIOME_RNA_ATAC in contract.acceptable_modalities

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
        the PROTEIN_MATRIX branch that the shared roles_for_modalities has."""
        from scdata_agent.domain.models import DataRole

        question = "TEA-seq trimodal data"
        draft = _draft(question, [_need(["TEA-seq"])])
        contract = _compile_contracts(
            question, draft, RuleBasedRequirementCompiler()
        )[0]

        assert DataRole.PROTEIN_MATRIX in contract.required_data_roles
