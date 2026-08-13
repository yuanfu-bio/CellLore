"""Command-line interface for the first evidence-first discovery vertical slice."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from celllore.application.agents.supervisor import MultiAgentSupervisor
from celllore.application.collaboration import CollaborationLedger
from celllore.application.discovery import default_corpus_workflow
from celllore.application.manifest_repair import (
    RepairNotAuthorized,
    classify_repair_candidates,
    execute_repair,
)
from celllore.application.orchestration_manifest import (
    ManifestRunSupervisor,
    supervisor_for_run,
)
from celllore.application.research_planning import configured_research_planner
from celllore.domain.manifest import (
    IssueSeverity,
    ManifestDocument,
    ManifestFormatError,
    ManifestIssue,
    ManifestRow,
    filter_manifest_issues,
    filter_manifest_rows,
    summarize_issues,
)
from celllore.domain.requirements import QueryPlanner
from celllore.infrastructure.llm.router import ModelRouter, ReasoningProfile
from celllore.infrastructure.manifest_reader import read_manifest
from celllore.infrastructure.persistence.download_store import DownloadStateStore
from celllore.infrastructure.persistence.evidence_ledger import EvidenceLedger

app = typer.Typer(
    no_args_is_help=True,
    help="CellLore: evidence-first public scATAC/multiome data discovery agent.",
)
console = Console()


@app.command("manifest-check")
def manifest_check(
    manifest: Annotated[Path, typer.Argument(help="Curated manifest CSV/TSV/XLSX path.")],
    dialect: Annotated[
        str | None,
        typer.Option("--dialect", help="Manifest dialect ID; auto-detected when omitted."),
    ] = None,
    dataset: Annotated[
        list[str] | None, typer.Option("--dataset", help="Keep only these dataset IDs; repeatable.")
    ] = None,
    pair_unit: Annotated[
        list[str] | None,
        typer.Option("--pair-unit", help="Keep only these pair unit IDs; repeatable."),
    ] = None,
    stage: Annotated[
        list[str] | None, typer.Option("--stage", help="Keep only these download stages; repeatable.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Parse and audit a curated manifest. Read-only: no run, no network."""
    try:
        document = read_manifest(manifest, dialect)
    except ManifestFormatError as error:
        raise typer.BadParameter(str(error)) from error
    rows = filter_manifest_rows(document.rows, dataset, pair_unit, stage)
    issues = filter_manifest_issues(document.issues, document.rows, rows)
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "source_path": document.source_path,
                    "source_sha256": document.source_sha256,
                    "dialect_id": document.dialect_id,
                    "row_count": len(rows),
                    "pair_unit_count": len({row.pair_unit_key for row in rows}),
                    "issue_counts": summarize_issues(issues),
                    "rows": [row.model_dump(mode="json") for row in rows],
                    "issues": [item.model_dump(mode="json") for item in issues],
                },
                ensure_ascii=False,
            )
        )
        return
    _render_manifest_report(document, rows, issues)
    if any(item.severity == IssueSeverity.ERROR for item in issues):
        raise typer.Exit(code=2)


