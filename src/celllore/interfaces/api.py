"""Local FastAPI control surface for the multi-agent system."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from celllore.application.agents.supervisor import MultiAgentSupervisor
from celllore.application.collaboration import CollaborationLedger
from celllore.application.curation_catalog import (
    build_curation_catalog,
    write_curation_catalog_xlsx,
)
from celllore.application.orchestration_v2 import workflow_topology
from celllore.domain.curation.profiles import PROFILES, profile_by_id

_STATUS_COLOR = {
    "queued": "gray", "running": "blue", "waiting_for_decision": "amber",
    "completed": "green", "failed": "red", "cancelled": "gray",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["status_color"] = lambda status: _STATUS_COLOR.get(status, "gray")

_FRONTEND_DIST = Path(__file__).resolve().parent / "static" / "ui"
_RUN_ID_ONLY = re.compile(r"agent_run_[0-9a-f]{8,}", re.IGNORECASE)

class CreateRunRequest(BaseModel):
    question: str = Field(min_length=3)
    max_results: int = Field(default=20, ge=1, le=100)
    use_llm: bool = True
    profile_id: str | None = None
    sources: list[Literal["geo"]] = Field(default_factory=lambda: ["geo"])
    data_root: str = "data"
    max_download_bytes: int | None = Field(default=None, ge=1)
    evaluation_identifiers: list[str] = Field(default_factory=list)
    approved_license_candidate_ids: list[str] = Field(default_factory=list)
    allow_qc_warn: bool = False
    approved_license_ids: list[str] = Field(default_factory=list)
    # When false, an archive's published terms of use stand in for a missing
    # per-record licence, and the basis is recorded on the preflight report.
    strict_license: bool = False
    # A permissive acquisition policy: public access remains mandatory, while
    # an unresolved per-record licence is recorded rather than blocking a
    # recommendation. Restricted licences remain hard blockers.
    allow_unknown_license: bool = False
    # Per-repository cap on files enumerated for one dataset. Exceeding it
    # marks the bundle inventory:truncated rather than silently under-reporting.
    max_assets: int = Field(default=500, ge=1, le=100_000)
    # deep runs the planner's 4-stage sequential analysis (~600s budget) so
    # the model completes its reasoning instead of racing two calls against a
    # 30s budget each and falling back to a generic template on timeout.
    planner_mode: Literal["fast", "deep"] = "deep"
    enable_fulltext_enrichment: bool = True
    max_fulltexts: int = Field(default=5, ge=0, le=100)
    remote_probe_enabled: bool = True
    remote_probe_concurrency: int = Field(default=8, ge=1, le=64)
    remote_probe_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    remote_probe_max_attempts: int = Field(default=2, ge=1, le=5)

    @field_validator("question")
    @classmethod
    def validate_research_question(cls, value: str) -> str:
        question = value.strip()
        if len(question) < 3:
            raise ValueError("Research question must contain at least 3 non-space characters.")
        if _RUN_ID_ONLY.fullmatch(question):
            raise ValueError(
                "A CellLore run ID cannot be used as a research question. "
                "Open it from Recent Projects, or enter the dataset you want to find."
            )
        return question

class ResolveCheckpointRequest(BaseModel):
    resolution: dict[str, Any]

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)

def create_app(database_path: str | Path = "var/celllore.sqlite") -> FastAPI:
    app = FastAPI(title="CellLore", version="0.2.0")
    ledger = CollaborationLedger(database_path)
    supervisor = MultiAgentSupervisor(ledger, str(database_path), trace_mode=os.getenv("SCDATA_AGENT_TRACE_MODE"))
    app.state.ledger = ledger
    app.state.supervisor = supervisor
    app.state.background_tasks = {}

    # Serve Vite's hashed production assets when a frontend build is present.
    if (_FRONTEND_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="frontend-assets")


    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", status_code=202)
    async def create_run(request: CreateRunRequest) -> dict[str, Any]:
        if request.profile_id is not None:
            try:
                profile_by_id(request.profile_id)
            except KeyError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        run = ledger.create_run(request.question, {
            "max_results": request.max_results,
            "use_llm": request.use_llm,
            "profile_id": request.profile_id,
            "sources": request.sources,
            "data_root": request.data_root,
            "max_download_bytes": request.max_download_bytes,
            "evaluation_identifiers": request.evaluation_identifiers,
            "approved_license_candidate_ids": request.approved_license_candidate_ids,
            "allow_qc_warn": request.allow_qc_warn,
            "enable_fulltext_enrichment": request.enable_fulltext_enrichment,
            "max_fulltexts": request.max_fulltexts,
            "approved_license_ids": request.approved_license_ids,
            "strict_license": request.strict_license,
            "allow_unknown_license": request.allow_unknown_license,
            "max_assets": request.max_assets,
            "planner_mode": request.planner_mode,
            "remote_probe_enabled": request.remote_probe_enabled,
            "remote_probe_concurrency": request.remote_probe_concurrency,
            "remote_probe_timeout_seconds": request.remote_probe_timeout_seconds,
            "remote_probe_max_attempts": request.remote_probe_max_attempts,
        })
        task = asyncio.create_task(supervisor.execute(run.run_id), name=f"celllore:{run.run_id}")
        app.state.background_tasks[run.run_id] = task
        return run.model_dump(mode="json")

    @app.get("/curation-profiles")
    async def curation_profiles() -> list[dict[str, str]]:
        return [{"profile_id": profile.profile_id, "label": profile.label,
                 "scientific_objective": profile.scientific_objective} for profile in PROFILES]

    @app.get("/runs")
    async def list_runs() -> list[dict[str, Any]]:
        return [run.model_dump(mode="json") for run in ledger.list_runs()]

    @app.get("/catalog/records")
    async def catalog_records(
        record_type: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return supervisor.catalog.list_records(
            record_type=record_type, run_id=run_id
        )

    @app.get("/corpus-snapshots")
    async def corpus_snapshots(
        approved_only: bool = False,
    ) -> list[dict[str, Any]]:
        records = supervisor.catalog.list_records(
            record_type="corpus_snapshot"
        )
        if approved_only:
            records = [
                item for item in records if item["payload"].get("approved")
            ]
        return records

    @app.get("/ui/runs/{run_id}")
    async def run_page(request: Request, run_id: str):
        if (_FRONTEND_DIST / "index.html").is_file():
            return FileResponse(_FRONTEND_DIST / "index.html")
        try:
            payload = _run_payload(ledger, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return templates.TemplateResponse(
            request, "run.html", {"run_id": run_id, "initial_data": payload}
        )

    @app.get("/ui/{path:path}", include_in_schema=False)
    async def frontend_page(path: str):
        """Return the SPA entry point for browser-routed UI pages."""
        if (_FRONTEND_DIST / "index.html").is_file():
            return FileResponse(_FRONTEND_DIST / "index.html")
        raise HTTPException(status_code=404, detail=f"UI page not found: {path}")

    @app.get("/runs/{run_id}/graph-state")
    async def graph_state(run_id: str) -> dict[str, Any]:
        try:
            return await supervisor.graph_state(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return _run_payload(ledger, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/runs/{run_id}/curation-catalog")
    async def curation_catalog(run_id: str) -> dict[str, Any]:
        try:
            ledger.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        catalog = build_curation_catalog(run_id, ledger.list_artifacts(run_id))
        return {
            "studies": catalog.studies,
            "pair_units": catalog.pair_units,
            "files": catalog.files,
            "audit_hold": catalog.audit_hold,
            "policy": catalog.policy,
        }

    @app.get("/runs/{run_id}/curation-catalog.xlsx")
    async def curation_catalog_xlsx(run_id: str) -> FileResponse:
        try:
            ledger.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        catalog = build_curation_catalog(run_id, ledger.list_artifacts(run_id))
        out_path = Path(tempfile.gettempdir()) / f"curation_catalog_{run_id}.xlsx"
        write_curation_catalog_xlsx(catalog, str(out_path))
        return FileResponse(
            out_path,
            filename=f"curation_catalog_{run_id}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            cursor = after
            while True:
                for event in ledger.events_since(run_id, cursor):
                    cursor = event["event_id"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                run = ledger.get_run(run_id)
                if run.status.value in {"completed", "failed", "cancelled"}:
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, str]:
        try:
            supervisor.cancel(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        task = app.state.background_tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        return {
            "run_id": run_id,
            "status": ledger.get_run(run_id).status.value,
        }

    @app.post("/checkpoints/{checkpoint_id}/resolve")
    async def resolve_checkpoint(checkpoint_id: str, request: ResolveCheckpointRequest) -> dict[str, Any]:
        try:
            checkpoint = await supervisor.resume_checkpoint(checkpoint_id, request.resolution)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return checkpoint.model_dump(mode="json")

    @app.post("/runs/{run_id}/chat")
    async def chat(run_id: str, request: ChatRequest) -> dict[str, Any]:
        try:
            response = await supervisor.chat(run_id, request.message)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return response.model_dump(mode="json")

    @app.get("/")
    async def dashboard(request: Request):
        if (_FRONTEND_DIST / "index.html").is_file():
            return FileResponse(_FRONTEND_DIST / "index.html")
        return templates.TemplateResponse(
            request, "dashboard.html", {"runs": ledger.list_runs()[:20]}
        )

    return app


def _workflow_topology_trace(ledger: CollaborationLedger, run_id: str) -> dict[str, Any]:
    topology = workflow_topology()
    canonical_node_ids = {node["id"] for node in topology["nodes"]}
    executed_nodes: set[str] = set()
    task_node_ids: dict[str, str] = {}
    transitions: set[tuple[str, str, str | None]] = set()
    for event in ledger.events_since(run_id):
        payload = event["payload"]
        if event["type"] in {"graph_node_started", "graph_node_finished", "graph_node_failed", "graph_topology_node"}:
            node_id = payload.get("node")
            if node_id in canonical_node_ids:
                executed_nodes.add(node_id)
            task_id = payload.get("task_id")
            if node_id in canonical_node_ids and task_id:
                task_node_ids[task_id] = node_id
        if event["type"] == "graph_topology_transition":
            source, target = payload.get("source"), payload.get("target")
            if source and target:
                executed_nodes.update({source, target})
                transitions.add((source, target, payload.get("condition")))
    for checkpoint in ledger.list_checkpoints(run_id):
        if checkpoint.kind == "download_selection":
            executed_nodes.add("download_approval")
            option = (checkpoint.resolution or {}).get("option")
            if option:
                route = (
                    "transfer"
                    if option in {"recommended", "all_with_unknown_size"}
                    else "stop"
                )
                transitions.add(
                    (
                        "download_approval",
                        "transfer_execution" if route == "transfer" else "finalize",
                        route,
                    )
                )
        elif checkpoint.kind == "corpus_snapshot":
            executed_nodes.add("corpus_approval")
    executed_edges = {
        edge["id"]
        for edge in topology["edges"]
        if (edge["source"], edge["target"], edge.get("condition")) in transitions
        or (not edge.get("condition") and edge["source"] in executed_nodes and edge["target"] in executed_nodes)
    }
    return {
        **topology,
        "executed_node_ids": sorted(executed_nodes),
        "executed_edge_ids": sorted(executed_edges),
        "task_node_ids": task_node_ids,
        "trace_available": bool(task_node_ids or transitions),
        "trace_complete": len(task_node_ids) >= len(ledger.list_tasks(run_id)),
    }

def _run_payload(ledger: CollaborationLedger, run_id: str) -> dict[str, Any]:
    """Everything the run-monitoring panel needs, in one shape shared by the
    JSON API and the server-rendered page's initial hydration."""
    run = ledger.get_run(run_id)
    artifacts = ledger.list_artifacts(run_id)
    plan_artifacts = [item for item in artifacts if item.kind.value == "research_plan"]
    research_plan = plan_artifacts[-1].payload if plan_artifacts else None
    llm_calls = [
        {**item.payload, "created_at": item.created_at.isoformat()}
        for item in artifacts
        if item.kind.value == "llm_call"
    ]
    failed_tasks = [task for task in ledger.list_tasks(run_id) if task.status.value == "failed"]
    run_error = failed_tasks[-1].error if failed_tasks else None
    acquisition_artifacts = [item for item in artifacts if item.kind.value == "acquisition_plan"]
    acquisition_plan = acquisition_artifacts[-1].payload if acquisition_artifacts else None
    inventory_progress_events = [
        event
        for event in ledger.events_since(run_id)
        if event["type"] == "inventory_progress"
    ]
    return {
        "run": run.model_dump(mode="json"),
        "workflow_topology": _workflow_topology_trace(ledger, run_id),
        "run_error": run_error,
        "tasks": [task.model_dump(mode="json") for task in ledger.list_tasks(run_id)],
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "messages": [item.model_dump(mode="json") for item in ledger.list_messages(run_id)],
        "checkpoints": [item.model_dump(mode="json") for item in ledger.list_checkpoints(run_id)],
        "research_plan": research_plan,
        "planning_readiness": research_plan.get("readiness") if research_plan else None,
        "capability_gaps": research_plan.get("capability_gaps", []) if research_plan else [],
        "executable_contracts": research_plan.get("executable_contracts", []) if research_plan else [],
        "llm_calls": llm_calls,
        "acquisition_plan": acquisition_plan,
        "inventory_progress": (
            inventory_progress_events[-1]["payload"]
            if inventory_progress_events
            else None
        ),
    }


app = create_app(os.getenv("SCDATA_AGENT_DATABASE", "var/celllore.sqlite"))
