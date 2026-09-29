"""Chat models behind one small protocol, and the OpenAI-compatible adapter every provider uses.

Anthropic, OpenAI, Gemini, OpenRouter, and the other configured providers all serve the Chat
Completions shape, so one adapter covers them. Provider-specific reasoning data comes back in
`reasoning_details` and is replayed unmodified: Gemini and Claude need it to continue reasoning
after tool results.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

import httpx

from paid_media_agent.config import ModelConfig
from paid_media_agent.harness.messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolMessage,
    Usage,
    UserMessage,
)

PromptCache = Literal["auto", "off"]


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ModelError(Exception):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class ChatModel(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AssistantMessage: ...


@dataclass(frozen=True)
class Provider:
    base_url: str
    key_env: str


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "openai": Provider("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "google_genai": Provider(
        "https://generativelanguage.googleapis.com/v1beta/openai", "GOOGLE_API_KEY"
    ),
    "openrouter": Provider("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": Provider("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "xai": Provider("https://api.x.ai/v1", "XAI_API_KEY"),
    "deepseek": Provider("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "mistralai": Provider("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "moonshot": Provider("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    "zhipu": Provider("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
}
TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def wire_messages(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for message in messages:
        if isinstance(message, UserMessage):
            wire.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            item: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.invalid_arguments
                            if call.invalid_arguments is not None
                            else json.dumps(call.args),
                        },
                    }
                    for call in message.tool_calls
                ]
            if message.provider_state:
                item.update(message.provider_state)
            wire.append(item)
        elif isinstance(message, ToolMessage):
            wire.append(
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
            )
    return wire


def parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    function = raw.get("function") or {}
    text = function.get("arguments") or "{}"
    try:
        args = json.loads(text) if isinstance(text, str) else text
    except json.JSONDecodeError:
        args = None
    if not isinstance(args, dict):
        return ToolCall(
            id=str(raw.get("id")),
            name=str(function.get("name")),
            args={},
            invalid_arguments=str(text),
        )
    return ToolCall(id=str(raw.get("id")), name=str(function.get("name")), args=args)


def parse_assistant(message: dict[str, Any]) -> AssistantMessage:
    content = message.get("content")
    if isinstance(content, list):
        # Only text parts reach a client; reasoning and other parts stay out of the reply.
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    state = (
        {"reasoning_details": message["reasoning_details"]}
        if message.get("reasoning_details")
        else None
    )
    return AssistantMessage(
        content=(content or "").strip(),
        tool_calls=tuple(parse_tool_call(c) for c in message.get("tool_calls") or ()),
        provider_state=state,
    )


class OpenAICompatibleModel:
    """Chat Completions over httpx. Retries and timeouts belong to the agent loop."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120,
        max_tokens: int = 4096,
        zero_data_retention: bool = False,
        prompt_cache: PromptCache = "auto",
        provider: str | None = None,
        missing_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._missing_key = missing_key
        self._max_tokens = max_tokens
        self._zdr = zero_data_retention
        self._openrouter = "openrouter.ai" in base_url
        self.provider = provider or ("openrouter" if self._openrouter else "openai_compatible")
        self.cache_requested = (
            prompt_cache == "auto" and self._openrouter and model.startswith("anthropic/")
        )
        """OpenRouter's automatic prompt caching, for Anthropic models (cache_control)."""
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def name(self) -> str:
        return self._model

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        if self._missing_key:
            raise ModelError(f"{self._missing_key} is not set", transient=False)
        body: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages(system, messages),
            "max_tokens": self._max_tokens,
        }
        if tools:
            body["tools"] = [t.as_openai() for t in tools]
        if self._zdr and self._openrouter:
            # OpenRouter's routing option; other providers do not accept the field.
            body["provider"] = {"zdr": True}
        if self.cache_requested:
            # Automatic caching: the breakpoint follows the last cacheable block as a thread grows.
            body["cache_control"] = {"type": "ephemeral"}
        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise ModelError("model request timed out", transient=True) from exc
        except httpx.TransportError as exc:
            raise ModelError(
                f"model transport error: {type(exc).__name__}", transient=True
            ) from exc
        if response.status_code >= 400:
            raise ModelError(
                f"model returned HTTP {response.status_code}: {response.text[:300]}",
                transient=response.status_code in TRANSIENT_STATUS,
            )
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            error = payload.get("error") or {}
            raise ModelError(f"model returned no choices: {str(error)[:300]}", transient=True)
        reply = parse_assistant(choices[0].get("message") or {})
        return replace(reply, usage=parse_usage(payload))


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def parse_usage(payload: dict[str, Any]) -> Usage | None:
    """The response's usage block (OpenAI shape, with OpenRouter's cost and cache details)."""
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    cost = raw.get("cost")
    return Usage(
        input_tokens=_int(raw.get("prompt_tokens")),
        output_tokens=_int(raw.get("completion_tokens")),
        cached_tokens=_int(prompt.get("cached_tokens")) if isinstance(prompt, dict) else None,
        cache_write_tokens=(
            _int(prompt.get("cache_write_tokens")) if isinstance(prompt, dict) else None
        ),
        reasoning_tokens=(
            _int(completion.get("reasoning_tokens")) if isinstance(completion, dict) else None
        ),
        cost_usd=float(cost)
        if isinstance(cost, int | float) and not isinstance(cost, bool)
        else None,
        response_model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        generation_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
    )


def resolve_model(
    config: ModelConfig,
    *,
    api_key_env: str | None,
    timeout_seconds: int,
    zero_data_retention: bool = False,
    prompt_cache: PromptCache = "auto",
) -> OpenAICompatibleModel:
    """The configured model. `base_url` overrides the provider's endpoint for compatible servers.

    A missing key fails the first model call, not construction: `report` builds the same runtime
    and never calls the model.
    """
    provider = PROVIDERS.get(config.provider)
    base_url = str(config.base_url) if config.base_url else provider.base_url if provider else None
    if base_url is None:
        raise ValueError(
            f"unknown model provider {config.provider!r}; set PAID_MEDIA_MODEL_BASE_URL for an "
            "OpenAI-compatible endpoint"
        )
    key_env = api_key_env or (provider.key_env if provider else "PAID_MEDIA_MODEL_API_KEY_ENV")
    api_key = os.environ.get(key_env, "")
    return OpenAICompatibleModel(
        model=config.model,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        zero_data_retention=zero_data_retention,
        prompt_cache=prompt_cache,
        provider=config.provider,
        missing_key=None if api_key else key_env,
    )
