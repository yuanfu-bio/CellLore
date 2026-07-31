"""Local FastAPI + HTMX control surface for the multi-agent system."""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from scdata_agent.application.agents.supervisor import MultiAgentSupervisor
from scdata_agent.application.collaboration import CollaborationLedger


class CreateRunRequest(BaseModel):
    question: str = Field(min_length=3)
    max_results: int = Field(default=20, ge=1, le=100)
    use_llm: bool = True
    data_root: str = "data"
    max_download_bytes: int | None = Field(default=None, ge=1)
    evaluation_identifiers: list[str] = Field(default_factory=list)
    approved_license_candidate_ids: list[str] = Field(default_factory=list)
    allow_qc_warn: bool = False
    approved_license_ids: list[str] = Field(default_factory=list)
    # When false, an archive's published terms of use stand in for a missing
    # per-record licence, and the basis is recorded on the preflight report.
    strict_license: bool = False
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

class ResolveCheckpointRequest(BaseModel):
    resolution: dict[str, Any]

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)

def create_app(database_path: str | Path = "var/scdata_agent.sqlite") -> FastAPI:
    app = FastAPI(title="scdata-agent", version="0.2.0")
    ledger = CollaborationLedger(database_path)
    supervisor = MultiAgentSupervisor(ledger, str(database_path), trace_mode=os.getenv("SCDATA_AGENT_TRACE_MODE"))
    app.state.ledger = ledger
    app.state.supervisor = supervisor
    app.state.background_tasks = {}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", status_code=202)
    async def create_run(request: CreateRunRequest) -> dict[str, Any]:
        run = ledger.create_run(request.question, {
            "max_results": request.max_results,
            "use_llm": request.use_llm,
            "data_root": request.data_root,
            "max_download_bytes": request.max_download_bytes,
            "evaluation_identifiers": request.evaluation_identifiers,
            "approved_license_candidate_ids": request.approved_license_candidate_ids,
            "allow_qc_warn": request.allow_qc_warn,
            "enable_fulltext_enrichment": request.enable_fulltext_enrichment,
            "max_fulltexts": request.max_fulltexts,
            "approved_license_ids": request.approved_license_ids,
            "strict_license": request.strict_license,
            "max_assets": request.max_assets,
            "planner_mode": request.planner_mode,
            "remote_probe_enabled": request.remote_probe_enabled,
            "remote_probe_concurrency": request.remote_probe_concurrency,
            "remote_probe_timeout_seconds": request.remote_probe_timeout_seconds,
            "remote_probe_max_attempts": request.remote_probe_max_attempts,
        })
        task = asyncio.create_task(supervisor.execute(run.run_id), name=f"scdata-agent:{run.run_id}")
        app.state.background_tasks[run.run_id] = task
        return run.model_dump(mode="json")

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

    @app.get("/ui/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(run_id: str) -> str:
        try:
            return _run_html(ledger, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/runs/{run_id}/graph-state")
    async def graph_state(run_id: str) -> dict[str, Any]:
        try:
            return await supervisor.graph_state(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            run = ledger.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        artifacts = ledger.list_artifacts(run_id)
        plan_artifacts = [item for item in artifacts if item.kind.value == "research_plan"]
        research_plan = plan_artifacts[-1].payload if plan_artifacts else None
        return {
            "run": run.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in ledger.list_tasks(run_id)],
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
            "messages": [item.model_dump(mode="json") for item in ledger.list_messages(run_id)],
            "checkpoints": [item.model_dump(mode="json") for item in ledger.list_checkpoints(run_id)],
            "research_plan": research_plan,
            "planning_readiness": research_plan.get("readiness") if research_plan else None,
            "capability_gaps": research_plan.get("capability_gaps", []) if research_plan else [],
            "executable_contracts": research_plan.get("executable_contracts", []) if research_plan else [],
        }

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            cursor = after
            while True:
                for event in ledger.events_since(run_id, cursor):
                    cursor = event["event_id"]
                    yield f"id: {cursor}\\nevent: {event['type']}\\ndata: {json.dumps(event, ensure_ascii=False)}\\n\\n"
                run = ledger.get_run(run_id)
                if run.status.value in {"completed", "failed", "cancelled"}:
                    yield "event: end\\ndata: {}\\n\\n"
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
        return {"run_id": run_id, "status": "cancelled"}

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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _dashboard_html(ledger)
    return app

def _dashboard_html(ledger: CollaborationLedger) -> str:
    cards = "".join(f"<li><a href='/ui/runs/{run.run_id}'>{run.run_id}</a> · {run.status.value} · {run.question}</li>" for run in ledger.list_runs()[:20]) or "<li>No runs yet.</li>"
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>scdata-agent</title><script src='https://unpkg.com/htmx.org@2.0.4'></script><style>body{{font-family:system-ui;max-width:960px;margin:2rem auto;padding:0 1rem}} textarea{{width:100%;min-height:5rem}} button{{padding:.5rem 1rem}} code{{background:#eee;padding:.15rem}} .card{{border:1px solid #ddd;padding:1rem;margin:1rem 0;border-radius:.5rem}}</style></head><body>
<h1>scdata-agent multi-agent control room</h1><div class='card'><h2>Start a research run</h2>
<form onsubmit=\"event.preventDefault(); fetch('/runs', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:this.question.value,max_results:Number(this.max_results.value),use_llm:this.use_llm.checked}})}}).then(r=>r.json()).then(x=>document.querySelector('#result').textContent=JSON.stringify(x,null,2))\"><textarea name='question' placeholder='Describe your research question' required></textarea><br><label>Maximum results <input name='max_results' type='number' value='20' min='1' max='100'></label><label><input name='use_llm' type='checkbox' checked> Use configured LLM</label><button type='submit'>Run agents</button></form><pre id='result'></pre></div>
<div class='card'><h2>Recent runs</h2><ul>{cards}</ul></div><div class='card'><p>Use <code>GET /runs/{{run_id}}/events</code> for live SSE events, and <code>POST /runs/{{run_id}}/chat</code> for natural-language control messages.</p></div></body></html>"""


def _run_html(ledger: CollaborationLedger, run_id: str) -> str:
    run = ledger.get_run(run_id)
    tasks = ledger.list_tasks(run_id)
    artifacts = ledger.list_artifacts(run_id)
    checkpoints = ledger.list_checkpoints(run_id)
    task_rows = "".join(
        f"<li><b>{task.agent.value}</b> — {task.status.value} {('<small>' + task.error + '</small>') if task.error else ''}</li>"
        for task in tasks
    )
    artifact_rows = "".join(
        f"<li>{item.kind.value} · {item.producer.value}</li>" for item in artifacts
    )
    checkpoint_rows = "".join(
        f"<div class='checkpoint'><b>{item.title}</b><p>{item.recommendation}</p>"
        + "".join(
            f"<button onclick=\"resolveCheckpoint('{item.checkpoint_id}','{option['id']}')\">{option['label']}</button> "
            for option in item.options
        ) + "</div>"
        for item in checkpoints if item.status.value == "open"
    ) or "<p>No open decisions.</p>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>{run.run_id}</title>
<script src='https://unpkg.com/htmx.org@2.0.4'></script><style>body{{font-family:system-ui;max-width:960px;margin:2rem auto;padding:0 1rem}} .card{{border:1px solid #ddd;padding:1rem;margin:1rem 0;border-radius:.5rem}} textarea{{width:100%;min-height:4rem}} button{{padding:.4rem .8rem;margin:.2rem}} small{{color:#a00}}</style></head><body>
<a href='/'>← Runs</a><h1>Run {run.run_id}</h1><p><b>Status:</b> <span id='status'>{run.status.value}</span></p>
<div class='card'><h2>Agent tasks</h2><ul id='tasks'>{task_rows}</ul></div>
<div class='card'><h2>Artifacts</h2><ul>{artifact_rows}</ul></div>
<div class='card'><h2>Decision checkpoint</h2><div id='checkpoint'>{checkpoint_rows}</div></div>
<div class='card'><h2>Talk to the Supervisor</h2><form onsubmit=\"sendChat(event)\"><textarea id='message' placeholder='Ask about the run, priorities, or the current decision'></textarea><br><button>Send</button></form><pre id='reply'></pre></div>
<script>
const runId={json.dumps(run_id)};
function sendChat(event){{event.preventDefault();fetch(`/runs/${{runId}}/chat`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:document.querySelector('#message').value}})}}).then(r=>r.json()).then(x=>document.querySelector('#reply').textContent=x.payload.assistant)}}
function resolveCheckpoint(id, option){{fetch(`/checkpoints/${{id}}/resolve`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{resolution:{{option}}}})}}).then(()=>location.reload())}}
const stream=new EventSource(`/runs/${{runId}}/events`);stream.onmessage=()=>setTimeout(()=>location.reload(),300);</script></body></html>"""

app = create_app(os.getenv("SCDATA_AGENT_DATABASE", "var/scdata_agent.sqlite"))