@app.command("manifest-run")
def manifest_run(
    manifest: Annotated[Path, typer.Argument(help="Curated manifest CSV/TSV/XLSX path.")],
    dialect: Annotated[
        str | None,
        typer.Option("--dialect", help="Manifest dialect ID; auto-detected when omitted."),
    ] = None,
    dataset: Annotated[
        list[str] | None, typer.Option("--dataset", help="Keep only these dataset IDs; repeatable.")
    ] = None,
    pair_unit: Annotated[
        list[str] | None,
        typer.Option("--pair-unit", help="Keep only these pair unit IDs; repeatable."),
    ] = None,
    stage: Annotated[
        list[str] | None, typer.Option("--stage", help="Keep only these download stages; repeatable.")
    ] = None,
    include_portal: Annotated[
        bool,
        typer.Option(
            "--include-portal/--direct-only",
            help=(
                "A portal row is a click-through landing page, not a file; "
                "including it only seeds a dead-end manual_review bundle "
                "(non-negotiable #4: no automated click-through resolution)."
            ),
        ),
    ] = False,
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/celllore.sqlite"
    ),
    data_root: Annotated[
        Path, typer.Option("--data-root", help="Raw and curated data root.")
    ] = Path("data"),
    max_download_bytes: Annotated[
        int | None, typer.Option("--max-download-bytes", min=1)
    ] = None,
    remote_probe: Annotated[
        bool,
        typer.Option(
            "--remote-probe/--no-remote-probe",
            help="HEAD/Range-probe declared URLs before preflight. Metadata only.",
        ),
    ] = True,
    require_standard_object: Annotated[
        bool,
        typer.Option(
            "--require-standard-object/--allow-native-package",
            help="Native mtx/tsv.gz/fragments packages are admissible by default (S4).",
        ),
    ] = False,
    approved_license_candidate: Annotated[
        list[str] | None,
        typer.Option(
            "--approved-license-candidate",
            help="Candidate ID to admit on repository-policy licence terms; repeatable.",
        ),
    ] = None,
    downloader: Annotated[
        str,
        typer.Option(
            "--downloader",
            help="auto prefers aria2 when aria2c is on PATH, else httpx. aria2 requires the binary.",
        ),
    ] = "auto",
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="aria2 concurrency/segmentation profile: stable, balanced, perfile, or aggressive.",
        ),
    ] = "balanced",
    contact_email: Annotated[
        str | None,
        typer.Option(
            "--contact-email",
            help="Sent to NCBI in the aria2 User-Agent. Defaults to $NCBI_CONTACT_EMAIL; optional.",
            envvar="NCBI_CONTACT_EMAIL",
        ),
    ] = None,
    manifest_layout: Annotated[
        bool,
        typer.Option(
            "--manifest-layout/--bundle-layout",
            help="Lay files out at the curator's relative_path instead of <bundle_id>/<filename>.",
        ),
    ] = True,
    trace_mode: Annotated[str, typer.Option("--trace-mode", help="local or langsmith.")] = "local",
) -> None:
    """Run a curated manifest through the same downstream path as a discovered one."""
    try:
        document = read_manifest(manifest, dialect)
    except ManifestFormatError as error:
        raise typer.BadParameter(str(error)) from error
    if document.has_errors:
        raise typer.BadParameter(
            f"{manifest} has parse errors; run `manifest-check` first and fix them."
        )
    ledger = CollaborationLedger(database)
    run = ledger.create_run(
        f"Curated manifest ingestion: {document.source_path}",
        {
            "source": "manifest",
            "manifest_path": str(manifest),
            "manifest_dialect": document.dialect_id,
            "manifest_filters": {
                "dataset": dataset or [],
                "pair_unit": pair_unit or [],
                "stage": stage or [],
            },
            "include_portal": include_portal,
            "data_root": str(data_root),
            "max_download_bytes": max_download_bytes,
            "remote_probe_enabled": remote_probe,
            "require_standard_object": require_standard_object,
            "approved_license_candidate_ids": approved_license_candidate or [],
            "downloader": downloader,
            "profile": profile,
            "contact_email": contact_email,
            "honour_manifest_layout": manifest_layout,
        },
    )
    supervisor = ManifestRunSupervisor(ledger, str(database), trace_mode=trace_mode)
    asyncio.run(supervisor.execute(run.run_id))
    payload = {
        "run": ledger.get_run(run.run_id).model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in ledger.list_tasks(run.run_id)],
        "checkpoints": [item.model_dump(mode="json") for item in ledger.list_checkpoints(run.run_id)],
    }
    console.print_json(json.dumps(payload, ensure_ascii=False))


