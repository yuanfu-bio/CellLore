"""Composition root for deep research planning."""

from __future__ import annotations

from scdata_agent.domain.planning import ResearchPlan
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler
from scdata_agent.infrastructure.llm.research_planner import (
    MultiStageResearchPlanner,
    fallback_research_plan,
)
from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile


class DeterministicResearchPlanner:
    """Explicit degraded mode used when no semantic model is configured."""

    def __init__(self) -> None:
        self.fallback = RuleBasedRequirementCompiler()

    async def plan(self, question: str) -> ResearchPlan:
        return fallback_research_plan(
            question,
            self.fallback,
            "planner:offline_deterministic_fallback",
        )


def configured_research_planner(use_llm: bool = True, mode: str | None = None):
    if not use_llm:
        return DeterministicResearchPlanner()
    try:
        router = ModelRouter.from_environment()
        return MultiStageResearchPlanner(
            router.client(ReasoningProfile.PLANNER),
            review_client=router.client(ReasoningProfile.REVIEW),
            mode=mode,
        )
    except RuntimeError:
        return DeterministicResearchPlanner()
