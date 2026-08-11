import os

import pytest

from celllore.infrastructure.observability import TraceMode, configure_tracing


def test_local_mode_disables_cloud_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert configure_tracing("local") is TraceMode.LOCAL
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_langsmith_mode_requires_explicit_credentials(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.setattr("celllore.infrastructure.llm.stepfun.load_local_env", lambda: None)
    with pytest.raises(RuntimeError):
        configure_tracing("langsmith")
