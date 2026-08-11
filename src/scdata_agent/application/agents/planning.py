"""Research intent to open-world strategy and executable contracts."""

from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.research_planning import configured_research_planner
from scdata_agent.domain.requirements import QueryPlanner


class ResearchPlannerAgent:
    name = AgentName.RESEARCH_PLANNER

    async def run(self, context: AgentContext):
        planner = configured_research_planner(
            context.policy.get("use_llm", True),
            mode=context.policy.get("planner_mode"),
            profile_id=context.policy.get("profile_id"),
        )

        # The supervisor's dispatch wrapper owns the tracing sink so every
        # provider attempt is tagged with the exact task and workflow node.
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
