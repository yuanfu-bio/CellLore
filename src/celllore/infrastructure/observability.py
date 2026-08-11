"""Explicit local-or-LangSmith tracing configuration."""

from __future__ import annotations

import os
from enum import StrEnum


class TraceMode(StrEnum):
    LOCAL = "local"
    LANGSMITH = "langsmith"


def configure_tracing(mode: str | None = None) -> TraceMode:
    """Configure tracing once at process start; local mode never uploads traces."""
    from celllore.infrastructure.llm.stepfun import load_local_env
    load_local_env()
    selected = TraceMode((mode or os.getenv("SCDATA_AGENT_TRACE_MODE", "local")).lower())
    if selected is TraceMode.LOCAL:
        os.environ["LANGSMITH_TRACING"] = "false"
        return selected
    required = ("LANGSMITH_API_KEY", "LANGSMITH_PROJECT")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError("LangSmith tracing requires " + ", ".join(missing))
    os.environ["LANGSMITH_TRACING"] = "true"
    return selected
