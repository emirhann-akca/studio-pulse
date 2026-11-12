"""Token prices (USD per million tokens, (input, output)) used for the cost ledger and the budget cap.

Prices drift: override them per model in the config (`llm: prices: {model: [in, out]}`).
A model without a price is refused while a budget is set, so a cap can never be bypassed
by an unknown model.

Sources (checked 12 Nov 2026):
- Gemini: aistudio.google.com/docs/pricing, paid Standard tier, output price includes thinking
  tokens. gemini-3.8-flash rises to 1.50/7.50 on 1 Jan 2027 and gemini-4-argon to 4/20 on
  1 Dec 2026. The 2.5 models retired on Vertex AI on 20 Oct 2026 (model-versions page) and
  are not listed. gemini-3.5-flash had no price on that page: set it under llm.prices.
- Claude: Anthropic's model table (claude-opus-5-5 4/20, claude-sonnet-5-5 2/10,
  claude-haiku-5-5 0.10/0.50 for prompts up to 100K tokens).
"""

from __future__ import annotations

from collections.abc import Mapping

PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-4-argon": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "fake-1": (0.0, 0.0),
}

# Conservative pre-call estimate: one token per ~2.5 characters of text, a flat count per image.
CHARS_PER_TOKEN = 2.5
IMAGE_TOKENS = 1800


def price_for(
    model: str,
    overrides: Mapping[str, tuple[float, float] | list[float]] | None = None,
    provider_price: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    if overrides and model in overrides:
        a, b = overrides[model]
        return float(a), float(b)
    if provider_price is not None:
        return provider_price
    return PRICES.get(model)


def cost(price: tuple[float, float] | None, input_tokens: int, output_tokens: int) -> float:
    if price is None:
        return 0.0
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def estimate_tokens(text_chars: int, images: int) -> int:
    return int(text_chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS + 16
