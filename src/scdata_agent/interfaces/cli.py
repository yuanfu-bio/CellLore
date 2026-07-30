"""Command-line interface for the first evidence-first discovery vertical slice."""

from __future__ import annotations

import asyncio
from collections import Counter
import inspect
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from scdata_agent.application.agents.supervisor import MultiAgentSupervisor
from scdata_agent.domain.assets import build_asset_bundle
from scdata_agent.application.collaboration import CollaborationLedger
from scdata_agent.infrastructure.persistence.evidence_ledger import EvidenceLedger
from scdata_agent.infrastructure.llm.stepfun import StepFunChatClient, StepFunSettings
from scdata_agent.domain.models import DataRole
from scdata_agent.domain.requirements import QueryPlanner
from scdata_agent.infrastructure.sources.ena import ENAAssetResolver
from scdata_agent.domain.transfer import TransferPlanner
from scdata_agent.application.discovery import default_corpus_workflow, default_workflow
from scdata_agent.application.research_planning import configured_research_planner
from scdata_agent.infrastructure.persistence.catalog import Catalog


app = typer.Typer(
    no_args_is_help=True,
    help="Evidence-first public scATAC/multiome data discovery agent.",
)
console = Console()


@app.command("agent-run")
def agent_run(
    question: Annotated[str, typer.Argument(help="Natural-language research question.")],
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/scdata_agent.sqlite"
    ),
    max_results: Annotated[
        int, typer.Option("--max-results", "-n", min=1, max=100)
    ] = 20,
    use_llm: Annotated[
        bool,
        typer.Option("--llm/--no-llm", help="Use configured model routing."),
    ] = True,
    data_root: Annotated[
        Path, typer.Option("--data-root", help="Raw and curated data root.")
    ] = Path("data"),
    max_download_bytes: Annotated[
        int | None, typer.Option("--max-download-bytes", min=1)
    ] = None,
    evaluation_identifier: Annotated[
        list[str] | None,
        typer.Option("--evaluation-id", help="Evaluation Vault identifier; repeatable."),
    ] = None,
    approved_license_id: Annotated[
        list[str] | None,
        typer.Option("--approved-license", help="Approved training license ID; repeatable."),
    ] = None,
    enable_fulltext: Annotated[
        bool, typer.Option("--fulltext/--no-fulltext")
    ] = True,
    remote_probe: Annotated[
        bool, typer.Option("--remote-probe/--no-remote-probe")
    ] = True,
    trace_mode: Annotated[str, typer.Option("--trace-mode", help="local or langsmith.")] = "local",
) -> None:
    """Run evidence, acquisition, materialization, QC, and governance agents."""
    ledger = CollaborationLedger(database)
    run = ledger.create_run(
        question,
        {
            "max_results": max_results,
            "use_llm": use_llm,
            "data_root": str(data_root),
            "max_download_bytes": max_download_bytes,
            "evaluation_identifiers": evaluation_identifier or [],
            "enable_fulltext_enrichment": enable_fulltext,
            "remote_probe_enabled": remote_probe,
            "approved_license_ids": approved_license_id or [],
        },
    )
    supervisor = MultiAgentSupervisor(ledger, str(database), trace_mode=trace_mode)
    asyncio.run(supervisor.execute(run.run_id))
    payload = {
        "run": ledger.get_run(run.run_id).model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in ledger.list_tasks(run.run_id)],
        "checkpoints": [item.model_dump(mode="json") for item in ledger.list_checkpoints(run.run_id)],
    }
    console.print_json(json.dumps(payload, ensure_ascii=False))


@app.command("resume")
def resume(
    checkpoint_id: Annotated[str, typer.Argument(help="Open decision checkpoint ID.")],
    option: Annotated[str, typer.Option("--option", help="Selected option ID.")] = "plan_only",
    database: Annotated[Path, typer.Option("--database", "-d")] = Path("var/scdata_agent.sqlite"),
    trace_mode: Annotated[str, typer.Option("--trace-mode")] = "local",
) -> None:
    """Resume a persisted LangGraph run after a human decision."""
    supervisor = MultiAgentSupervisor(CollaborationLedger(database), str(database), trace_mode=trace_mode)
    checkpoint = asyncio.run(supervisor.resume_checkpoint(checkpoint_id, {"option": option}))
    console.print_json(checkpoint.model_dump_json())


@app.command("graph-state")
def graph_state(
    run_id: Annotated[str, typer.Argument(help="LangGraph thread/run ID.")],
    database: Annotated[Path, typer.Option("--database", "-d")] = Path("var/scdata_agent.sqlite"),
    trace_mode: Annotated[str, typer.Option("--trace-mode")] = "local",
) -> None:
    """Inspect the persisted LangGraph state for a run."""
    supervisor = MultiAgentSupervisor(CollaborationLedger(database), str(database), trace_mode=trace_mode)
    console.print_json(json.dumps(asyncio.run(supervisor.graph_state(run_id)), ensure_ascii=False, default=str))


