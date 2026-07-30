"""LangGraph Studio export for visual graph debugging.

The LangGraph dev server supplies durable checkpointing; the project SQLite ledger
remains the audit record for research artifacts and human decisions.
"""

from __future__ import annotations

import os

from scdata_agent.application.collaboration import CollaborationLedger
from scdata_agent.application.orchestration_v2 import MultiAgentSupervisor

_database = os.getenv("SCDATA_AGENT_STUDIO_DATABASE", "var/studio_scdata_agent.sqlite")
_supervisor = MultiAgentSupervisor(
    CollaborationLedger(_database),
    _database,
    trace_mode=os.getenv("SCDATA_AGENT_TRACE_MODE", "local"),
)
graph = _supervisor.build_graph()
