"""The OpenAI-compatible adapter's wire format, reasoning replay, and error classification."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from paid_media_agent.config import ModelConfig
from paid_media_agent.harness.messages import AssistantMessage, ToolCall, ToolMessage, UserMessage
from paid_media_agent.harness.models import (
    ModelError,
    OpenAICompatibleModel,
    ToolSchema,
    resolve_model,
)

TOOL = ToolSchema("lookup", "Look something up", {"type": "object", "properties": {}})
REASONING = [
    {"type": "reasoning.text", "text": "thinking", "signature": "sig-1", "format": "anthropic-v1"},
    {"type": "reasoning.encrypted", "data": "opaque==", "id": "r1", "format": "google-v1"},
]


def _model(handler: Any, **kwargs: Any) -> tuple[OpenAICompatibleModel, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return handler(request)

    model = OpenAICompatibleModel(
        model="vendor/model",
        base_url="https://llm.example/v1",
        api_key="test-key",
        transport=httpx.MockTransport(respond),
        **kwargs,
    )
    return model, sent


def _reply(message: dict[str, Any], status: int = 200) -> Any:
    return lambda _request: httpx.Response(status, json={"choices": [{"message": message}]})


async def test_a_turn_round_trips_tool_calls_and_replays_reasoning_unmodified() -> None:
    model, sent = _model(
        _reply(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"q": 1}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "not json"},
                    },
                ],
                "reasoning_details": REASONING,
            }
        ),
        zero_data_retention=True,
    )
    first = await model.complete(system="rules", messages=[UserMessage("hi")], tools=[TOOL])

    assert first.tool_calls[0] == ToolCall("c1", "lookup", {"q": 1})
    assert first.tool_calls[1].invalid_arguments == "not json" and first.tool_calls[1].args == {}
    assert first.provider_state == {"reasoning_details": REASONING}
    request = sent[0]
    assert request["messages"][0] == {"role": "system", "content": "rules"}
    assert request["tools"][0]["function"]["name"] == "lookup"
    assert "provider" not in request, "zero data retention is OpenRouter's routing option only"

    history = [UserMessage("hi"), first, ToolMessage("c1", "lookup", "result")]
    await model.complete(system="rules", messages=history, tools=[TOOL])
    assistant = sent[1]["messages"][2]
    assert assistant["reasoning_details"] == REASONING, "replayed byte-for-byte"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"q": 1}'
    assert assistant["tool_calls"][1]["function"]["arguments"] == "not json"
    assert sent[1]["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "result"}


async def test_text_parts_are_joined_and_no_tools_means_no_tools_key() -> None:
    model, sent = _model(
        _reply({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
    )
    reply = await model.complete(system="", messages=[UserMessage("hi")], tools=[])
    assert reply == AssistantMessage("ab")
    assert "tools" not in sent[0] and sent[0]["messages"][0]["role"] == "user"


@pytest.mark.parametrize(
    ("status", "transient"), [(429, True), (503, True), (400, False), (401, False)]
)
async def test_http_errors_are_classified_for_retry(status: int, transient: bool) -> None:
    model, _ = _model(lambda _r: httpx.Response(status, text="nope"))
    with pytest.raises(ModelError) as caught:
        await model.complete(system="", messages=[UserMessage("hi")], tools=[])
    assert caught.value.transient is transient and str(status) in str(caught.value)


async def test_timeouts_and_empty_choices_are_transient() -> None:
    def time_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    model, _ = _model(time_out)
    with pytest.raises(ModelError) as caught:
        await model.complete(system="", messages=[UserMessage("hi")], tools=[])
    assert caught.value.transient

    model, _ = _model(lambda _r: httpx.Response(200, json={"choices": [], "error": {"code": 502}}))
    with pytest.raises(ModelError) as caught:
        await model.complete(system="", messages=[UserMessage("hi")], tools=[])
    assert caught.value.transient


async def test_a_missing_key_fails_the_call_not_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    model = resolve_model(
        ModelConfig.parse("anthropic:claude-sonnet-4-6"), api_key_env=None, timeout_seconds=30
    )
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY is not set") as caught:
        await model.complete(system="", messages=[UserMessage("hi")], tools=[])
    assert caught.value.transient is False


def test_providers_resolve_to_their_endpoint_or_an_explicit_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    model = resolve_model(
        ModelConfig.parse("openrouter:anthropic/claude-haiku-4.5"),
        api_key_env=None,
        timeout_seconds=30,
    )
    assert model.name == "anthropic/claude-haiku-4.5"
    assert str(model._client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"

    monkeypatch.setenv("MY_KEY", "k")
    custom = resolve_model(
        ModelConfig.parse("openai:local-model", base_url="http://localhost:8000/v1"),
        api_key_env="MY_KEY",
        timeout_seconds=30,
    )
    assert str(custom._client.base_url).rstrip("/") == "http://localhost:8000/v1"

    with pytest.raises(ValueError, match="unknown model provider"):
        resolve_model(ModelConfig.parse("nobody:model"), api_key_env=None, timeout_seconds=30)
