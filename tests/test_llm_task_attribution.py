from typing import Any

import pytest

from celllore.application.agents.shared import AgentContext
from celllore.application.collaboration import (
    AgentName,
    ArtifactKind,
    CollaborationLedger,
)
from celllore.application.orchestration_v2 import MultiAgentSupervisor
from celllore.infrastructure.llm import tracing


class _TracingAgent:
    name = AgentName.ASSET_SCOUT

    async def run(self, context: AgentContext) -> list[Any]:
        tracing.emit(
            client="test",
            role="extract",
            model="test-model",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
        return []


@pytest.mark.asyncio
async def test_dispatch_tags_llm_call_with_exact_task_and_node(tmp_path):
    database = tmp_path / "agent.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run("attribute this call", {"use_llm": False})
    supervisor = MultiAgentSupervisor(ledger, str(database))
    context = AgentContext(
        run_id=run.run_id,
        question=run.question,
        database_path=str(database),
        ledger=ledger,
        catalog=supervisor.catalog,
    )

    await supervisor._dispatch(
        context,
        AgentName.ASSET_SCOUT,
        _TracingAgent(),
        node_id="inventory_reconcile",
    )

    task = ledger.list_tasks(run.run_id)[0]
    call = next(
        item
        for item in ledger.list_artifacts(run.run_id)
        if item.kind == ArtifactKind.LLM_CALL
    )
    assert call.payload["task_id"] == task.task_id
    assert call.payload["workflow_node"] == "inventory_reconcile"
