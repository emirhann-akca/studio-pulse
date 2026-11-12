"""Shared LLM provider layer (used by the market radar; reusable by any feature).

Pick a provider with the `llm:` block of the config and/or environment variables; the
environment wins, so a scheduled job and a one-off shell can differ without editing files.

    llm:
      provider: gemini            # gemini | openai | anthropic | fake | none
      model: gemini-3.5-flash-lite
      location: global            # Vertex AI location
      budget_usd: 0.50            # per-run cap
      prices: {my-model: [0.3, 2.5]}

Environment: PULSE_LLM_PROVIDER, PULSE_LLM_MODEL, PULSE_LLM_BUDGET_USD,
PULSE_LLM_PROJECT (else GOOGLE_CLOUD_PROJECT, CREATIVE_FORGE_PROJECT), PULSE_LLM_LOCATION,
PULSE_LLM_BASE_URL, PULSE_LLM_API_KEY, PULSE_LLM_JSON_MODE, ANTHROPIC_API_KEY.
Project ids and keys belong in the environment, never in a committed file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import (
    BudgetExceeded,
    Completion,
    ImagePart,
    LLMConfigError,
    LLMError,
    LLMOutputError,
    LLMRequest,
    LLMResponse,
    Provider,
    TransientLLMError,
    redact,
)
from .client import LLMClient
from .providers import (
    AnthropicProvider,
    FakeProvider,
    GeminiVertexProvider,
    OpenAICompatibleProvider,
)

__all__ = [
    "AnthropicProvider",
    "BudgetExceeded",
    "Completion",
    "FakeProvider",
    "GeminiVertexProvider",
    "ImagePart",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMOutputError",
    "LLMRequest",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "Provider",
    "TransientLLMError",
    "client_from_settings",
    "make_provider",
    "redact",
    "settings",
]

PROVIDERS = ("gemini", "openai", "anthropic", "fake", "none")
DEFAULT_MODEL = {
    "gemini": "gemini-3.5-flash-lite",
    "anthropic": "claude-haiku-5-5",
    "fake": "fake-1",
}


def settings(
    block: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Merge the config block with the environment (environment wins)."""
    env = os.environ if env is None else env
    s: dict[str, Any] = dict(block or {})

    def put(key: str, *names: str) -> None:
        for n in names:
            if env.get(n):
                s[key] = env[n]
                return

    put("provider", "PULSE_LLM_PROVIDER")
    put("model", "PULSE_LLM_MODEL")
    put("location", "PULSE_LLM_LOCATION")
    put("base_url", "PULSE_LLM_BASE_URL")
    put("json_mode", "PULSE_LLM_JSON_MODE")
    put("budget_usd", "PULSE_LLM_BUDGET_USD")
    s["provider"] = str(s.get("provider") or "none").lower()
    if s["provider"] not in PROVIDERS:
        raise LLMConfigError(f"llm provider must be one of {', '.join(PROVIDERS)}")
    s.setdefault("model", DEFAULT_MODEL.get(s["provider"], ""))
    if s.get("budget_usd") not in (None, ""):
        s["budget_usd"] = float(s["budget_usd"])
    # secrets and the project id come only from the environment
    s["_project"] = next(
        (
            env[n]
            for n in ("PULSE_LLM_PROJECT", "GOOGLE_CLOUD_PROJECT", "CREATIVE_FORGE_PROJECT")
            if env.get(n)
        ),
        None,
    )
    s["_api_key"] = env.get("PULSE_LLM_API_KEY") or None
    s["_anthropic_key"] = env.get("ANTHROPIC_API_KEY") or None
    return s


def make_provider(s: Mapping[str, Any], *, session: Any = None) -> Provider | None:
    p = s.get("provider", "none")
    if p == "none":
        return None
    if p == "fake":
        return FakeProvider(model=str(s.get("model") or "fake-1"))
    if p == "gemini":
        return GeminiVertexProvider(
            str(s.get("model")),
            project=s.get("_project"),
            location=str(s.get("location") or "global"),
            session=session,
            thinking_level=s.get("thinking_level", "low"),
        )
    if p == "openai":
        price = s.get("price")
        return OpenAICompatibleProvider(
            str(s.get("model") or ""),
            base_url=str(s.get("base_url") or ""),
            api_key=s.get("_api_key"),
            json_mode=str(s.get("json_mode") or "json_schema"),
            session=session,
            price=tuple(price) if price else (0.0, 0.0),
        )
    if p == "anthropic":
        return AnthropicProvider(
            str(s.get("model")), api_key=s.get("_anthropic_key"), effort=s.get("effort", "low")
        )
    raise LLMConfigError(f"unknown provider {p!r}")


def client_from_settings(
    s: Mapping[str, Any],
    *,
    state_dir: str | Path,
    provider: Provider | None = None,
    session: Any = None,
    run_id: str | None = None,
) -> LLMClient | None:
    """An LLMClient with cache and ledger under `state_dir` (None when provider is none)."""
    prov = provider or make_provider(s, session=session)
    if prov is None:
        return None
    base = Path(state_dir)
    return LLMClient(
        prov,
        cache_dir=Path(s["cache_dir"]) if s.get("cache_dir") else base / "llm-cache",
        ledger_path=Path(s["ledger"]) if s.get("ledger") else base / "llm-ledger.jsonl",
        budget_usd=s.get("budget_usd"),
        prices=s.get("prices") or {},
        timeout_s=float(s.get("timeout_s", 120)),
        max_retries=int(s.get("max_retries", 3)),
        run_id=run_id,
    )
