"""Composition root for deep research planning."""

from __future__ import annotations

from scdata_agent.domain.curation.profile import CurationProfile
from scdata_agent.domain.curation.profiles import profile_by_id, select_profile
from scdata_agent.domain.planning import ResearchPlan
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler
from scdata_agent.infrastructure.llm.research_planner import (
    MultiStageResearchPlanner,
    fallback_research_plan,
)
from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile


class DeterministicResearchPlanner:
    """Explicit deterministic mode -- for tests and offline debugging only.

    Research goal interpretation, search planning, and evidence synthesis are
    core LLM-dependent capabilities of this agent, not an optional
    enhancement it can quietly do without. This class exists so tests and
    explicit offline tooling can still exercise the deterministic contract
    machinery; it is reached only when a caller passes ``use_llm=False``
    outright, never as a silent fallback when an LLM was supposed to run but
    wasn't configured -- see ``configured_research_planner``.
    """

    def __init__(self, profile: CurationProfile | None = None) -> None:
        self.fallback = RuleBasedRequirementCompiler()
        self.profile = profile

    async def plan(self, question: str) -> ResearchPlan:
        # Selected per-question, not defaulted at construction time: a
        # profile is a decision about this specific research goal, not a
        # blanket default every question gets tagged with regardless of what
        # it actually asks for (paired_atac_rna_human is a specialized
        # objective, not the system's universal fallback profile).
        profile = self.profile or select_profile(question)
        return fallback_research_plan(
            question,
            self.fallback,
            "planner:offline_deterministic_fallback",
            profile,
        )


def configured_research_planner(
    use_llm: bool = True, mode: str | None = None, profile_id: str | None = None
):
    """Return the LLM-backed planner, or ``DeterministicResearchPlanner`` when
    explicitly requested via ``use_llm=False``.

    If the caller asked for LLM planning (the default) and no model is
    actually configured, that is a deployment error -- ``ModelRouter.
    from_environment()``'s ``RuntimeError`` propagates (fail fast) rather
    than silently downgrading to a deterministic plan nobody asked for.
    """
    profile = profile_by_id(profile_id) if profile_id else None
    if not use_llm:
        return DeterministicResearchPlanner(profile)
    router = ModelRouter.from_environment()
    return MultiStageResearchPlanner(
        router.client(ReasoningProfile.PLANNER),
        review_client=router.client(ReasoningProfile.REVIEW),
        mode=mode,
        profile=profile,
    )
