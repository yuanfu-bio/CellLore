import json

import httpx
import pytest

from celllore.infrastructure.llm.stepfun import StepFunChatClient, StepFunSettings


@pytest.mark.asyncio
async def test_stepfun_client_uses_openai_compatible_endpoint():
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )

    settings = StepFunSettings(api_key="test-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = StepFunChatClient(settings, http_client)
        assert await client.complete([{"role": "user", "content": "hello"}]) == "ok"

    assert seen["url"] == (
        "https://api.stepfun.com/step_plan/v1/chat/completions"
    )
    assert seen["authorization"] == "Bearer test-key"
    assert seen["payload"] == {
        "model": "step-3.7-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.1,
        "max_tokens": 2000,
        "reasoning_effort": "medium",
    }


@pytest.mark.asyncio
async def test_stepfun_client_retries_empty_content_with_larger_budget():
    payloads = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "", "reasoning_content": "thinking"},
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
            request=request,
        )

    settings = StepFunSettings(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = StepFunChatClient(settings, http_client)
        assert await client.complete([{"role": "user", "content": "hello"}], max_tokens=1_000) == "ok"

    assert [payload["max_tokens"] for payload in payloads] == [1_000, 2_000]
    assert [payload["reasoning_effort"] for payload in payloads] == ["medium", "low"]


@pytest.mark.asyncio
async def test_stepfun_client_retries_nonempty_truncated_content():
    payloads = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        content = "partial" if len(payloads) == 1 else "complete"
        finish = "length" if len(payloads) == 1 else "stop"
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": finish, "message": {"content": content}}]},
            request=request,
        )

    settings = StepFunSettings(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = StepFunChatClient(settings, http_client)
        assert await client.complete([{"role": "user", "content": "hello"}], max_tokens=2_400) == "complete"

    assert [payload["max_tokens"] for payload in payloads] == [2_400, 4_800]


@pytest.mark.asyncio
async def test_stepfun_client_caps_unusually_large_output_budget():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
            request=request,
        )

    settings = StepFunSettings(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = StepFunChatClient(settings, http_client)
        assert await client.complete(
            [{"role": "user", "content": "hello"}], max_tokens=90_000
        ) == "ok"

    assert seen["max_tokens"] == 32_000


@pytest.mark.asyncio
async def test_stepfun_client_uses_native_strict_json_schema():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"value":"ok"}'}}]},
            request=request,
        )

    settings = StepFunSettings(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = StepFunChatClient(settings, http_client)
        result = await client.complete_structured(
            [{"role": "user", "content": "hello"}],
            schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            schema_name="TestOutput",
        )

    assert result == '{"value":"ok"}'
    assert seen["response_format"] == {"type": "json_object"}
