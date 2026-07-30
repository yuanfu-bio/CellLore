"""Provider-neutral model routing for specialist-agent reasoning profiles."""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scdata_agent.infrastructure.llm.stepfun import StepFunChatClient, StepFunSettings, load_local_env


class ReasoningProfile(StrEnum):
    PLANNER = "planner"
    DISCOVERY = "discovery"
    REVIEW = "review"
    CHAT = "chat"
    GOVERN = "govern"
    EXTRACT = "extract"


class ChatClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]], *, temperature: float = 0.1, max_tokens: int = 2_000) -> str: ...


class OpenAICompatibleSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    api_key: str = Field(min_length=1)
    base_url: str
    model: str
    reasoning_effort: str | None = None


class OpenAICompatibleClient:
    def __init__(self, settings: OpenAICompatibleSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def complete(self, messages: list[dict[str, Any]], *, temperature: float = 0.1, max_tokens: int = 2_000) -> str:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=60.0)
        payload: dict[str, Any] = {"model": self.settings.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if self.settings.reasoning_effort:
            payload["reasoning_effort"] = self.settings.reasoning_effort
        try:
            response = await client.post(f"{self.settings.base_url.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {self.settings.api_key}"}, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("OpenAI-compatible provider returned an empty message.")
            return content
        finally:
            if owns_client:
                await client.aclose()


class AnthropicSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    api_key: str = Field(min_length=1)
    model: str
    base_url: str = "https://api.anthropic.com/v1"


class AnthropicClient:
    def __init__(self, settings: AnthropicSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def complete(self, messages: list[dict[str, Any]], *, temperature: float = 0.1, max_tokens: int = 2_000) -> str:
        system = "\n".join(str(item["content"]) for item in messages if item.get("role") == "system")
        turns = [{"role": item["role"], "content": item["content"]} for item in messages if item.get("role") != "system"]
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=60.0)
        try:
            response = await client.post(f"{self.settings.base_url.rstrip('/')}/messages", headers={"x-api-key": self.settings.api_key, "anthropic-version": "2023-06-01"}, json={"model": self.settings.model, "system": system, "messages": turns, "temperature": temperature, "max_tokens": max_tokens})
            response.raise_for_status()
            blocks = response.json().get("content", [])
            text = "".join(item.get("text", "") for item in blocks if item.get("type") == "text")
            if not text.strip():
                raise RuntimeError("Anthropic provider returned an empty message.")
            return text
        finally:
            if owns_client:
                await client.aclose()


class ModelRouter:
    """Routes named reasoning profiles to configured providers with safe defaults."""

    def __init__(self, clients: dict[ReasoningProfile, ChatClient]) -> None:
        self.clients = clients

    def client(self, profile: ReasoningProfile) -> ChatClient:
        return self.clients.get(profile) or self.clients[ReasoningProfile.PLANNER]


    def tool_model(self, profile: ReasoningProfile):
        """Return a LangChain model configured for this specialist profile."""
        load_local_env()
        provider = os.getenv("SCDATA_AGENT_MODEL_PROVIDER", "stepfun").lower()
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            key = os.getenv("ANTHROPIC_API_KEY")
            model = _profile_value(profile, os.getenv("ANTHROPIC_MODEL"), "MODEL")
            if not key or not model:
                raise RuntimeError("ANTHROPIC_API_KEY and ANTHROPIC_MODEL are required.")
            return ChatAnthropic(model=model, api_key=key, base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"), temperature=0.0)
        from langchain_openai import ChatOpenAI
        if provider in {"openai_compatible", "openai", "openrouter", "ollama"}:
            key = os.getenv("SCDATA_AGENT_MODEL_API_KEY")
            base_url = os.getenv("SCDATA_AGENT_MODEL_BASE_URL")
            model = _profile_value(profile, os.getenv("SCDATA_AGENT_MODEL"), "MODEL")
            if not key or not base_url or not model:
                raise RuntimeError("SCDATA_AGENT_MODEL_API_KEY, SCDATA_AGENT_MODEL_BASE_URL, and SCDATA_AGENT_MODEL are required.")
            return ChatOpenAI(model=model, api_key=key, base_url=base_url, temperature=0.0)
        settings = StepFunSettings.from_environment()
        model = _profile_value(profile, settings.model, "MODEL")
        effort = _profile_effort(profile, settings.reasoning_effort)
        return ChatOpenAI(model=model, api_key=settings.api_key, base_url=settings.base_url, temperature=0.0, extra_body={"reasoning_effort": effort})

    @classmethod
    def from_environment(cls) -> "ModelRouter":
        load_local_env()
        provider = os.getenv("SCDATA_AGENT_MODEL_PROVIDER", "stepfun").lower()
        clients: dict[ReasoningProfile, ChatClient] = {}
        for profile in ReasoningProfile:
            if provider == "anthropic":
                key = os.getenv("ANTHROPIC_API_KEY")
                model = _profile_value(profile, os.getenv("ANTHROPIC_MODEL"), "MODEL")
                if not key or not model:
                    raise RuntimeError("ANTHROPIC_API_KEY and ANTHROPIC_MODEL are required.")
                clients[profile] = AnthropicClient(AnthropicSettings(api_key=key, model=model, base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")))
            elif provider in {"openai_compatible", "openai", "openrouter", "ollama"}:
                key, base_url = os.getenv("SCDATA_AGENT_MODEL_API_KEY"), os.getenv("SCDATA_AGENT_MODEL_BASE_URL")
                model = _profile_value(profile, os.getenv("SCDATA_AGENT_MODEL"), "MODEL")
                if not key or not base_url or not model:
                    raise RuntimeError("SCDATA_AGENT_MODEL_API_KEY, SCDATA_AGENT_MODEL_BASE_URL, and SCDATA_AGENT_MODEL are required.")
                clients[profile] = OpenAICompatibleClient(OpenAICompatibleSettings(api_key=key, base_url=base_url, model=model, reasoning_effort=_profile_value(profile, os.getenv("SCDATA_AGENT_REASONING_EFFORT"), "REASONING_EFFORT")))
            else:
                settings = StepFunSettings.from_environment()
                clients[profile] = StepFunChatClient(settings.model_copy(update={"model": _profile_value(profile, settings.model, "MODEL"), "reasoning_effort": _profile_effort(profile, settings.reasoning_effort)}))
        return cls(clients)


def _profile_value(profile: ReasoningProfile, default: str | None, suffix: str) -> str | None:
    return os.getenv(f"SCDATA_AGENT_{suffix}_{profile.value.upper()}") or default


def _profile_effort(profile: ReasoningProfile, default: str | None) -> str | None:
    stage_default = "low" if profile == ReasoningProfile.PLANNER else default
    return _profile_value(profile, stage_default, "REASONING_EFFORT")