@app.command("manifest-status")
def manifest_status(
    run_id: Annotated[str, typer.Argument(help="CellLore run ID.")],
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/celllore.sqlite"
    ),
) -> None:
    """Summarize per-file download state for a run."""
    store = DownloadStateStore(database)
    summary = store.summary(run_id)
    if not summary:
        console.print(f"No download_file_state records for {run_id}.")
        return
    table = Table(title=f"Download state: {run_id}")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for status, count in sorted(summary.items()):
        table.add_row(status, str(count))
    console.print(table)


@app.command("manifest-repair")
def manifest_repair(
    run_id: Annotated[str, typer.Argument(help="CellLore run ID.")],
    run: Annotated[
        bool,
        typer.Option(
            "--run",
            help="Actually retry the RETRYABLE_CLASSES subset. Default is a dry-run report.",
        ),
    ] = False,
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/celllore.sqlite"
    ),
) -> None:
    """Classify a run's incomplete transfers; retry only what a repair should retry.

    Refuses to run at all unless the run's download_selection checkpoint was
    already resolved to a download option -- repair retries what was
    approved, it never decides what to transfer.
    """
    ledger = CollaborationLedger(database)
    try:
        report = asyncio.run(classify_repair_candidates(ledger, run_id))
    except RepairNotAuthorized as error:
        raise typer.BadParameter(str(error)) from error

    if not report.candidates:
        console.print("[green]Nothing incomplete.[/green]")
        return

    table = Table(title="Classification")
    table.add_column("Class")
    table.add_column("Count", justify="right")
    for classification, count in sorted(report.histogram.items()):
        table.add_row(classification, str(count))
    console.print(table)

    if report.manifest_audit_required:
        audit = Table(title="manifest_audit_required (never retried)")
        audit.add_column("Asset")
        audit.add_column("Bundle")
        audit.add_column("URL", overflow="fold")
        for item in report.manifest_audit_required:
            audit.add_row(item.asset_id, item.bundle_id, item.url)
        console.print(audit)
        console.print(
            "[yellow]The manifest names a file, accession, or URL that is wrong, "
            "or the record was withdrawn -- fix the manifest, not the downloader.[/yellow]"
        )

    if not run:
        console.print(f"{len(report.retryable)} retryable; re-run with --run to retry them.")
        return

    policy = ledger.get_run(run_id).policy
    receipts = asyncio.run(execute_repair(ledger, run_id, report, policy=policy))
    console.print(f"Retried {len(receipts)} file(s).")


def _render_manifest_report(
    document: ManifestDocument,
    rows: list[ManifestRow],
    issues: list[ManifestIssue],
) -> None:
    counts = summarize_issues(issues)
    console.print(
        f"[bold]{document.source_path}[/bold]\n"
        f"dialect={document.dialect_id} sha256={document.source_sha256[:16]}… "
        f"rows={len(rows)} pair_units={len({row.pair_unit_key for row in rows})}"
    )
    units = Table(title="Pair units")
    units.add_column("Dataset")
    units.add_column("Pair unit", overflow="fold")
    units.add_column("Files", justify="right")
    units.add_column("Declared size", justify="right")
    units.add_column("Roles", overflow="fold")
    units.add_column("Transfer")
    for key in sorted({row.pair_unit_key for row in rows}):
        members = [row for row in rows if row.pair_unit_key == key]
        known = [row.size_bytes for row in members if row.size_bytes is not None]
        size = f"{sum(known) / 1024**3:.2f} GiB" if known else "—"
        if len(known) < len(members):
            size = f"≥ {size}" if known else "unknown"
        units.add_row(
            key[0],
            key[1],
            str(len(members)),
            size,
            ", ".join(sorted({row.role.value for row in members})),
            "direct" if all(row.is_direct for row in members) else "needs resolution",
        )
    console.print(units)
    if not issues:
        console.print("[green]No issues.[/green]")
        return
    console.print(
        f"issues: {counts['error']} error, {counts['warning']} warning, {counts['info']} info"
    )
    table = Table(title="Issues")
    table.add_column("Severity")
    table.add_column("Code")
    table.add_column("Row", justify="right")
    table.add_column("File", overflow="fold")
    table.add_column("Message", overflow="fold")
    colours = {
        IssueSeverity.ERROR: "red",
        IssueSeverity.WARNING: "yellow",
        IssueSeverity.INFO: "cyan",
    }
    for item in issues:
        table.add_row(
            f"[{colours[item.severity]}]{item.severity.value}[/{colours[item.severity]}]",
            item.code,
            str(item.row_index or "—"),
            item.file_name or "—",
            item.message,
        )
    console.print(table)


