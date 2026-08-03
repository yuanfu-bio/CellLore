"""Research intent to open-world strategy and executable contracts."""

from typing import Any

from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.research_planning import configured_research_planner
from scdata_agent.domain.requirements import QueryPlanner
from scdata_agent.infrastructure.llm import tracing


class ResearchPlannerAgent:
    name = AgentName.RESEARCH_PLANNER

    async def run(self, context: AgentContext):
        planner = configured_research_planner(
            context.policy.get("use_llm", True),
            mode=context.policy.get("planner_mode"),
        )

        def _persist_llm_event(event: dict[str, Any]) -> None:
            context.ledger.add_artifact(
                artifact(context.run_id, ArtifactKind.LLM_CALL, self.name, event)
            )

        with tracing.sink(_persist_llm_event):
            research_plan = await planner.plan(context.question)
        requirement = research_plan.executable_contracts[0]
        context.research_plan = research_plan
        context.requirement = requirement
        query_plan = QueryPlanner().plan(requirement, context.policy.get("max_results", 20))
        query_payload = query_plan.model_dump(mode="json")
        query_payload.update(
            {
                "contract_count": len(research_plan.executable_contracts),
                "capability_gaps": research_plan.capability_gaps,
            }
        )
        return [
            artifact(
                context.run_id,
                ArtifactKind.RESEARCH_PLAN,
                self.name,
                research_plan.model_dump(mode="json"),
            ),
            artifact(
                context.run_id,
                ArtifactKind.QUERY_PLAN,
                self.name,
                query_payload,
            ),
        ]
