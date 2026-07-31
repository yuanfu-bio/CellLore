"""Optional StepFun (OpenAI-compatible) client for research-question reasoning."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_BASE_URL = "https://api.stepfun.com/step_plan/v1"
DEFAULT_MODEL = "step-3.7-flash"
VALID_EFFORTS = {"low", "medium", "high"}


def load_local_env(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overriding real environment values."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class StepFunSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key: str = Field(min_length=1)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "medium"
    timeout_seconds: float = Field(default=180.0, gt=0)

    @classmethod
    def from_environment(cls) -> StepFunSettings:
        load_local_env()
        api_key = os.getenv("STEPFUN_API_KEY")
        if not api_key:
            raise RuntimeError(
                "STEPFUN_API_KEY is not configured. Copy .env.example to .env "
                "and add a rotated key, or export it in the shell."
            )
        effort = os.getenv("STEPFUN_REASONING_EFFORT", "medium").lower()
        if effort not in VALID_EFFORTS:
            raise RuntimeError(
                "STEPFUN_REASONING_EFFORT must be one of low, medium, high."
            )
        return cls(
            api_key=api_key,
            base_url=os.getenv("STEPFUN_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=os.getenv("STEPFUN_MODEL", DEFAULT_MODEL),
            reasoning_effort=effort,
            timeout_seconds=float(os.getenv("STEPFUN_TIMEOUT_SECONDS", "180")),
        )


class StepFunChatClient:
    """Minimal async client; no request is sent until ``complete`` is called."""

    def __init__(
        self,
        settings: StepFunSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2_000,
    ) -> str:
        return await self._complete(
            messages, temperature=temperature, max_tokens=max_tokens
        )

    async def complete_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any],
        schema_name: str,
        temperature: float = 0.0,
        max_tokens: int = 2_000,
    ) -> str:
        """Use StepFun JSON mode; callers validate against the supplied schema locally."""
        return await self._complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        try:
            request_payload = {
                "model": self.settings.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "reasoning_effort": self.settings.reasoning_effort,
            }
            if response_format is not None:
                request_payload["response_format"] = response_format
            empty_diagnostic = "unknown"
            for attempt in range(2):
                response = await client.post(
                    f"{self.settings.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.api_key}"},
                    json=request_payload,
                )
                response.raise_for_status()
                payload = response.json()
                choice = payload["choices"][0]
                message = choice["message"]
                content = message.get("content")
                truncated = choice.get("finish_reason") == "length"
                if isinstance(content, str) and content.strip() and not truncated:
                    return content
                empty_diagnostic = (
                    f"finish_reason={choice.get('finish_reason')};"
                    f"content_chars={len(content or '')};"
                    f"reasoning_chars={len(message.get('reasoning_content') or '')}"
                )
                if attempt == 0:
                    request_payload["max_tokens"] = min(
                        max(int(request_payload["max_tokens"]) * 2, 4_000),
                        12_000,
                    )
            raise RuntimeError(
                "StepFun returned an empty or truncated chat-completion message after retry; "
                + empty_diagnostic
            )
        finally:
            if owns_client:
                await client.aclose()
