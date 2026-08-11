"""Supervisor variant for a curated-manifest run.

``orchestration_v2.py`` is a frozen seam: the graph it builds unconditionally
runs ``initialize_run -> research_planner -> planning_gate ->
resource_discovery`` and overwrites ``state["discovery"]`` along the way, so a
curated manifest cannot enter through that graph by pre-seeding an artifact.
``ManifestRunSupervisor`` instead compiles a different graph whose first real
node ingests the manifest and produces the same ``requirement``/``discovery``
state keys ``resource_discovery`` would have -- everything downstream
(preflight, lineage, acquisition, the two human approval gates, transfer,
curation, QC, governance) is inherited from ``MultiAgentSupervisor`` unchanged,
so a curated bundle takes the identical path a discovered one does.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from celllore.application.agents.manifest_ingest import ManifestIngestAgent
from celllore.application.collaboration import AgentName, CollaborationLedger
from celllore.application.orchestration_v2 import (
    MultiAgentSupervisor,
    SupervisorState,
)


class ManifestRunSupervisor(MultiAgentSupervisor):
    """A curated manifest enters the same downstream path as a discovered one."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.manifest_ingest = ManifestIngestAgent()

    def _build_graph(self, saver: Any | None):
        graph = StateGraph(SupervisorState)
        graph.add_node("initialize_run", self._initialize_run)
        graph.add_node("manifest_ingest", self._manifest_ingest)
        graph.add_node(
            "preflight_assessment",
            self._specialist(AgentName.PREFLIGHT_ASSESSMENT),
        )
        graph.add_node(
            "early_lineage",
            self._specialist(AgentName.LINEAGE_LEAKAGE, self.early_lineage),
        )
        graph.add_node(
            "acquisition_strategy",
            self._specialist(AgentName.ACQUISITION_STRATEGIST),
        )
        graph.add_node("download_approval", self._download_approval)
        graph.add_node(
            "transfer_execution",
            self._specialist(AgentName.ASSET_TRANSFER),
        )
        # Archive expansion (P1.2, S7) is not wired in yet: none of this
        # branch's fixture data has an ARCHIVE-role asset, and inserting a
        # no-op node here would just be one more place for the milestone-0
        # path to diverge from what is actually tested.
        graph.add_node(
            "dataset_curator",
            self._specialist(AgentName.DATASET_CURATOR),
        )
        graph.add_node(
            "materialized_quality",
            self._specialist(AgentName.QUALITY_ASSESSMENT),
        )
        graph.add_node(
            "late_lineage",
            self._specialist(AgentName.LINEAGE_LEAKAGE, self.late_lineage),
        )
        graph.add_node(
            "corpus_governance",
            self._specialist(AgentName.CORPUS_GOVERNANCE),
        )
        graph.add_node("corpus_approval", self._corpus_approval)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "initialize_run")
        graph.add_edge("initialize_run", "manifest_ingest")
        graph.add_edge("manifest_ingest", "preflight_assessment")
        graph.add_edge("manifest_ingest", "early_lineage")
        graph.add_edge(
            ["preflight_assessment", "early_lineage"],
            "acquisition_strategy",
        )
        graph.add_edge("acquisition_strategy", "download_approval")
        graph.add_conditional_edges(
            "download_approval",
            self._route_after_download,
            {"stop": "finalize", "transfer": "transfer_execution"},
        )
        graph.add_edge("transfer_execution", "dataset_curator")
        graph.add_edge("dataset_curator", "materialized_quality")
        graph.add_edge("materialized_quality", "late_lineage")
        graph.add_edge("late_lineage", "corpus_governance")
        graph.add_edge("corpus_governance", "corpus_approval")
        graph.add_edge("corpus_approval", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=saver)

    async def _manifest_ingest(self, state: SupervisorState) -> dict[str, Any]:
        context = self._context(state)
        await self._dispatch(context, AgentName.MANIFEST_INGEST, self.manifest_ingest)
        assert context.requirement is not None
        assert context.discovery is not None
        return {
            "requirement": context.requirement.model_dump(mode="json"),
            "discovery": context.discovery.model_dump(mode="json"),
        }


def supervisor_for_run(
    ledger: CollaborationLedger,
    database_path: str,
    run_id: str,
    *,
    trace_mode: str | None = None,
) -> MultiAgentSupervisor:
    """Resume rebuilds the graph, so it must rebuild the one the run started on."""
    policy = ledger.get_run(run_id).policy
    cls = ManifestRunSupervisor if policy.get("source") == "manifest" else MultiAgentSupervisor
    return cls(ledger, database_path, trace_mode=trace_mode)