@app.command("serve")
def serve(
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/scdata_agent.sqlite"
    ),
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
) -> None:
    """Start the local API, SSE event stream, and HTMX control room."""
    import uvicorn

    from scdata_agent.interfaces.api import create_app

    uvicorn.run(create_app(database), host=host, port=port)


@app.command("llm-check")
def llm_check() -> None:
    """Verify StepFun configuration without sending a billable request."""
    settings = StepFunSettings.from_environment()
    console.print(
        "StepFun configuration loaded: "
        f"model={settings.model}, base_url={settings.base_url}, "
        f"reasoning_effort={settings.reasoning_effort}"
    )


@app.command("llm-ping")
def llm_ping() -> None:
    """Send one small billable request to verify the StepFun connection."""
    settings = StepFunSettings.from_environment()
    client = StepFunChatClient(settings)
    reply = asyncio.run(
        client.complete(
            [{"role": "user", "content": "Reply with exactly: configured"}],
            max_tokens=128,
        )
    )
    console.print(reply)


@app.command()
def compile(
    question: Annotated[str, typer.Argument(help="Natural-language research question.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
    use_llm: Annotated[
        bool,
        typer.Option("--llm/--no-llm", help="Use configured StepFun semantic parsing."),
    ] = True,
) -> None:
    """Understand a research question and compile its executable discovery contract."""
    research_plan = asyncio.run(configured_research_planner(use_llm).plan(question))
    requirement = research_plan.executable_contracts[0]
    query_plan = QueryPlanner().plan(requirement)
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "research_plan": research_plan.model_dump(mode="json"),
                    "requirement": requirement.model_dump(mode="json"),
                    "query_plan": query_plan.model_dump(mode="json"),
                    "planning_readiness": research_plan.readiness,
                    "capability_gaps": research_plan.capability_gaps,
                },
                ensure_ascii=False,
            )
        )
        return
    console.print("[bold]Research intent[/bold]")
    console.print(research_plan.intent.intent_summary)
    console.print("\n[bold]Recommended data and resources[/bold]")
    for need in research_plan.data_needs:
        console.print(f"- {need.priority}: {need.name} ({need.execution_status}) — {need.rationale}")
    for need in research_plan.resource_needs:
        console.print(f"- {need.priority}: {need.resource_type} ({need.execution_status}) — {need.purpose}")
    if research_plan.assumptions:
        console.print("\n[bold]Assumptions[/bold]")
        for assumption in research_plan.assumptions:
            console.print(f"- {assumption}")
    if research_plan.capability_gaps:
        console.print("\n[bold]Capability gaps[/bold]")
        for gap in research_plan.capability_gaps:
            console.print(f"- {gap}")
    console.print("\n[bold]Executable query plan[/bold]")
    for query in query_plan.queries:
        console.print(f"- {query.source}: {query.query}")


@app.command()
def discover(
    question: Annotated[str, typer.Argument(help="Natural-language research question.")],
    database: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite evidence-ledger path.",
        ),
    ] = Path("var/scdata_agent.sqlite"),
    max_results: Annotated[
        int,
        typer.Option("--max-results", "-n", min=1, max=100),
    ] = 20,
    json_output: Annotated[
        Path | None,
        typer.Option("--json-output", help="Optional path for full JSON result."),
    ] = None,
    use_llm: Annotated[
        bool,
        typer.Option("--llm/--no-llm", help="Use configured StepFun semantic parsing."),
    ] = True,
) -> None:
    """Run Europe PMC + GEO + SRA discovery and persist evidence."""
    result = asyncio.run(
        default_workflow(str(database), use_llm=use_llm).run(
            question=question,
            max_results=max_results,
        )
    )
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            result.model_dump_json(indent=2),
            encoding="utf-8",
        )

    reports = {
        report.candidate_id: report for report in result.feasibility_reports
    }
    table = Table(title=f"Discovery run {result.run_id}")
    table.add_column("Candidate")
    table.add_column("Title", overflow="fold")
    table.add_column("Identifiers")
    table.add_column("Modalities")
    table.add_column("Decision")
    for candidate in result.candidates:
        report = reports[candidate.candidate_id]
        table.add_row(
            candidate.candidate_id,
            candidate.title,
            ", ".join(identifier.value for identifier in candidate.identifiers)
            or "—",
            ", ".join(modality.value for modality in candidate.modalities) or "unknown",
            report.decision.value,
        )
    console.print(table)
    console.print(
        f"records={len(result.source_records)} "
        f"candidates={len(result.candidates)} errors={len(result.errors)}"
    )
    for error in result.errors:
        console.print(f"[yellow]warning:[/yellow] {error}")


@app.command("list-runs")
def list_runs(
    database: Annotated[
        Path, typer.Option("--database", "-d")
    ] = Path("var/scdata_agent.sqlite"),
) -> None:
    """List discovery runs stored in the evidence ledger."""
    ledger = EvidenceLedger(database)
    table = Table(title="Discovery runs")
    table.add_column("Run ID")
    table.add_column("Status")
    table.add_column("Created")
    table.add_column("Question", overflow="fold")
    for row in ledger.list_runs():
        table.add_row(
            row["run_id"],
            row["status"],
            row["created_at"],
            row["question"],
        )
    console.print(table)