@app.command("agent-run")
def agent_run(
    question: Annotated[str, typer.Argument(help="Natural-language research question.")],
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/celllore.sqlite"
    ),
    max_results: Annotated[
        int, typer.Option("--max-results", "-n", min=1, max=100)
    ] = 20,
    use_llm: Annotated[
        bool,
        typer.Option(
            "--llm/--no-llm",
            help=(
                "LLM reasoning (research goal interpretation, search planning, "
                "GEO/paper/metadata understanding, PairUnit resolution, evidence "
                "synthesis) is this agent's core dependency, not an optional "
                "enhancement -- leave this at --llm for any real run. Without a "
                "configured model, --llm now fails fast rather than silently "
                "falling back to a deterministic-only workflow. --no-llm exists "
                "for tests and offline debugging only; it downgrades every "
                "semantic step (including PairUnit grouping, which then keeps "
                "each study's assets in one unresolved, provisional unit) and "
                "must never be used to produce a corpus-admission decision."
            ),
        ),
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
    max_assets: Annotated[
        int,
        typer.Option(
            "--max-assets",
            min=1,
            max=100000,
            help=(
                "Per-repository cap on files enumerated for one dataset. A "
                "study whose listing exceeds it is marked inventory:truncated "
                "and its reported size becomes a lower bound."
            ),
        ),
    ] = 500,
    strict_license: Annotated[
        bool,
        typer.Option(
            "--strict-license/--repository-license-policy",
            help=(
                "Strict requires a declared, allowlisted licence. The default "
                "also accepts an archive's own published terms of use when no "
                "per-record licence exists, recording the basis for audit."
            ),
        ),
    ] = False,
    allow_unknown_license: Annotated[
        bool,
        typer.Option(
            "--allow-unknown-license/--require-known-license",
            help=(
                "Allow publicly accessible bundles with no resolved per-record "
                "license into the recommendation tier, while retaining the license "
                "status and source basis in the audit trail."
            ),
        ),
    ] = False,
    planner_mode: Annotated[
        str,
        typer.Option(
            "--planner-mode",
            help=(
                "deep runs a sequential 4-stage LLM analysis (intent, study "
                "design, critical review, synthesis; ~600s budget) so the "
                "model actually completes its reasoning. fast races two "
                "parallel calls against a 30s budget each and degrades to a "
                "generic template on timeout -- pick it only when you need a "
                "quick, approximate result."
            ),
        ),
    ] = "deep",
    trace_mode: Annotated[str, typer.Option("--trace-mode", help="local or langsmith.")] = "local",
) -> None:
    """Run evidence, acquisition, materialization, QC, and governance agents."""
    if planner_mode not in {"fast", "deep"}:
        raise typer.BadParameter("planner-mode must be 'fast' or 'deep'.")
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
            "strict_license": strict_license,
            "allow_unknown_license": allow_unknown_license,
            "max_assets": max_assets,
            "planner_mode": planner_mode,
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
    bundle_id: Annotated[
        list[str] | None,
        typer.Option(
            "--bundle-id",
            help="Approve only these recommended bundle IDs; repeatable.",
        ),
    ] = None,
    database: Annotated[Path, typer.Option("--database", "-d")] = Path("var/celllore.sqlite"),
    trace_mode: Annotated[str, typer.Option("--trace-mode")] = "local",
) -> None:
    """Resume a persisted LangGraph run after a human decision."""
    ledger = CollaborationLedger(database)
    run_id = ledger.get_checkpoint(checkpoint_id).run_id
    supervisor = supervisor_for_run(ledger, str(database), run_id, trace_mode=trace_mode)
    if bundle_id:
        if option != "plan_only":
            raise typer.BadParameter("--bundle-id cannot be combined with --option.")
        resolution = {"option": "selected_recommended", "bundle_ids": bundle_id}
    else:
        resolution = {"option": option}
    checkpoint = asyncio.run(supervisor.resume_checkpoint(checkpoint_id, resolution))
    console.print_json(checkpoint.model_dump_json())


