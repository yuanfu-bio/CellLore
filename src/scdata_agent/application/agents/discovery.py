"""Public resource discovery specialization with bounded read-only reasoning."""

from __future__ import annotations

import json

from langchain.tools import tool

from scdata_agent.application.agents.shared import AgentContext, FixedRequirementCompiler
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.discovery import default_workflow
from scdata_agent.application.resource_pipeline import (
    build_discovery_pointer_batch,
)
from scdata_agent.domain.models import QuerySpec
from scdata_agent.domain.resolution import merge_source_records
from scdata_agent.infrastructure.llm.agent_runtime import run_bounded_agent
from scdata_agent.infrastructure.llm.query_planner import configured_query_planner
from scdata_agent.infrastructure.llm.router import ReasoningProfile

# Bounded, not unlimited: a profile could make this configurable later, but a
# fixed budget already turns "at most one fixed supplementary search" into a
# genuine multi-round loop without needing a new LangGraph node -- see the
# plan's own note that the existing single-node bounded tool loop already
# fits this need.
MAX_SUPPLEMENTARY_SEARCHES = 3


def _coverage_gaps(requirement, candidates: list[dict]) -> dict:
    """What the requirement asked for that no discovered candidate shows yet.

    Read-only comparison against ``ResearchRequirement.required_omics_layers``/
    ``pairing_requirement`` (populated even for assays no named technology
    covers -- see domain.requirements), so a coverage gap can be identified
    for an assay this table has never seen by name.
    """
    if requirement is None:
        return {"gaps": [], "candidate_count": len(candidates)}
    observed_layers = {
        layer
        for candidate in candidates
        for profile in candidate.get("assay_profiles", [])
        for layer in profile.get("omics_layers", [])
    }
    required_layers = set(requirement.required_omics_layers)
    missing_layers = sorted(str(layer) for layer in required_layers - observed_layers)
    gaps = []
    if missing_layers:
        gaps.append(
            f"No discovered candidate yet reports layer(s): {', '.join(missing_layers)}."
        )
    if requirement.pairing_requirement is not None:
        acceptable = set(requirement.pairing_requirement.acceptable_levels)
        has_acceptable_relationship = any(
            profile.get("pairing_level") in acceptable
            for candidate in candidates
            for profile in candidate.get("assay_profiles", [])
        )
        if not has_acceptable_relationship:
            gaps.append(
                "No discovered candidate yet reports a pairing relationship in "
                f"{sorted(str(level) for level in acceptable)}."
            )
    return {"gaps": gaps, "candidate_count": len(candidates)}