@app.command("show-run")
def show_run(
    run_id: Annotated[str, typer.Argument()],
    database: Annotated[
        Path, typer.Option("--database", "-d")
    ] = Path("var/scdata_agent.sqlite"),
) -> None:
    """Show persisted requirement, query plan, and candidates."""
    ledger = EvidenceLedger(database)
    payload = {
        "requirement": ledger.load_requirement(run_id).model_dump(mode="json"),
        "query_plan": ledger.load_plan(run_id).model_dump(mode="json"),
        "candidates": [
            candidate.model_dump(mode="json")
            for candidate in ledger.load_candidates(run_id)
        ],
    }
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))



@app.command("resolve-assets")
def resolve_assets(
    run_id: Annotated[str, typer.Argument(help="Persisted discovery run ID.")],
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/scdata_agent.sqlite"
    ),
    candidate_id: Annotated[
        str | None,
        typer.Option("--candidate-id", help="Resolve only one candidate."),
    ] = None,
    destination: Annotated[
        Path,
        typer.Option(
            "--destination",
            help="Logical download destination; files are not downloaded.",
        ),
    ] = Path("data/raw"),
    json_output: Annotated[
        Path | None,
        typer.Option("--json-output", help="Optional asset-plan JSON path."),
    ] = None,
    expand_bioproject: Annotated[
        bool,
        typer.Option(
            "--expand-bioproject",
            help="Allow potentially very large BioProject expansion.",
        ),
    ] = False,
    max_assets: Annotated[
        int, typer.Option("--max-assets", min=1, max=10000)
    ] = 500,
) -> None:
    """Resolve ENA FASTQ manifests and create read-only transfer plans."""
    ledger = EvidenceLedger(database)
    candidates = ledger.load_candidates(run_id)
    if candidate_id is not None:
        candidates = [
            candidate
            for candidate in candidates
            if candidate.candidate_id == candidate_id
        ]
        if not candidates:
            raise typer.BadParameter(
                f"Candidate {candidate_id!r} is not present in run {run_id!r}."
            )

    async def resolve_all():
        resolver = ENAAssetResolver(
            expand_bioproject=expand_bioproject,
            max_assets=max_assets,
        )
        return await asyncio.gather(
            *(resolver.resolve(candidate) for candidate in candidates)
        )

    payload = []
    for result in asyncio.run(resolve_all()):
        bundle = build_asset_bundle(
            result.candidate_id,
            result.assets,
            bundle_type="raw_reads",
            required_roles=[DataRole.RAW_READS],
        )
        plan = TransferPlanner().plan(bundle, result.assets, destination)
        payload.append(
            {
                "resolution": result.model_dump(mode="json"),
                "bundle": bundle.model_dump(mode="json"),
                "transfer_plan": plan.model_dump(mode="json"),
            }
        )

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(rendered, encoding="utf-8")
    table = Table(title=f"Asset resolution for {run_id}")
    table.add_column("Candidate")
    table.add_column("Assets", justify="right")
    table.add_column("Complete")
    table.add_column("Expected bytes", justify="right")
    table.add_column("Errors", justify="right")
    for item in payload:
        resolution = item["resolution"]
        bundle = item["bundle"]
        transfer_plan = item["transfer_plan"]
        table.add_row(
            resolution["candidate_id"],
            str(len(resolution["assets"])),
            str(bundle["complete"]),
            str(transfer_plan["total_expected_bytes"] or "—"),
            str(len(resolution["errors"])),
        )
    console.print(table)
    if json_output is not None:
        console.print(f"Full plan: {json_output}")


@app.command("corpus-discover")
def corpus_discover(
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/scatac_corpus.sqlite"
    ),
    max_results_per_query: Annotated[
        int, typer.Option("--max-results-per-query", "-n", min=1, max=500)
    ] = 200,
) -> None:
    """Discover a broad public scATAC and paired multi-omics corpus."""
    question = (
        "public scATAC-seq and snATAC-seq; RNA+ATAC multiome; matched "
        "scRNA-seq and scATAC-seq; ATAC+protein and RNA+ATAC+protein multiomics"
    )
    result = asyncio.run(
        default_corpus_workflow(str(database)).run(
            question, max_results=max_results_per_query
        )
    )
    modality_counts = Counter(
        modality.value
        for candidate in result.candidates
        for modality in candidate.modalities
    )
    pairing_counts = Counter(
        profile.pairing_level.value
        for candidate in result.candidates
        for profile in candidate.assay_profiles
    )
    technology_counts = Counter(
        profile.label
        for candidate in result.candidates
        for profile in candidate.assay_profiles
    )
    console.print_json(json.dumps({
        "run_id": result.run_id,
        "database": str(database),
        "query_count": len(result.query_plan.queries),
        "source_record_count": len(result.source_records),
        "candidate_count": len(result.candidates),
        "modality_counts": dict(modality_counts),
        "pairing_level_counts": dict(pairing_counts),
        "technology_counts": dict(technology_counts),
        "errors": result.errors,
        "uncovered_sources": result.query_plan.uncovered_sources,
    }, ensure_ascii=False))