@app.command("graph-state")
def graph_state(
    run_id: Annotated[str, typer.Argument(help="LangGraph thread/run ID.")],
    database: Annotated[Path, typer.Option("--database", "-d")] = Path("var/celllore.sqlite"),
    trace_mode: Annotated[str, typer.Option("--trace-mode")] = "local",
) -> None:
    """Inspect the persisted LangGraph state for a run."""
    ledger = CollaborationLedger(database)
    supervisor = supervisor_for_run(ledger, str(database), run_id, trace_mode=trace_mode)
    console.print_json(json.dumps(asyncio.run(supervisor.graph_state(run_id)), ensure_ascii=False, default=str))


@app.command("serve")
def serve(
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(
        "var/celllore.sqlite"
    ),
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
) -> None:
    """Start the local API, SSE event stream, and React control room."""
    import uvicorn

    from celllore.interfaces.api import create_app

    uvicorn.run(create_app(database), host=host, port=port)


@app.command("llm-check")
def llm_check() -> None:
    """Verify the configured model provider without sending a billable request."""
    import os

    from celllore.infrastructure.llm.stepfun import load_local_env

    load_local_env()
    provider = os.getenv("SCDATA_AGENT_MODEL_PROVIDER", "stepfun").lower()
    settings = ModelRouter.from_environment().client(ReasoningProfile.PLANNER).settings
    effort = getattr(settings, "reasoning_effort", None)
    console.print(
        f"Provider '{provider}' configuration loaded: "
        f"model={settings.model}, base_url={settings.base_url}"
        + (f", reasoning_effort={effort}" if effort else "")
    )


@app.command("llm-ping")
def llm_ping() -> None:
    """Send one small billable request to verify the configured provider connection."""
    client = ModelRouter.from_environment().client(ReasoningProfile.PLANNER)
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
        typer.Option(
            "--llm/--no-llm",
            help=(
                "Use configured LLM planning (fails fast without a configured "
                "model). --no-llm is for tests/offline debugging only, not a "
                "supported way to compile a real research contract."
            ),
        ),
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
    degraded = [
        item
        for item in research_plan.planner_diagnostics
        if "failed" in item or "timeout" in item.casefold()
    ]
    if degraded:
        console.print(
            "[yellow]warning:[/yellow] the planner fell back to a template; "
            "this plan is not fully model-derived."
        )
        for item in degraded:
            console.print(f"  [yellow]-[/yellow] {item}")
        console.print()
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
    if query_plan.uncovered_sources:
        console.print(
            "\n[bold]Sources not covered by any adapter[/bold]\n"
            f"{', '.join(query_plan.uncovered_sources)}"
        )
    if research_plan.planner_diagnostics:
        console.print("\n[bold]Planner diagnostics[/bold]")
        for item in research_plan.planner_diagnostics:
            console.print(f"- {item}")


@app.command("list-runs")
def list_runs(
    database: Annotated[
        Path, typer.Option("--database", "-d")
    ] = Path("var/celllore.sqlite"),
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
    ] = Path("var/celllore.sqlite"),
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
    layer_counts = Counter(
        layer.value
        for candidate in result.candidates
        for profile in candidate.assay_profiles
        for layer in profile.omics_layers
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
        "layer_counts": dict(layer_counts),
        "pairing_level_counts": dict(pairing_counts),
        "technology_counts": dict(technology_counts),
        "errors": result.errors,
        "uncovered_sources": result.query_plan.uncovered_sources,
    }, ensure_ascii=False))
