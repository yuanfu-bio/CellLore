from types import SimpleNamespace

from celllore.infrastructure.llm import tracing
from celllore.infrastructure.llm.agent_runtime import _record_model_messages
from celllore.infrastructure.llm.router import ReasoningProfile


def test_tool_agent_messages_are_recorded_with_normalized_usage() -> None:
    events: list[dict] = []
    messages = [
        SimpleNamespace(type="human", content="question"),
        SimpleNamespace(
            type="ai",
            content="answer",
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
            },
            response_metadata={
                "model_provider": "openai-compatible",
                "model_name": "test-model",
            },
        ),
    ]

    with tracing.stage("resource_discovery"), tracing.sink(events.append):
        _record_model_messages(
            messages,
            profile=ReasoningProfile.DISCOVERY,
            system_prompt="system",
            user_prompt="user",
        )

    assert len(events) == 1
    assert events[0]["stage"] == "resource_discovery"
    assert events[0]["model"] == "test-model"
    assert events[0]["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
