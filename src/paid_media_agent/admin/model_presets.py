"""Model provider cards for the setup console and CLI: presets and default key names."""

from __future__ import annotations

from paid_media_agent.config import Settings
from paid_media_agent.domain.common import JsonValue
from paid_media_agent.harness.models import PROVIDERS

MODEL_PRESETS: tuple[dict[str, JsonValue], ...] = (
    {
        "id": "anthropic",
        "label": "Anthropic",
        "logo": "anthropic",
        "model": "anthropic:claude-sonnet-4-6",
        "key": "ANTHROPIC_API_KEY",
        "url": "https://console.anthropic.com/settings/keys",
        "recommended": True,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "logo": "openai",
        "model": "openai:gpt-5.5",
        "key": "OPENAI_API_KEY",
        "url": "https://platform.openai.com/api-keys",
    },
    {
        "id": "google",
        "label": "Google",
        "logo": "gemini",
        "model": "google_genai:gemini-3-flash",
        "key": "GOOGLE_API_KEY",
        "url": "https://aistudio.google.com/app/apikey",
        "note": "Portable selector",
    },
    {
        "id": "groq",
        "label": "Groq",
        "logo": "groq",
        "model": "groq:llama-3.3-70b-versatile",
        "key": "GROQ_API_KEY",
        "url": "https://console.groq.com/keys",
        "note": "Fast open models",
    },
    {
        "id": "xai",
        "label": "xAI",
        "logo": "xai",
        "model": "xai:grok-4",
        "key": "XAI_API_KEY",
        "url": "https://console.x.ai/",
        "note": "Grok",
    },
    {
        "id": "mistral",
        "label": "Mistral",
        "logo": "mistral",
        "model": "mistralai:mistral-large-latest",
        "key": "MISTRAL_API_KEY",
        "url": "https://console.mistral.ai/api-keys",
        "note": "Open weights",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "logo": "deepseek",
        "model": "deepseek:deepseek-chat",
        "key": "DEEPSEEK_API_KEY",
        "url": "https://platform.deepseek.com/api_keys",
        "note": "Open weights",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "logo": "openrouter",
        "model": "openai:moonshotai/kimi-k2",
        "key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "url": "https://openrouter.ai/keys",
        "note": "Kimi, GLM, and more",
    },
    {
        "id": "moonshot",
        "label": "Kimi",
        "logo": "moonshot",
        "model": "openai:kimi-k2-0905-preview",
        "key": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.ai/v1",
        "url": "https://platform.moonshot.ai/",
        "note": "OpenAI-compatible",
    },
    {
        "id": "zhipu",
        "label": "GLM",
        "logo": "zhipu",
        "model": "openai:glm-4.6",
        "key": "ZHIPU_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "url": "https://open.bigmodel.cn/",
        "note": "OpenAI-compatible",
    },
    {
        "id": "custom",
        "label": "Custom",
        "logo": "custom",
        "model": "",
        "key": "",
        "url": "",
        "note": "Your provider, key, and base URL",
    },
)
"""Model provider cards for the wizard. Key names are allowlisted env names; nothing else is written.

OpenAI-compatible presets use the `openai:` prefix with a base URL and their own key env var,
which `PAID_MEDIA_MODEL_API_KEY_ENV` hands to the client. Examples are configuration defaults; the model picker reads the provider API.
"""


def model_preset_payloads() -> list[dict[str, JsonValue]]:
    """Provider setup metadata. Available models come from each provider's live API."""
    return [dict(preset) for preset in MODEL_PRESETS]


def model_key_env(settings: Settings) -> str | None:
    """The env var that must hold the key for the configured model."""
    if settings.paid_media_model_api_key_env:
        return settings.paid_media_model_api_key_env
    provider = PROVIDERS.get(settings.model_settings().provider)
    return provider.key_env if provider else None


def provider_supported(settings: Settings) -> bool:
    """A known provider, or any OpenAI-compatible endpoint named by its base URL."""
    return settings.model_settings().provider in PROVIDERS or bool(
        settings.paid_media_model_base_url
    )