class ResourceDiscoveryAgent:
    name = AgentName.RESOURCE_DISCOVERY

    async def run(self, context: AgentContext):
        if context.requirement is None:
            raise RuntimeError("Research requirement is required before discovery.")
        workflow = default_workflow(context.database_path, use_llm=False)
        workflow.compiler = FixedRequirementCompiler(context.requirement)
        # Round-0 query authoring is a core dependency here too, same as
        # requirement compilation and research planning -- see
        # LLMQueryPlanner's own docstring for why the fixed technology tables
        # in domain.requirements are not good enough on their own.
        workflow.planner = configured_query_planner(context.policy.get("use_llm", True))
        context.discovery = await workflow.run(context.question, max_results=context.policy.get("max_results", 20))
        extra_records = []
        note, trajectory = await self._review_results(context, workflow, extra_records)
        if extra_records:
            records = [*context.discovery.source_records, *extra_records]
            candidates = merge_source_records(records)
            context.discovery = context.discovery.model_copy(update={
                "source_records": records,
                "candidates": candidates,
                "feasibility_reports": [workflow.assessor.assess(context.requirement, candidate) for candidate in candidates],
            })
        payload = context.discovery.model_dump(mode="json")
        payload["agent_note"] = note
        # The audit record of every round the bounded search loop ran: what it
        # searched, why (the coverage gap it was closing), and what it found.
        # `agent_note` stays a short human-readable summary for the UI.
        payload["search_trajectory"] = trajectory
        discovery_artifact = artifact(
            context.run_id,
            ArtifactKind.DISCOVERY_RESULT,
            self.name,
            payload,
        )
        pointer_batch = build_discovery_pointer_batch(
            context, discovery_artifact
        )
        pointer_artifact = artifact(
            context.run_id,
            ArtifactKind.RESOURCE_POINTERS,
            self.name,
            pointer_batch.model_dump(mode="json"),
            evidence_ids=[
                evidence_id
                for pointer in pointer_batch.pointers
                for evidence_id in pointer.evidence_ids
            ],
            parents=[discovery_artifact.artifact_id],
        )
        outputs = [discovery_artifact, pointer_artifact]
        if context.catalog is not None:
            for pointer in pointer_batch.pointers:
                context.catalog.append(
                    record_id=pointer.pointer_id,
                    record_type="resource_pointer",
                    run_id=context.run_id,
                    value=pointer,
                )
        reviewed_plan = self._review_plan_after_discovery(context)
        if reviewed_plan is not None:
            outputs.append(
                artifact(
                    context.run_id,
                    ArtifactKind.RESEARCH_PLAN,
                    self.name,
                    reviewed_plan.model_dump(mode="json"),
                )
            )
        return outputs

    def _review_plan_after_discovery(self, context: AgentContext):
        if context.research_plan is None or context.discovery is None:
            return None
        plan = context.research_plan
        uncertainties = list(plan.uncertainties)
        alternatives = list(plan.alternative_strategies)
        if context.discovery.candidates:
            uncertainties.append(
                "Discovered candidates remain unverified until evidence and metadata review complete."
            )
        else:
            uncertainties.append(
                "The initial supported-source search returned no candidates for the executable contract."
            )
            alternatives.append(
                "Broaden terminology or implement the reported capability gaps before concluding that suitable data do not exist."
            )
        if context.discovery.errors:
            uncertainties.append(
                "One or more source searches failed; absence of results is not evidence of absence."
            )
        context.research_plan = plan.model_copy(
            update={
                "uncertainties": list(dict.fromkeys(uncertainties)),
                "alternative_strategies": list(dict.fromkeys(alternatives)),
                "planner_diagnostics": [
                    *plan.planner_diagnostics,
                    "planner:bounded_post_discovery_review",
                ],
            }
        )
        return context.research_plan

    async def _review_results(
        self, context: AgentContext, workflow, extra_records: list
    ) -> tuple[str | None, list[dict]]:
        payload = context.discovery.model_dump(mode="json")
        trajectory: list[dict] = []
        supplementary_searches = 0
        if not context.policy.get("use_llm", True):
            return None, trajectory

        @tool
        def inspect_query_plan() -> dict:
            """Read the executed query plan and source errors. This tool is read-only."""
            return {"query_plan": payload["query_plan"], "errors": payload.get("errors", [])}

        @tool
        def inspect_candidates() -> list[dict]:
            """Read discovered candidate identifiers and titles. This tool is read-only."""
            return [{"candidate_id": item["candidate_id"], "title": item["title"], "access_level": item["access_level"]} for item in payload["candidates"]]

        @tool
        def inspect_coverage_gaps() -> dict:
            """Compare discovered candidates against the requirement's required omics layers
            and pairing relationship. Read-only. Use this to decide whether a supplementary
            search is worth spending, and what it should look for."""
            return _coverage_gaps(context.requirement, payload["candidates"])

        @tool
        async def search_repository(source: str, query: str, max_results: int = 5) -> list[dict]:
            """Search europe_pmc, ncbi_geo, or ncbi_sra for additional public metadata. Read-only;
            returns at most five records. You author the query text yourself -- do not expect a
            template; use inspect_coverage_gaps to decide what terms are still missing."""
            nonlocal supplementary_searches
            if supplementary_searches >= MAX_SUPPLEMENTARY_SEARCHES:
                return [{"error": "supplementary search limit reached"}]
            if source not in workflow.adapters:
                return [{"error": "source must be europe_pmc, ncbi_geo, or ncbi_sra"}]
            round_number = supplementary_searches + 1
            supplementary_searches += 1
            spec = QuerySpec(source=source, query=query[:1000], purpose="LLM-guided supplementary discovery", max_results=max(1, min(max_results, 5)))
            try:
                records = await workflow.adapters[source].search(spec)
            except Exception as error:
                trajectory.append({
                    "round": round_number, "source": source, "query": spec.query,
                    "record_count": 0, "error": f"{type(error).__name__}: {error}",
                })
                return [{"error": f"{type(error).__name__}: {error}"}]
            extra_records.extend(records)
            trajectory.append({
                "round": round_number, "source": source, "query": spec.query,
                "record_count": len(records),
                "record_ids": [item.record_id for item in records],
            })
            return [{"record_id": item.record_id, "source": item.source_name, "title": item.title, "identifiers": [identifier.model_dump() for identifier in item.identifiers]} for item in records]

        try:
            planning_context = None
            if context.research_plan is not None:
                planning_context = {
                    "intent_summary": context.research_plan.intent.intent_summary,
                    "data_needs": [
                        {
                            "name": item.name,
                            "data_type": item.data_type,
                            "priority": item.priority,
                            "execution_status": item.execution_status,
                        }
                        for item in context.research_plan.data_needs
                    ],
                    "capability_gaps": context.research_plan.capability_gaps,
                }
            note = await run_bounded_agent(
                profile=ReasoningProfile.DISCOVERY,
                tools=[inspect_query_plan, inspect_candidates, inspect_coverage_gaps, search_repository],
                system_prompt=(
                    "You review public single-cell epigenomics discovery results. Use only "
                    "supplied read-only tools. You may perform up to "
                    f"{MAX_SUPPLEMENTARY_SEARCHES} supplementary repository searches when "
                    "inspect_coverage_gaps shows baseline coverage is insufficient -- author each "
                    "search query yourself from the research question and the coverage gap you are "
                    "closing; do not expect a fixed template. Stop early once coverage looks "
                    "sufficient. Do not claim unobserved files or metadata. Return a short Chinese "
                    "recommendation for the next evidence check."
                ),
                user_prompt=json.dumps(
                    {
                        "research_question": context.question,
                        "planning_context": planning_context,
                    },
                    ensure_ascii=False,
                ),
            )
            return note, trajectory
        except Exception:
            return None, trajectory
