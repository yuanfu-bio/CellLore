"""Bounded LangChain tool-calling runtime used only by exploratory specialists."""

from __future__ import annotations

from typing import Any, Sequence

from langchain.agents import create_agent

from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile


async def run_bounded_agent(*, profile: ReasoningProfile, tools: Sequence[Any], system_prompt: str, user_prompt: str, max_steps: int = 8) -> str:
    """Run a read-only tool agent and return its final text.

    The caller owns validation, persistence, and any fallback behavior.
    """
    model = ModelRouter.from_environment().tool_model(profile)
    agent = create_agent(model, tools=list(tools), system_prompt=system_prompt, name=f"scdata_{profile.value}")
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_prompt}]},
        config={"recursion_limit": max_steps, "tags": ["scdata-agent", profile.value]},
    )
    message = result["messages"][-1]
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Tool-calling agent returned no final text.")
    return content.strip()
