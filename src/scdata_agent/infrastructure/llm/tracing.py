"""Cross-cutting hook so any ``ChatCompletionClient`` call can be observed live.

A provider client (StepFun, OpenAI-compatible, Anthropic) calls :func:`emit`
after every completion attempt. Nothing happens unless a caller has installed
a sink with :func:`sink` -- by default this module is inert, so it never
changes provider behaviour or leaks data on its own. ``stage`` lets a caller
(e.g. the four research_planner steps) tag which logical step a completion
belongs to; both are ``ContextVar``-based so they propagate correctly across
``asyncio.create_task`` (fast-mode planning runs two calls concurrently).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

_stage: ContextVar[str] = ContextVar("scdata_llm_stage", default="unknown")
_sink: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "scdata_llm_sink", default=None
)
_langsmith_client: Any | None = None


@contextmanager
def stage(name: str):
    token = _stage.set(name)
    try:
        yield
    finally:
        _stage.reset(token)


@contextmanager
def sink(callback: Callable[[dict[str, Any]], None] | None):
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)


def emit(**fields: Any) -> None:
    event = {"stage": _stage.get(), "emitted_at": time.time(), **fields}
    callback = _sink.get()
    if callback is not None:
        callback(event)
    _log_to_langsmith(event)


def _get_langsmith_client() -> Any | None:
    global _langsmith_client
    if _langsmith_client is not None:
        return _langsmith_client
    try:
        from langsmith import Client

        _langsmith_client = Client()
    except Exception:
        _langsmith_client = False
    return _langsmith_client or None


def _log_to_langsmith(event: dict[str, Any]) -> None:
    """Best-effort mirror of a captured LLM call into LangSmith.

    Only runs when the operator explicitly chose ``--trace-mode langsmith``
    (``configure_tracing`` is the sole place ``LANGSMITH_TRACING`` gets set,
    so this reads the same switch rather than introducing a second one).
    Never raises: a LangSmith outage must not break a local run.
    """
    if os.getenv("LANGSMITH_TRACING", "").lower() != "true":
        return
    client = _get_langsmith_client()
    if client is None:
        return
    try:
        latency_ms = event.get("latency_ms") or 0
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(milliseconds=latency_ms)
        error = event.get("error")
        outputs = (
            None
            if error and not event.get("raw_reply")
            else {
                "raw_reply": event.get("raw_reply"),
                "reasoning_content": event.get("reasoning_content"),
                "usage": event.get("usage"),
            }
        )
        client.create_run(
            id=uuid.uuid4(),
            name=f"{event.get('role') or event.get('client') or 'llm'}:{event.get('stage', 'unknown')}",
            run_type="llm",
            inputs={
                "system_prompt": event.get("system_prompt"),
                "user_payload": event.get("user_payload"),
            },
            outputs=outputs,
            error=error,
            start_time=start_time,
            end_time=end_time,
            project_name=os.getenv("LANGSMITH_PROJECT"),
            extra={
                "metadata": {
                    "model": event.get("model"),
                    "client": event.get("client"),
                    "attempt": event.get("attempt"),
                    "truncated": event.get("truncated"),
                }
            },
        )
    except Exception:
        return
