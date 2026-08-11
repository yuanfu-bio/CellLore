"""LangGraph discovery workflow with deterministic, typed nodes."""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from scdata_agent.domain.curation.profiles import select_profile
from scdata_agent.domain.feasibility import FeasibilityAssessor
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import (
    DatasetCandidate,
    DiscoveryRunResult,
    FeasibilityReport,
    QueryPlan,
    ResearchRequirement,
    SourceRecord,
)
from scdata_agent.domain.requirements import (
    CorpusQueryPlanner,
    QueryPlanner,
    RuleBasedRequirementCompiler,
)
from scdata_agent.domain.resolution import merge_source_records
from scdata_agent.infrastructure.llm.requirement_compiler import StepFunRequirementCompiler
from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile
from scdata_agent.infrastructure.persistence.evidence_ledger import EvidenceLedger
from scdata_agent.infrastructure.sources.base import SourceAdapter
from scdata_agent.infrastructure.sources.europe_pmc import EuropePMCAdapter
from scdata_agent.infrastructure.sources.ncbi import NCBIEntrezAdapter


class DiscoveryState(TypedDict, total=False):
    question: str
    max_results: int
    run_id: str
    requirement: ResearchRequirement
    query_plan: QueryPlan
    source_records: list[SourceRecord]
    candidates: list[DatasetCandidate]
    feasibility_reports: list[FeasibilityReport]
    errors: list[str]


class DiscoveryWorkflow:
    def __init__(
        self,
        adapters: list[SourceAdapter],
        ledger: EvidenceLedger | None = None,
        requirement_compiler: object | None = None,
        query_planner: object | None = None,
    ) -> None:
        self.adapters = {adapter.name: adapter for adapter in adapters}
        self.ledger = ledger
        self.compiler = requirement_compiler or RuleBasedRequirementCompiler()
        self.planner = query_planner or QueryPlanner()
        self.assessor = FeasibilityAssessor()
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(DiscoveryState)
        graph.add_node("compile_requirement", self._compile_requirement)
        graph.add_node("plan_queries", self._plan_queries)
        graph.add_node("search_sources", self._search_sources)
        graph.add_node("merge_candidates", self._merge_candidates)
        graph.add_node("assess_feasibility", self._assess_feasibility)
        graph.add_node("persist", self._persist)
        graph.add_edge(START, "compile_requirement")
        graph.add_edge("compile_requirement", "plan_queries")
        graph.add_edge("plan_queries", "search_sources")
        graph.add_edge("search_sources", "merge_candidates")
        graph.add_edge("merge_candidates", "assess_feasibility")
        graph.add_edge("assess_feasibility", "persist")
        graph.add_edge("persist", END)
        return graph.compile()

    async def _compile_requirement(self, state: DiscoveryState) -> dict[str, Any]:
        question = state["question"]
        requirement = self.compiler.compile(question, select_profile(question))
        if inspect.isawaitable(requirement):
            requirement = await requirement
        assert isinstance(requirement, ResearchRequirement)
        return {
            "run_id": stable_id("run", f"{question}:{os.urandom(8).hex()}"),
            "requirement": requirement,
            "errors": list(requirement.compiler_warnings),
        }

    async def _plan_queries(self, state: DiscoveryState) -> dict[str, Any]:
        query_plan = self.planner.plan(
            state["requirement"],
            max_results=state.get("max_results", 20),
        )
        if inspect.isawaitable(query_plan):
            query_plan = await query_plan
        return {"query_plan": query_plan}

    async def _search_sources(
        self, state: DiscoveryState
    ) -> dict[str, Any]:
        queries = [
            query
            for query in state["query_plan"].queries
            if query.source in self.adapters
        ]

        source_limits = {
            source: asyncio.Semaphore(1) for source in self.adapters
        }

        async def execute(query):
            try:
                async with source_limits[query.source]:
                    records = await self.adapters[query.source].search(query)
                return records, None
            except Exception as error:  # source isolation is intentional
                return [], f"{query.source}: {type(error).__name__}: {error}"

        results = await asyncio.gather(*(execute(query) for query in queries))
        records_by_id = {
            record.record_id: record
            for batch, _ in results
            for record in batch
        }
        records = list(records_by_id.values())
        errors = [
            *state.get("errors", []),
            *(error for _, error in results if error),
        ]
        return {"source_records": records, "errors": errors}

    def _merge_candidates(self, state: DiscoveryState) -> dict[str, Any]:
        return {"candidates": merge_source_records(state["source_records"])}

    def _assess_feasibility(self, state: DiscoveryState) -> dict[str, Any]:
        return {
            "feasibility_reports": [
                self.assessor.assess(state["requirement"], candidate)
                for candidate in state["candidates"]
            ]
        }

    def _persist(self, state: DiscoveryState) -> dict[str, Any]:
        if self.ledger is not None:
            self.ledger.save_run(_result_from_state(state))
        return {}

    async def run(
        self, question: str, max_results: int = 20
    ) -> DiscoveryRunResult:
        state = await self.graph.ainvoke(
            {"question": question, "max_results": max_results}
        )
        return _result_from_state(state)


def _result_from_state(state: DiscoveryState) -> DiscoveryRunResult:
    return DiscoveryRunResult(
        run_id=state["run_id"],
        requirement=state["requirement"],
        query_plan=state["query_plan"],
        source_records=state.get("source_records", []),
        candidates=state.get("candidates", []),
        feasibility_reports=state.get("feasibility_reports", []),
        errors=state.get("errors", []),
    )


def configured_requirement_compiler(use_llm: bool = True) -> object:
    """Return the LLM-backed compiler, or the deterministic one when
    explicitly requested via ``use_llm=False`` (tests/offline debugging only).

    Semantic requirement compilation is a core dependency of this agent, not
    an optional enhancement -- if the caller asked for LLM compilation and no
    model is actually configured, that is a deployment error and must fail
    fast (``ModelRouter.from_environment()``'s ``RuntimeError`` propagates)
    rather than silently downgrading to a deterministic contract nobody
    asked for and no downstream reviewer will know happened.
    """
    if not use_llm:
        return RuleBasedRequirementCompiler()
    client = ModelRouter.from_environment().client(ReasoningProfile.PLANNER)
    return StepFunRequirementCompiler(
        client,
        structured_model=ModelRouter.from_environment().tool_model(ReasoningProfile.PLANNER),
        critic_client=ModelRouter.from_environment().client(ReasoningProfile.REVIEW),
    )


def default_workflow(
    database_path: str = "var/celllore.sqlite",
    *,
    use_llm: bool = True,
) -> DiscoveryWorkflow:
    email = os.getenv("NCBI_EMAIL")
    api_key = os.getenv("NCBI_API_KEY")
    adapters = [
        EuropePMCAdapter(),
        NCBIEntrezAdapter(
            database="gds",
            name="ncbi_geo",
            email=email,
            api_key=api_key,
        ),
        NCBIEntrezAdapter(
            database="sra",
            name="ncbi_sra",
            email=email,
            api_key=api_key,
        ),
    ]
    return DiscoveryWorkflow(
        adapters=adapters,
        ledger=EvidenceLedger(database_path),
        requirement_compiler=configured_requirement_compiler(use_llm),
    )



def default_corpus_workflow(
    database_path: str = "var/celllore.sqlite",
) -> DiscoveryWorkflow:
    workflow = default_workflow(database_path, use_llm=False)
    workflow.planner = CorpusQueryPlanner()
    return workflow
