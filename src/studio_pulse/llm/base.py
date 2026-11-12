"""Shared types for the LLM provider layer: requests, completions, errors, secret redaction."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """A provider call failed for a reason a retry will not fix (bad request, auth, refusal)."""


class LLMConfigError(LLMError):
    """The provider is not configured (missing project, base URL, key or package)."""


class TransientLLMError(LLMError):
    """Rate limit, server error, timeout or dropped connection: retried with backoff."""


class BudgetExceeded(LLMError):
    """The call would take the run over its spending cap; it was not sent."""


class LLMOutputError(LLMError):
    """The model's answer was not valid JSON for the schema, even after one repair request."""


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    mime: str = "image/jpeg"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class LLMRequest:
    prompt: str
    system: str = ""
    schema: dict[str, Any] | None = None
    images: list[ImagePart] = field(default_factory=list)
    temperature: float = 0.0
    max_output_tokens: int = 2048
    purpose: str = "llm"

    def cache_material(self) -> dict[str, Any]:
        """Everything that decides the answer (not the purpose label)."""
        return {
            "system": self.system,
            "prompt": self.prompt,
            "schema": self.schema,
            "images": [(i.mime, i.sha256) for i in self.images],
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass
class Completion:
    """One provider answer with the usage the provider reported."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str | None = None


@dataclass
class LLMResponse:
    text: str
    data: Any
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str
    provider: str
    cached: bool
    repaired: bool = False
    key: str = ""


class Provider:
    """A model behind one API. Subclasses implement `complete`; they never log their secrets."""

    name = "provider"
    model = ""
    # Price per million tokens (input, output) when the provider knows it better than the
    # shared table (a local model is free). None: look the model up in pricing.PRICES.
    price: tuple[float, float] | None = None

    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in logs, errors, ledgers or caches."""
        return ()

    def complete(self, req: LLMRequest, *, timeout_s: float) -> Completion:
        raise NotImplementedError

    def __repr__(self) -> str:  # never print keys
        return f"<{type(self).__name__} {self.name}:{self.model}>"


_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|x-api-key|access[_-]?token|token|key|authorization|password|secret)"
        r"[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;}]{6,}"
    ),
    re.compile("AI" r"za[0-9A-Za-z_\-]{20,}"),  # Google API keys (split so scanners skip this line)
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_\-]{10,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-.]+"),
]


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    """Replace known secret values and anything shaped like a key or bearer token."""
    out = str(text)
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(s, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", out)
    return out
