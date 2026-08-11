import pytest

from celllore.application.agents.supervisor import MultiAgentSupervisor
from celllore.application.collaboration import (
    AgentName,
    CollaborationLedger,
    RunStatus,
    TaskStatus,
)
from celllore.domain.models import (
    AccessLevel,
    CurationDecision,
    DatasetCandidate,
    DiscoveryRunResult,
    FeasibilityReport,
    QueryPlan,
)


class FakeWorkflow:
    compiler = None

    async def run(self, question, max_results=20):
        requirement = self.compiler.compile(question)
        candidate = DatasetCandidate(
            candidate_id="candidate_1",
            title="Human lung multiome",
            access_level=AccessLevel.PUBLIC_VERIFIED,
        )
        report = FeasibilityReport(
            report_id="report_1",
            candidate_id="candidate_1",
            dimensions={},
            decision=CurationDecision.ELIGIBLE,
        )
        return DiscoveryRunResult(
            run_id="discovery_1",
            requirement=requirement,
            query_plan=QueryPlan(requirement=requirement, queries=[]),
            source_records=[],
            candidates=[candidate],
            feasibility_reports=[report],
        )


def test_cancel_closes_running_task_rows(tmp_path):
    database = tmp_path / "agent.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run("human PBMC multiome", {"use_llm": False})
    ledger.start_task(run.run_id, AgentName.ASSET_SCOUT, [])
    supervisor = MultiAgentSupervisor(ledger, str(database))

    supervisor.cancel(run.run_id)

    assert ledger.get_run(run.run_id).status == RunStatus.CANCELLED
    tasks = ledger.list_tasks(run.run_id)
    assert tasks[0].status == TaskStatus.CANCELLED
    assert tasks[0].error == "Run cancelled by user."


def test_cancel_does_not_rewrite_a_completed_run(tmp_path):
    database = tmp_path / "agent.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run("completed run", {"use_llm": False})
    ledger.set_run_status(run.run_id, RunStatus.COMPLETED)
    supervisor = MultiAgentSupervisor(ledger, str(database))

    supervisor.cancel(run.run_id)

    assert ledger.get_run(run.run_id).status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_supervisor_persists_handoffs_and_skips_empty_download(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "celllore.application.agents.discovery.default_workflow",
        lambda database_path, use_llm=False: FakeWorkflow(),
    )
    ledger = CollaborationLedger(tmp_path / "agent.sqlite")
    run = ledger.create_run("human lung multiome", {"use_llm": False})
    supervisor = MultiAgentSupervisor(ledger, str(tmp_path / "agent.sqlite"))

    await supervisor.execute(run.run_id)

    assert ledger.get_run(run.run_id).status == RunStatus.COMPLETED
    # The pre-download workflow includes direct/enriched scouting, metadata
    # evidence, escalation, GEO sample metadata, and inventory reconciliation.
    assert len(ledger.list_tasks(run.run_id)) == 11
    assert len(ledger.list_artifacts(run.run_id)) >= 8
    assert ledger.list_checkpoints(run.run_id) == []
    events = ledger.events_since(run.run_id)
    assert any(
        event["type"] == "download_skipped"
        and event["payload"]["reason"] == "no_eligible_bundles"
        for event in events
    )
    assert len(events) > 10
