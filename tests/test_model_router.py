import json

import httpx
import pytest

from celllore.infrastructure.llm.router import OpenAICompatibleClient, OpenAICompatibleSettings


@pytest.mark.asyncio
async def test_openai_compatible_router_client():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content)["model"] == "test-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=request)

    settings = OpenAICompatibleSettings(api_key="test-key", base_url="https://example.test/v1", model="test-model")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleClient(settings, http_client)
        assert await client.complete([{"role": "user", "content": "hello"}]) == "ok"


def test_profile_model_override(monkeypatch):
    from celllore.infrastructure.llm.router import ModelRouter, ReasoningProfile

    monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("SCDATA_AGENT_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("SCDATA_AGENT_MODEL_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("SCDATA_AGENT_MODEL", "default-model")
    monkeypatch.setenv("SCDATA_AGENT_MODEL_PLANNER", "planner-model")

    router = ModelRouter.from_environment()
    assert router.client(ReasoningProfile.PLANNER).settings.model == "planner-model"
    assert router.client(ReasoningProfile.REVIEW).settings.model == "default-model"


def test_deepseek_provider_uses_documented_defaults(monkeypatch, tmp_path):
    from celllore.infrastructure.llm.router import ModelRouter, ReasoningProfile

    # chdir away from the repo's real .env, which may itself configure DeepSeek.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    router = ModelRouter.from_environment()
    settings = router.client(ReasoningProfile.PLANNER).settings
    assert settings.model == "deepseek-v4-flash"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.api_key == "test-key"


def test_deepseek_provider_honours_overrides(monkeypatch):
    from celllore.infrastructure.llm.router import ModelRouter, ReasoningProfile

    monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("SCDATA_AGENT_MODEL_PLANNER", "planner-override")

    router = ModelRouter.from_environment()
    assert router.client(ReasoningProfile.PLANNER).settings.model == "planner-override"
    assert router.client(ReasoningProfile.REVIEW).settings.model == "deepseek-v4-pro"
    assert router.client(ReasoningProfile.REVIEW).settings.base_url == "https://example.test/v1"


def test_deepseek_provider_requires_api_key(monkeypatch, tmp_path):
    from celllore.infrastructure.llm.router import ModelRouter

    # chdir away from the repo's real .env, which may itself set DEEPSEEK_API_KEY.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        ModelRouter.from_environment()


def test_stepfun_planner_uses_low_effort_by_default(monkeypatch):
    from celllore.infrastructure.llm.router import ModelRouter, ReasoningProfile

    monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "stepfun")
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setenv("STEPFUN_REASONING_EFFORT", "medium")
    monkeypatch.delenv("SCDATA_AGENT_REASONING_EFFORT_PLANNER", raising=False)

    router = ModelRouter.from_environment()
    assert router.client(ReasoningProfile.PLANNER).settings.reasoning_effort == "low"
    assert router.client(ReasoningProfile.EXTRACT).settings.reasoning_effort == "low"
    assert router.client(ReasoningProfile.REVIEW).settings.reasoning_effort == "medium"


def test_stepfun_extract_effort_can_be_overridden(monkeypatch):
    from celllore.infrastructure.llm.router import ModelRouter, ReasoningProfile

    monkeypatch.setenv("SCDATA_AGENT_MODEL_PROVIDER", "stepfun")
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setenv("STEPFUN_REASONING_EFFORT", "medium")
    monkeypatch.setenv("SCDATA_AGENT_REASONING_EFFORT_EXTRACT", "medium")

    router = ModelRouter.from_environment()
    assert router.client(ReasoningProfile.EXTRACT).settings.reasoning_effort == "medium"
