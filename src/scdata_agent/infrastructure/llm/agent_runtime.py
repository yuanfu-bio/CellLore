"""Bounded LangChain tool-calling runtime used only by exploratory specialists."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent

from scdata_agent.infrastructure.llm import tracing
from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return content if isinstance(content, str) else str(content or "")


def _record_model_messages(
    messages: Sequence[Any],
    *,
    profile: ReasoningProfile,
    system_prompt: str,
    user_prompt: str,
) -> None:
    """Emit one trace event for every model response in a tool-calling loop."""
    for attempt, message in enumerate(messages):
        if getattr(message, "type", "") != "ai":
            continue
        usage_metadata = getattr(message, "usage_metadata", None) or {}
        response_metadata = getattr(message, "response_metadata", None) or {}
        provider_usage = response_metadata.get("token_usage", {})
        prompt_tokens = int(
            usage_metadata.get("input_tokens")
            or provider_usage.get("prompt_tokens")
            or 0
        )
        completion_tokens = int(
            usage_metadata.get("output_tokens")
            or provider_usage.get("completion_tokens")
            or 0
        )
        total_tokens = int(
            usage_metadata.get("total_tokens")
            or provider_usage.get("total_tokens")
            or prompt_tokens + completion_tokens
        )
        tracing.emit(
            client=response_metadata.get("model_provider") or "langchain",
            role=profile.value,
            model=(
                response_metadata.get("model_name")
                or response_metadata.get("model")
                or "unknown"
            ),
            system_prompt=system_prompt,
            user_payload=user_prompt,
            raw_reply=_message_text(message),
            reasoning_content=None,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
            latency_ms=None,
            attempt=attempt,
            truncated=False,
            error=None,
        )


async def run_bounded_agent(*, profile: ReasoningProfile, tools: Sequence[Any], system_prompt: str, user_prompt: str, max_steps: int = 8) -> str:
    """Run a read-only tool agent and return its final text.

    The caller owns validation, persistence, and any fallback behavior.
    """
    model = ModelRouter.from_environment().tool_model(profile)
    agent = create_agent(model, tools=list(tools), system_prompt=system_prompt, name=f"scdata_{profile.value}")
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_prompt}]},
        config={"recursion_limit": max_steps, "tags": ["celllore", profile.value]},
    )
    _record_model_messages(
        result["messages"],
        profile=profile,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    message = result["messages"][-1]
    content = _message_text(message)
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Tool-calling agent returned no final text.")
    return content.strip()
