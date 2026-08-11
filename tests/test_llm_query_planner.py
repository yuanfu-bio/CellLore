"""LLMQueryPlanner: round-0 search queries authored by the model, not a
fixed technology-term table. See its own module docstring for why this
exists -- GSE228428's real GEO record never uses any of MODALITY_QUERY_TERMS/
SAME_CELL_QUERY_TERMS's named technologies.
"""

import json

import pytest

from celllore.domain.models import (
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
    ResearchRequirement,
)
from celllore.domain.requirements import QueryPlanner
from celllore.infrastructure.llm.query_planner import (
    LLMQueryPlanner,
    _sanitize_terms,
    configured_query_planner,
)


class FakeClient:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[list[dict]] = []

    async def complete(self, messages, *, temperature=0.1, max_tokens=2_000):
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _requirement(**overrides) -> ResearchRequirement:
    defaults = {
        "question": "paired same-cell multiome RNA and ATAC data, human endothelial cells",
        "profile_id": "paired_atac_rna_human",
        "organisms": ["Homo sapiens"],
        "cell_types": ["endothelial cell"],
        "required_omics_layers": [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
        "pairing_requirement": PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        ),
    }
    defaults.update(overrides)
    return ResearchRequirement(**defaults)


class TestSanitizeTerms:
    def test_drops_empty_and_overlong_terms(self):
        assert _sanitize_terms(["", "   ", "x" * 90, "10x Multiome"]) == ["10x Multiome"]

    def test_strips_control_and_quote_characters(self):
        assert _sanitize_terms(['10x "Multiome"\n']) == ["10x Multiome"]

    def test_deduplicates_case_insensitively(self):
        assert _sanitize_terms(["Multiome", "multiome", "MULTIOME"]) == ["Multiome"]

    def test_bounds_term_count(self):
        terms = [f"term-{i}" for i in range(20)]
        assert len(_sanitize_terms(terms)) == 12


class TestLLMQueryPlanner:
    @pytest.mark.asyncio
    async def test_llm_authored_terms_drive_the_technology_clause(self):
        client = FakeClient(
            json.dumps({"terms": ["10x Genomics Multiome", "multiome", "multiomic"]})
        )
        requirement = _requirement()
        plan = await LLMQueryPlanner(client).plan(requirement, max_results=15)
        geo_query = next(q.query for q in plan.queries if q.source == "ncbi_geo")
        assert '"10x Genomics Multiome"' in geo_query
        assert '"multiome"' in geo_query
        assert '"multiomic"' in geo_query
        # The deterministic part -- organism/biological AND filter -- must
        # still be present; the LLM only supplies the technology clause.
        assert '"Homo sapiens"[Organism]' in geo_query
        assert '"endothelial cell"' in geo_query

    @pytest.mark.asyncio
    async def test_llm_call_failure_falls_back_to_the_deterministic_tables(self):
        requirement = _requirement()
        deterministic_plan = QueryPlanner().plan(requirement, max_results=15)
        client = FakeClient(RuntimeError("boom"))
        llm_plan = await LLMQueryPlanner(client).plan(requirement, max_results=15)
        assert llm_plan.queries == deterministic_plan.queries

    @pytest.mark.asyncio
    async def test_empty_llm_terms_falls_back_to_the_deterministic_tables(self):
        requirement = _requirement()
        deterministic_plan = QueryPlanner().plan(requirement, max_results=15)
        client = FakeClient(json.dumps({"terms": []}))
        llm_plan = await LLMQueryPlanner(client).plan(requirement, max_results=15)
        assert llm_plan.queries == deterministic_plan.queries

    @pytest.mark.asyncio
    async def test_seed_vocabulary_from_the_resolved_profile_reaches_the_prompt(self):
        client = FakeClient(json.dumps({"terms": ["multiome"]}))
        await LLMQueryPlanner(client).plan(_requirement(), max_results=15)
        user_payload = json.loads(client.calls[0][1]["content"])["input"]
        assert "10x Multiome" in " ".join(user_payload["seed_technology_vocabulary"]) or any(
            "multiome" in term.casefold()
            for term in user_payload["seed_technology_vocabulary"]
        )

    @pytest.mark.asyncio
    async def test_profile_rejects_single_layer_llm_query_terms(self):
        client = FakeClient(json.dumps({"terms": ["ATAC", "10x Multiome"]}))
        plan = await LLMQueryPlanner(client).plan(_requirement(), max_results=15)
        geo_query = next(q.query for q in plan.queries if q.source == "ncbi_geo")
        assert '"10x Multiome"' in geo_query
        assert '"ATAC"' not in geo_query

    @pytest.mark.asyncio
    async def test_unresolvable_profile_id_does_not_crash(self):
        client = FakeClient(json.dumps({"terms": ["multiome"]}))
        requirement = _requirement(profile_id="does_not_exist")
        plan = await LLMQueryPlanner(client).plan(requirement, max_results=15)
        assert plan.queries


class TestConfiguredQueryPlanner:
    def test_use_llm_false_returns_the_deterministic_planner(self):
        assert isinstance(configured_query_planner(use_llm=False), QueryPlanner)

    def test_use_llm_true_without_a_configured_model_raises(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "deepseek")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            configured_query_planner(use_llm=True)
