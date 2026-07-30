"""Shared contracts and helpers for specialist agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from scdata_agent.application.collaboration import AgentArtifact, AgentName, CollaborationLedger
from scdata_agent.domain.models import DiscoveryRunResult, ResearchRequirement
from scdata_agent.domain.planning import ResearchPlan
from scdata_agent.infrastructure.persistence.catalog import Catalog


@dataclass
class AgentContext:
    run_id: str
    question: str
    database_path: str
    ledger: CollaborationLedger
    policy: dict[str, Any] = field(default_factory=dict)
    research_plan: ResearchPlan | None = None
    requirement: ResearchRequirement | None = None
    discovery: DiscoveryRunResult | None = None
    artifacts: list[AgentArtifact] = field(default_factory=list)
    catalog: Catalog | None = None
    resolution: dict[str, Any] | None = None


class SpecialistAgent(Protocol):
    name: AgentName

    async def run(self, context: AgentContext) -> list[AgentArtifact]: ...


class FixedRequirementCompiler:
    """Adapter that supplies a previously compiled requirement to discovery."""

    def __init__(self, requirement: ResearchRequirement) -> None:
        self.requirement = requirement

    def compile(self, question: str) -> ResearchRequirement:
        return self.requirement


def need_discovery(context: AgentContext) -> DiscoveryRunResult:
    if context.discovery is None:
        raise RuntimeError("Discovery result is required for this agent.")
    return context.discovery


def lineage_map(artifacts: list[AgentArtifact]) -> dict[str, list[str]]:
    for item in reversed(artifacts):
        if item.kind.value == "lineage_report":
            return {
                candidate_id: [family["family_id"]]
                for family in item.payload["families"]
                for candidate_id in family["candidate_ids"]
            }
    return {}


def latest_artifact(
    artifacts: list[AgentArtifact], kind: str
) -> AgentArtifact | None:
    for item in reversed(artifacts):
        if item.kind.value == kind:
            return item
