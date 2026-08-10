"""Evidence-maturity Supervisor with gated acquisition and corpus publication."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from scdata_agent.application.agents.acquisition_v2 import (
    AcquisitionStrategistAgent,
)
from scdata_agent.application.agents.curator import DatasetCuratorAgent
from scdata_agent.application.agents.discovery import ResourceDiscoveryAgent
from scdata_agent.application.agents.evidence_escalation import (
    EvidenceEscalationAgent,
)
from scdata_agent.application.agents.geo_sample_metadata import (
    GeoSampleMetadataAgent,
)
from scdata_agent.application.agents.inventory_reconcile import (
    InventoryReconcileAgent,
)
from scdata_agent.application.agents.materialized_quality import (
    MaterializedQualityAssessmentAgent,
)
from scdata_agent.application.agents.planning import ResearchPlannerAgent
from scdata_agent.application.agents.progressive_asset_scout import (
    ProgressiveAssetScoutAgent,
)
from scdata_agent.application.agents.progressive_evidence import (
    ProgressiveEvidenceCuratorAgent,
)
from scdata_agent.application.agents.progressive_lineage_v2 import (
    ProgressiveLineageLeakageAgent,
)
from scdata_agent.application.agents.progressive_preflight import (
    ProgressivePreflightAssessmentAgent,
)
from scdata_agent.application.agents.shared import (
    AgentContext,
    SpecialistAgent,
    latest_artifact,
)
from scdata_agent.application.agents.snapshot_governance import (
    SnapshotCorpusGovernanceAgent,
)
from scdata_agent.application.agents.transfer_execution import (
    TransferExecutionAgent,
)
from scdata_agent.application.collaboration import (
    AgentArtifact,
    AgentMessage,
    AgentName,
    ArtifactKind,
    CheckpointStatus,
    CollaborationLedger,
    DecisionCheckpoint,
    RunStatus,
    artifact,
)
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    AcquisitionPlan,
    BundleStatus,
    CorpusSnapshot,
    PreflightBundleReport,
)
from scdata_agent.domain.models import DiscoveryRunResult, ResearchRequirement
from scdata_agent.domain.planning import PlanReadiness, ResearchPlan
from scdata_agent.domain.resource_pipeline import (
    EvidenceEscalationReport,
    ScoutPhase,
)
from scdata_agent.infrastructure.llm import tracing
from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile
from scdata_agent.infrastructure.observability import configure_tracing
from scdata_agent.infrastructure.persistence.catalog import Catalog


class SupervisorState(TypedDict, total=False):
    run_id: str
    question: str
    database_path: str
    policy: dict[str, Any]
    research_plan: ResearchPlan
    requirement: ResearchRequirement
    planning_iterations: int
    planning_replan: bool
    discovery: DiscoveryRunResult

    evidence_route: str
    resolution: dict[str, Any]
    corpus_resolution: dict[str, Any]
def workflow_topology() -> dict[str, Any]:
    """Canonical supervisor DAG exposed to the control-room UI."""
    nodes = [
        ("initialize_run", "Initialize run", "gate", 20, 20),
        ("research_planner", "Research planner", "agent", 185, 20),
        ("planning_gate", "Planning gate", "gate", 350, 20),
        ("resource_discovery", "Resource discovery", "agent", 515, 20),
        ("asset_scout_direct", "Asset scout · direct", "agent", 350, 115),
        ("evidence_metadata", "Evidence curator · metadata", "agent", 515, 115),
        ("evidence_escalation_gate", "Evidence escalation", "gate", 350, 210),
        ("evidence_fulltext", "Evidence curator · full text", "agent", 515, 210),
        ("asset_scout_enriched", "Asset scout · enriched", "agent", 515, 305),
        ("geo_sample_metadata", "GEO sample metadata", "agent", 350, 305),
        ("inventory_reconcile", "Inventory reconcile", "agent", 350, 400),
        ("preflight_assessment", "Preflight assessment", "agent", 350, 495),
        ("early_lineage", "Early lineage", "agent", 515, 495),
        ("acquisition_strategy", "Acquisition strategy", "agent", 350, 590),
        ("download_approval", "Download approval", "gate", 515, 590),
        ("transfer_execution", "Transfer execution", "agent", 20, 685),
        ("dataset_curator", "Dataset curator", "agent", 185, 685),
        ("materialized_quality", "Materialized quality", "agent", 350, 685),
        ("late_lineage", "Late lineage", "agent", 515, 685),
        ("corpus_governance", "Corpus governance", "agent", 350, 780),
        ("corpus_approval", "Corpus approval", "gate", 185, 780),
        ("finalize", "Finalize run", "gate", 20, 780),
    ]
    edges = [
        ("initialize_run", "research_planner", None),
        ("research_planner", "planning_gate", None),
        ("planning_gate", "research_planner", "replan"),
        ("planning_gate", "resource_discovery", "discover"),
        ("resource_discovery", "asset_scout_direct", None),
        ("resource_discovery", "evidence_metadata", None),
        ("asset_scout_direct", "evidence_escalation_gate", None),
        ("evidence_metadata", "evidence_escalation_gate", None),
        ("evidence_escalation_gate", "evidence_fulltext", "fulltext"),
        ("evidence_escalation_gate", "asset_scout_enriched", "scout"),
        ("evidence_fulltext", "asset_scout_enriched", None),
        ("asset_scout_enriched", "geo_sample_metadata", None),
        ("geo_sample_metadata", "inventory_reconcile", None),
        ("inventory_reconcile", "preflight_assessment", None),
        ("inventory_reconcile", "early_lineage", None),
        ("preflight_assessment", "acquisition_strategy", None),
        ("early_lineage", "acquisition_strategy", None),
        ("acquisition_strategy", "download_approval", None),
        ("download_approval", "finalize", "stop"),
        ("download_approval", "transfer_execution", "transfer"),
        ("transfer_execution", "dataset_curator", None),
        ("dataset_curator", "materialized_quality", None),
        ("materialized_quality", "late_lineage", None),
        ("late_lineage", "corpus_governance", None),
        ("corpus_governance", "corpus_approval", None),
        ("corpus_approval", "finalize", None),
    ]
    return {
        "nodes": [
            {"id": node_id, "label": label, "kind": kind, "x": x, "y": y}
            for node_id, label, kind, x, y in nodes
        ],
        "edges": [
            {
                "id": f"{source}->{target}:{condition or 'direct'}",
                "source": source,
                "target": target,
                "condition": condition,
            }
            for source, target, condition in edges
        ],
    }


class MultiAgentSupervisor:
    """Central supervisor; specialists exchange typed, persisted artifacts."""

    def __init__(
        self,
        ledger: CollaborationLedger,
        database_path: str,
        checkpoint_path: str | Path | None = None,
        trace_mode: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.database_path = database_path
        self.catalog = Catalog(database_path)
        self.checkpoint_path = str(
            checkpoint_path
            or Path(database_path).with_name("langgraph_checkpoints.sqlite")
        )
        self.trace_mode = configure_tracing(trace_mode)
        self.agents: dict[AgentName, SpecialistAgent] = {
            AgentName.RESEARCH_PLANNER: ResearchPlannerAgent(),
            AgentName.RESOURCE_DISCOVERY: ResourceDiscoveryAgent(),
            AgentName.EVIDENCE_CURATOR: ProgressiveEvidenceCuratorAgent(
                "metadata"
            ),
            AgentName.ASSET_SCOUT: ProgressiveAssetScoutAgent(
                ScoutPhase.DIRECT
            ),
            AgentName.PREFLIGHT_ASSESSMENT: (
                ProgressivePreflightAssessmentAgent()
            ),
            AgentName.ACQUISITION_STRATEGIST: AcquisitionStrategistAgent(),
            AgentName.ASSET_TRANSFER: TransferExecutionAgent(),
            AgentName.DATASET_CURATOR: DatasetCuratorAgent(),
            AgentName.QUALITY_ASSESSMENT: MaterializedQualityAssessmentAgent(),
            AgentName.CORPUS_GOVERNANCE: SnapshotCorpusGovernanceAgent(),
        }
        self.evidence_metadata = ProgressiveEvidenceCuratorAgent("metadata")
        self.evidence_fulltext = ProgressiveEvidenceCuratorAgent("fulltext")
        self.asset_scout_direct = ProgressiveAssetScoutAgent(ScoutPhase.DIRECT)
        self.asset_scout_enriched = ProgressiveAssetScoutAgent(
            ScoutPhase.ENRICHED
        )
        self.evidence_escalation = EvidenceEscalationAgent()
        self.geo_sample_metadata = GeoSampleMetadataAgent()
        self.inventory_reconcile = InventoryReconcileAgent()
        self.early_lineage = ProgressiveLineageLeakageAgent("early")
        self.late_lineage = ProgressiveLineageLeakageAgent("late")

    async def execute(self, run_id: str) -> None:
        run = self.ledger.get_run(run_id)
        self.ledger.set_run_status(run_id, RunStatus.RUNNING)
        self.ledger.add_event(
            run_id,
            "graph_started",
            {"thread_id": run_id, "trace_mode": self.trace_mode.value},
        )
        try:
            async with AsyncSqliteSaver.from_conn_string(
                self.checkpoint_path
            ) as saver:
                await saver.setup()
                graph = self._build_graph(saver)
                await graph.ainvoke(
                    {
                        "run_id": run_id,
                        "question": run.question,
                        "database_path": self.database_path,
                        "policy": run.policy,
                    },
                    config=self._config(run_id),
                )
        except asyncio.CancelledError:
            self.ledger.set_run_status(run_id, RunStatus.CANCELLED)
            raise
        except Exception as error:
            self._record_failure(run_id, error)
            raise

    async def resume_checkpoint(
        self, checkpoint_id: str, resolution: dict[str, Any]
    ) -> DecisionCheckpoint:
        checkpoint = self.ledger.get_checkpoint(checkpoint_id)
        if checkpoint.kind == "download_selection":
            self._validate_download_resolution(checkpoint, resolution)
        checkpoint = self.ledger.resolve_checkpoint(checkpoint_id, resolution)
        self.ledger.add_event(
            checkpoint.run_id,
            "graph_resumed",
            {"checkpoint_id": checkpoint_id, "resolution": resolution},
        )
        try:
            async with AsyncSqliteSaver.from_conn_string(
                self.checkpoint_path
            ) as saver:
                await saver.setup()
                graph = self._build_graph(saver)
                await graph.ainvoke(
                    Command(resume=resolution),
                    config=self._config(checkpoint.run_id),
                )
        except Exception as error:
            self._record_failure(checkpoint.run_id, error)
            raise
        return checkpoint

    def resolve_checkpoint(
        self, checkpoint_id: str, resolution: dict[str, Any]
    ) -> DecisionCheckpoint:
        return self.ledger.resolve_checkpoint(checkpoint_id, resolution)

    async def graph_state(self, run_id: str) -> dict[str, Any]:
        self.ledger.get_run(run_id)
        async with AsyncSqliteSaver.from_conn_string(
            self.checkpoint_path
        ) as saver:
            await saver.setup()
            snapshot = await self._build_graph(saver).aget_state(
                self._config(run_id)
            )
        return {
            "values": snapshot.values,
            "next": list(snapshot.next),
            "config": snapshot.config,
        }

    def cancel(self, run_id: str) -> None:
        self.ledger.get_run(run_id)
        self.ledger.set_run_status(run_id, RunStatus.CANCELLED)
        self.ledger.add_event(
            run_id, "graph_cancelled", {"thread_id": run_id}
        )

    def build_graph(self, checkpointer: Any | None = None):
        return self._build_graph(checkpointer)

    def _build_graph(self, saver: Any | None):
        graph = StateGraph(SupervisorState)
        graph.add_node("initialize_run", self._initialize_run)
        graph.add_node("research_planner", self._planner)
        graph.add_node("planning_gate", self._planning_gate)
        graph.add_node("resource_discovery", self._discovery)
        graph.add_node(
            "evidence_metadata",
            self._specialist(AgentName.EVIDENCE_CURATOR, self.evidence_metadata, "evidence_metadata"),
        )
        graph.add_node(
            "asset_scout_direct",
            self._specialist(AgentName.ASSET_SCOUT, self.asset_scout_direct, "asset_scout_direct"),
        )
        graph.add_node(
            "evidence_escalation_gate", self._evidence_escalation_gate
        )
        graph.add_node(
            "evidence_fulltext",
            self._specialist(AgentName.EVIDENCE_CURATOR, self.evidence_fulltext, "evidence_fulltext"),
        )
        graph.add_node(
            "asset_scout_enriched",
            self._specialist(AgentName.ASSET_SCOUT, self.asset_scout_enriched, "asset_scout_enriched"),
        )
        graph.add_node(
            "geo_sample_metadata",
            self._specialist(AgentName.GEO_SAMPLE_METADATA, self.geo_sample_metadata, "geo_sample_metadata"),
        )
        graph.add_node(
            "inventory_reconcile",
            self._specialist(AgentName.ASSET_SCOUT, self.inventory_reconcile, "inventory_reconcile"),
        )
        graph.add_node(
            "preflight_assessment",
            self._specialist(AgentName.PREFLIGHT_ASSESSMENT, node_id="preflight_assessment"),
        )
        graph.add_node(
            "early_lineage",
            self._specialist(AgentName.LINEAGE_LEAKAGE, self.early_lineage, "early_lineage"),
        )
        graph.add_node(
            "acquisition_strategy",
            self._specialist(AgentName.ACQUISITION_STRATEGIST, node_id="acquisition_strategy"),
        )
        graph.add_node("download_approval", self._download_approval)
        graph.add_node(
            "transfer_execution",
            self._specialist(AgentName.ASSET_TRANSFER, node_id="transfer_execution"),
        )
        graph.add_node(
            "dataset_curator",
            self._specialist(AgentName.DATASET_CURATOR, node_id="dataset_curator"),
        )
        graph.add_node(
            "materialized_quality",
            self._specialist(AgentName.QUALITY_ASSESSMENT, node_id="materialized_quality"),
        )
        graph.add_node(
            "late_lineage",
            self._specialist(AgentName.LINEAGE_LEAKAGE, self.late_lineage, "late_lineage"),
        )
        graph.add_node(
            "corpus_governance",
            self._specialist(AgentName.CORPUS_GOVERNANCE, node_id="corpus_governance"),
        )
        graph.add_node("corpus_approval", self._corpus_approval)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "initialize_run")
        graph.add_edge("initialize_run", "research_planner")
        graph.add_edge("research_planner", "planning_gate")
        graph.add_conditional_edges(
            "planning_gate",
            self._route_after_planning_gate,
            {"replan": "research_planner", "discover": "resource_discovery"},
        )
        graph.add_edge("resource_discovery", "asset_scout_direct")
        graph.add_edge("resource_discovery", "evidence_metadata")
        graph.add_edge(
            ["asset_scout_direct", "evidence_metadata"],
            "evidence_escalation_gate",
        )
        graph.add_conditional_edges(
            "evidence_escalation_gate",
            self._route_after_evidence_escalation,
            {
                "fulltext": "evidence_fulltext",
                "scout": "asset_scout_enriched",
            },
        )
        graph.add_edge("evidence_fulltext", "asset_scout_enriched")
        graph.add_edge("asset_scout_enriched", "geo_sample_metadata")
        graph.add_edge("geo_sample_metadata", "inventory_reconcile")
        graph.add_edge("inventory_reconcile", "preflight_assessment")
        graph.add_edge("inventory_reconcile", "early_lineage")
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

    async def _initialize_run(
        self, state: SupervisorState
    ) -> dict[str, Any]:
        run_id = state.get("run_id")
        if run_id:
            run = self.ledger.get_run(run_id)
        else:
            question = state.get("question")
            if not question:
                raise ValueError("question is required to start a run.")
            run = self.ledger.create_run(question, state.get("policy", {}))
        self.ledger.set_run_status(run.run_id, RunStatus.RUNNING)
        self.ledger.add_event(run.run_id, "graph_topology_node", {"node": "initialize_run"})
        return {
            "run_id": run.run_id,
            "question": run.question,
            "database_path": state.get(
                "database_path", self.database_path
            ),
            "policy": run.policy,
            "planning_iterations": state.get("planning_iterations", 0),
        }

    async def _planner(self, state: SupervisorState) -> dict[str, Any]:
        context = self._context(state)
        await self._dispatch(context, AgentName.RESEARCH_PLANNER, node_id="research_planner")
        assert context.research_plan is not None
        assert context.requirement is not None
        return {
            "research_plan": context.research_plan.model_dump(mode="json"),
            "requirement": context.requirement.model_dump(mode="json"),
            "planning_replan": False,
        }

    async def _planning_gate(
        self, state: SupervisorState
    ) -> dict[str, Any]:
        self.ledger.add_event(state["run_id"], "graph_topology_node", {"node": "planning_gate"})
        plan = ResearchPlan.model_validate(state["research_plan"])
        if plan.readiness != PlanReadiness.BLOCKED:
            self.ledger.add_event(
                state["run_id"],
                "graph_topology_transition",
                {
                    "source": "planning_gate",
                    "target": "resource_discovery",
                    "condition": "discover",
                },
            )
            return {"planning_replan": False}
        iteration = state.get("planning_iterations", 0)
        checkpoint = self._open_planning_checkpoint(
            state["run_id"], plan, iteration
        )
        self._mark_waiting(checkpoint)
        resolution = interrupt(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "kind": checkpoint.kind,
                "questions": plan.clarifying_questions,
                "options": checkpoint.options,
                "recommendation": checkpoint.recommendation,
            }
        )
        clarification = str(resolution.get("clarification", "")).strip()
        if clarification:
            self.ledger.add_event(
                state["run_id"],
                "graph_topology_transition",
                {
                    "source": "planning_gate",
                    "target": "research_planner",
                    "condition": "replan",
                },
            )
            return {
                "question": (
                    f"{state['question']}\nUser clarification: {clarification}"
                ),
                "planning_iterations": iteration + 1,
                "planning_replan": True,
            }
        self.ledger.add_event(
            state["run_id"],
            "graph_topology_transition",
            {
                "source": "planning_gate",
                "target": "resource_discovery",
                "condition": "discover",
            },
        )
        return {"planning_replan": False}

    @staticmethod
    def _route_after_planning_gate(state: SupervisorState) -> str:
        return "replan" if state.get("planning_replan", False) else "discover"

    async def _evidence_escalation_gate(
        self, state: SupervisorState
    ) -> dict[str, Any]:
        context = self._context(state)
        await self._dispatch(
            context,
            AgentName.EVIDENCE_CURATOR,
            self.evidence_escalation,
            node_id="evidence_escalation_gate",
        )
        escalation_artifact = latest_artifact(
            context.artifacts, ArtifactKind.EVIDENCE_ESCALATION.value
        )
        report = (
            EvidenceEscalationReport.model_validate(
                escalation_artifact.payload
            )
            if escalation_artifact is not None
            else None
        )
        route = (
            "fulltext" if report and report.fulltext_candidate_ids else "scout"
        )
        self.ledger.add_event(
            state["run_id"],
            "graph_topology_transition",
            {
                "source": "evidence_escalation_gate",
                "target": "evidence_fulltext"
                if route == "fulltext"
                else "asset_scout_enriched",
                "condition": route,
            },
        )
        return {"evidence_route": route}

    
    @staticmethod
    def _route_after_evidence_escalation(
        state: SupervisorState,
    ) -> str:
        return state.get("evidence_route", "scout")

    async def _discovery(self, state: SupervisorState) -> dict[str, Any]:
        context = self._context(state)
        await self._dispatch(context, AgentName.RESOURCE_DISCOVERY, node_id="resource_discovery")
        assert context.discovery is not None
        update: dict[str, Any] = {
            "discovery": context.discovery.model_dump(mode="json")
        }
        if context.research_plan is not None:
            update["research_plan"] = context.research_plan.model_dump(
                mode="json"
            )
        return update

    def _specialist(
        self, name: AgentName, agent: SpecialistAgent | None = None, node_id: str | None = None
    ):
        async def node(state: SupervisorState) -> dict[str, Any]:
            await self._dispatch(self._context(state), name, agent, node_id=node_id)
            return {}

        return node

    async def _download_approval(
        self, state: SupervisorState
    ) -> dict[str, Any]:
        context = self._context(state)
        checkpoint = self._open_download_checkpoint(context)
        self._mark_waiting(checkpoint)
        resolution = interrupt(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "kind": checkpoint.kind,
                "options": checkpoint.options,
                "recommendation": checkpoint.recommendation,
            }
        )
        return {"resolution": resolution}

    @staticmethod
    def _route_after_download(state: SupervisorState) -> str:
        option = state.get("resolution", {}).get("option")
        return (
            "transfer"
            if option in {"recommended", "selected_recommended", "all_with_unknown_size"}
            else "stop"
        )

    def _validate_download_resolution(
        self, checkpoint: DecisionCheckpoint, resolution: dict[str, Any]
    ) -> None:
        """Allow a bounded approval without letting a client widen the plan."""
        if resolution.get("option") != "selected_recommended":
            return
        selected = resolution.get("bundle_ids")
        if not isinstance(selected, list) or not selected or not all(
            isinstance(item, str) for item in selected
        ):
            raise ValueError(
                "selected_recommended requires a non-empty bundle_ids list."
            )
        recommended = next(
            (
                set(item.get("bundle_ids", []))
                for item in checkpoint.options
                if item.get("id") == "recommended"
            ),
            set(),
        )
        unknown = set(selected) - recommended
        if unknown:
            raise ValueError(
                "selected_recommended may approve only checkpoint-recommended bundles: "
                + ", ".join(sorted(unknown))
            )

    async def _corpus_approval(
        self, state: SupervisorState
    ) -> dict[str, Any]:
        context = self._context(state)
        checkpoint = self._open_corpus_checkpoint(context)
        self._mark_waiting(checkpoint)
        resolution = interrupt(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "kind": checkpoint.kind,
                "options": checkpoint.options,
                "recommendation": checkpoint.recommendation,
            }
        )
        if resolution.get("option") == "approve_snapshot":
            snapshot_artifact = latest_artifact(
                self.ledger.list_artifacts(context.run_id),
                ArtifactKind.CORPUS_SNAPSHOT.value,
            )
            if snapshot_artifact is not None:
                snapshot = CorpusSnapshot.model_validate(
                    snapshot_artifact.payload
                ).model_copy(update={"approved": True})
                self.catalog.append(
                    record_id=snapshot.snapshot_id,
                    record_type="corpus_snapshot",
                    run_id=context.run_id,
                    value=snapshot,
                )
                approved = artifact(
                    context.run_id,
                    ArtifactKind.CORPUS_SNAPSHOT,
                    AgentName.SUPERVISOR,
                    snapshot.model_dump(mode="json"),
                    parents=[snapshot_artifact.artifact_id],
                )
                self.ledger.add_artifact(approved)
        return {"corpus_resolution": resolution}

    async def _finalize(self, state: SupervisorState) -> dict[str, Any]:
        self.ledger.add_event(state["run_id"], "graph_topology_node", {"node": "finalize"})
        self.ledger.set_run_status(state["run_id"], RunStatus.COMPLETED)
        self.ledger.add_event(
            state["run_id"],
            "graph_completed",
            {
                "download_resolution": state.get("resolution"),
                "corpus_resolution": state.get("corpus_resolution"),
            },
        )
        return {}

    def _context(self, state: SupervisorState) -> AgentContext:
        raw_plan = state.get("research_plan")
        raw_requirement = state.get("requirement")
        raw_discovery = state.get("discovery")
        return AgentContext(
            run_id=state["run_id"],
            question=state["question"],
            database_path=state["database_path"],
            ledger=self.ledger,
            policy=state.get("policy", {}),
            research_plan=(
                ResearchPlan.model_validate(raw_plan) if raw_plan else None
            ),
            requirement=(
                ResearchRequirement.model_validate(raw_requirement)
                if raw_requirement
                else None
            ),
            discovery=(
                DiscoveryRunResult.model_validate(raw_discovery)
                if raw_discovery
                else None
            ),
            artifacts=self.ledger.list_artifacts(state["run_id"]),
            catalog=self.catalog,
            resolution=state.get("resolution"),
        )

    async def _dispatch(
        self,
        context: AgentContext,
        name: AgentName,
        agent: SpecialistAgent | None = None,
        node_id: str | None = None,
    ) -> None:
        self._ensure_active(context.run_id)
        implementation = agent or self.agents[name]
        task = self.ledger.start_task(
            context.run_id,
            name,
            [item.artifact_id for item in context.artifacts],
        )
        self.ledger.add_event(
            context.run_id,
            "graph_node_started",
            {"node": node_id or name.value, "task_id": task.task_id},
        )

        def _persist_llm_event(event: dict[str, Any]) -> None:
            item = artifact(
                context.run_id,
                ArtifactKind.LLM_CALL,
                name,
                event,
            )
            self.ledger.add_artifact(item)
            context.artifacts.append(item)

        try:
            with tracing.stage(node_id or name.value), tracing.sink(_persist_llm_event):
                outputs = await implementation.run(context)
            for output in outputs:
                self.ledger.add_artifact(output)
                context.artifacts.append(output)
            self.ledger.add_message(
                AgentMessage(
                    message_id=stable_id(
                        "message",
                        f"{context.run_id}:{name.value}:{task.task_id}",
                    ),
                    run_id=context.run_id,
                    sender=name,
                    recipient=AgentName.SUPERVISOR,
                    summary=(
                        f"{name.value} completed and published "
                        f"{len(outputs)} artifact(s)."
                    ),
                    recommendation=_recommendation(outputs),
                    artifact_ids=[item.artifact_id for item in outputs],
                    confidence=0.9,
                )
            )
            self.ledger.finish_task(
                task.task_id, [item.artifact_id for item in outputs]
            )
            self.ledger.add_event(
                context.run_id,
                "graph_node_finished",
                {"node": node_id or name.value, "task_id": task.task_id},
            )
        except Exception as error:
            self.ledger.finish_task(
                task.task_id, [], f"{type(error).__name__}: {error}"
            )
            self.ledger.add_event(
                context.run_id,
                "graph_node_failed",
                {"node": node_id or name.value, "error": str(error)},
            )
            raise

    def _open_planning_checkpoint(
        self, run_id: str, plan: ResearchPlan, iteration: int
    ) -> DecisionCheckpoint:
        checkpoint_id = stable_id(
            "checkpoint", f"{run_id}:research_clarification:{iteration}"
        )
        existing = [
            item
            for item in self.ledger.list_checkpoints(run_id)
            if item.checkpoint_id == checkpoint_id
        ]
        if existing:
            return existing[-1]
        return self.ledger.add_checkpoint(
            DecisionCheckpoint(
                checkpoint_id=checkpoint_id,
                run_id=run_id,
                kind="research_clarification",
                title="Clarify a research-design conflict before discovery",
                recommendation=plan.readiness_rationale,
                options=[
                    {
                        "id": "provide_clarification",
                        "label": "Provide clarification",
                    },
                    {
                        "id": "proceed_with_assumptions",
                        "label": "Proceed with documented assumptions",
                    },
                ],
            )
        )

    def _open_download_checkpoint(
        self, context: AgentContext
    ) -> DecisionCheckpoint:
        existing = [
            item
            for item in self.ledger.list_checkpoints(context.run_id)
            if item.kind == "download_selection"
        ]
        if existing:
            return existing[-1]
        plan_artifact = latest_artifact(
            context.artifacts, ArtifactKind.ACQUISITION_PLAN.value
        )
        plan = (
            AcquisitionPlan.model_validate(plan_artifact.payload)
            if plan_artifact
            else AcquisitionPlan(
                plan_id=stable_id("acquisition", context.run_id),
                run_id=context.run_id,
                destination_root=str(
                    Path(context.policy.get("data_root", "data")) / "raw"
                ),
            )
        )
        preflight_artifact = latest_artifact(
            context.artifacts, ArtifactKind.PREFLIGHT_REPORT.value
        )
        ready_ids = {
            report.bundle_id
            for report in (
                PreflightBundleReport.model_validate(item)
                for item in (
                    preflight_artifact.payload.get("reports", [])
                    if preflight_artifact else []
                )
            )
            if report.decision == BundleStatus.PREFLIGHT_READY
        }
        all_ids = [
            item.bundle_id
            for item in plan.bundles
            if item.bundle_id in ready_ids
            and item.bundle_id in {
                *plan.recommended_bundle_ids,
                *plan.manual_review_bundle_ids,
            }
        ]
        unknown_ids = [
            item.bundle_id
            for item in plan.bundles
            if item.bundle_id in ready_ids and item.requires_size_override
        ]
        return self.ledger.add_checkpoint(
            DecisionCheckpoint(
                checkpoint_id=stable_id(
                    "checkpoint", f"{context.run_id}:download"
                ),
                run_id=context.run_id,
                kind="download_selection",
                title="Approve semantic bundles for transfer",
                recommendation=(
                    f"Recommended bundles: {len(plan.recommended_bundle_ids)}; "
                    f"known expected bytes: {plan.total_expected_bytes or 0}."
                ),
                options=[
                    {
                        "id": "plan_only",
                        "label": "Keep plans only",
                        "bundle_ids": [],
                    },
                    {
                        "id": "recommended",
                        "label": "Download recommended bundles",
                        "bundle_ids": plan.recommended_bundle_ids,
                    },
                    {
                        "id": "all_with_unknown_size",
                        "label": "Download recommended and eligible manual-review bundles; allow unknown sizes",
                        "bundle_ids": all_ids,
                        "unknown_size_bundle_ids": unknown_ids,
                    },
                ],
            )
        )

    def _open_corpus_checkpoint(
        self, context: AgentContext
    ) -> DecisionCheckpoint:
        existing = [
            item
            for item in self.ledger.list_checkpoints(context.run_id)
            if item.kind == "corpus_snapshot"
        ]
        if existing:
            return existing[-1]
        snapshot_artifact = latest_artifact(
            context.artifacts, ArtifactKind.CORPUS_SNAPSHOT.value
        )
        snapshot = (
            CorpusSnapshot.model_validate(snapshot_artifact.payload)
            if snapshot_artifact
            else None
        )
        recommendation = (
            f"Admit {len(snapshot.admitted_bundle_ids)}, quarantine "
            f"{len(snapshot.quarantined_bundle_ids)}, exclude "
            f"{len(snapshot.excluded_bundle_ids)} bundle(s)."
            if snapshot
            else "No snapshot was generated."
        )
        return self.ledger.add_checkpoint(
            DecisionCheckpoint(
                checkpoint_id=stable_id(
                    "checkpoint", f"{context.run_id}:corpus_snapshot"
                ),
                run_id=context.run_id,
                kind="corpus_snapshot",
                title="Approve immutable corpus snapshot",
                recommendation=recommendation,
                options=[
                    {
                        "id": "approve_snapshot",
                        "label": "Approve snapshot",
                    },
                    {
                        "id": "reject_snapshot",
                        "label": "Keep as draft",
                    },
                ],
            )
        )

    def _mark_waiting(self, checkpoint: DecisionCheckpoint) -> None:
        if checkpoint.status == CheckpointStatus.OPEN:
            self.ledger.set_run_status(
                checkpoint.run_id, RunStatus.WAITING_FOR_DECISION
            )
            self.ledger.add_event(
                checkpoint.run_id,
                "graph_interrupted",
                {"checkpoint_id": checkpoint.checkpoint_id},
            )

    def _ensure_active(self, run_id: str) -> None:
        if self.ledger.get_run(run_id).status == RunStatus.CANCELLED:
            raise asyncio.CancelledError(f"Run {run_id} was cancelled.")

    def _config(self, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "recursion_limit": 50,
            "tags": ["scdata-agent", "supervisor-v2"],
        }

    def _record_failure(self, run_id: str, error: Exception) -> None:
        self.ledger.add_message(
            AgentMessage(
                message_id=stable_id("message", f"{run_id}:failure:{error}"),
                run_id=run_id,
                sender=AgentName.SUPERVISOR,
                recipient=AgentName.SUPERVISOR,
                summary=(
                    "Supervisor stopped the run after an unrecoverable "
                    "stage failure."
                ),
                recommendation=(
                    "Inspect the failed task and retry after correcting input."
                ),
                confidence=1.0,
            )
        )
        self.ledger.set_run_status(run_id, RunStatus.FAILED)

    async def chat(self, run_id: str, message: str) -> AgentArtifact:
        run = self.ledger.get_run(run_id)
        reply = await _chat_reply(
            run.question,
            run.status.value,
            self.ledger.list_checkpoints(run_id),
            message,
        )
        response = artifact(
            run_id,
            ArtifactKind.CHAT,
            AgentName.SUPERVISOR,
            {"user": message, "assistant": reply},
        )
        self.ledger.add_artifact(response)
        return response


async def _chat_reply(
    question: str,
    status: str,
    checkpoints: list[DecisionCheckpoint],
    message: str,
) -> str:
    fallback = (
        f"已记录你的指令。当前运行状态为 {status}；"
        "高影响选择请在开放的 checkpoint 中确认。"
    )
    try:
        client = ModelRouter.from_environment().client(ReasoningProfile.CHAT)
        return (
            await client.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Answer briefly in Chinese. "
                            "Do not claim unobserved facts."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "question": question,
                                "run_status": status,
                                "checkpoints": [
                                    item.model_dump(mode="json")
                                    for item in checkpoints
                                ],
                                "user_message": message,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.2,
                max_tokens=500,
            )
        ).strip()
    except Exception:
        return fallback


def _recommendation(outputs: list[AgentArtifact]) -> str | None:
    for output in outputs:
        value = output.payload.get("recommendation") or output.payload.get(
            "next_action"
        )
        if isinstance(value, str):
            return value
    return None
