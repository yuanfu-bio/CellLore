"""LLM is a core dependency, not an optional enhancement.

`use_llm=True` (the default for every one of these factories) must fail fast
when no model is actually configured, rather than silently downgrading to a
deterministic-only workflow that looks like a resolved result but never
performed the semantic reasoning it claims to. `use_llm=False` remains a
legitimate, explicit choice for tests and offline debugging.
"""

import pytest

from celllore.application.agents.pair_unit_resolver import (
    PairUnitResolver,
    configured_pair_unit_resolver,
)
from celllore.application.agents.pairing_verifier import (
    PairingVerifier,
    configured_pairing_verifier,
)
from celllore.application.discovery import configured_requirement_compiler
from celllore.application.research_planning import (
    DeterministicResearchPlanner,
    configured_research_planner,
)
from celllore.domain.requirements import QueryPlanner, RuleBasedRequirementCompiler
from celllore.infrastructure.llm.query_planner import configured_query_planner


def _unconfigured_environment(monkeypatch, tmp_path):
    # chdir away from the repo's real .env (which configures a real provider)
    # and select a provider whose required key is then absent.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


class TestRequirementCompilerFailsFast:
    def test_use_llm_true_without_a_configured_model_raises(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            configured_requirement_compiler(use_llm=True)

    def test_use_llm_false_stays_a_legitimate_explicit_choice(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        compiler = configured_requirement_compiler(use_llm=False)
        assert isinstance(compiler, RuleBasedRequirementCompiler)


class TestResearchPlannerFailsFast:
    def test_use_llm_true_without_a_configured_model_raises(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            configured_research_planner(use_llm=True)

    def test_use_llm_false_stays_a_legitimate_explicit_choice(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        planner = configured_research_planner(use_llm=False)
        assert isinstance(planner, DeterministicResearchPlanner)


class TestPairUnitResolverFailsFast:
    def test_use_llm_true_without_a_configured_model_raises(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            configured_pair_unit_resolver(use_llm=True)

    def test_use_llm_false_stays_a_legitimate_explicit_choice(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        resolver = configured_pair_unit_resolver(use_llm=False)
        assert isinstance(resolver, PairUnitResolver)
        assert resolver.client is None


class TestQueryPlannerFailsFast:
    def test_use_llm_true_without_a_configured_model_raises(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            configured_query_planner(use_llm=True)

    def test_use_llm_false_stays_a_legitimate_explicit_choice(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        planner = configured_query_planner(use_llm=False)
        assert isinstance(planner, QueryPlanner)


class TestPairingVerifierFailsFast:
    def test_use_llm_true_without_a_configured_model_raises(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            configured_pairing_verifier(use_llm=True)

    def test_use_llm_false_stays_a_legitimate_explicit_choice(self, monkeypatch, tmp_path):
        _unconfigured_environment(monkeypatch, tmp_path)
        verifier = configured_pairing_verifier(use_llm=False)
        assert isinstance(verifier, PairingVerifier)
        assert verifier.client is None
