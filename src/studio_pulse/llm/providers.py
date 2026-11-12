"""LLM providers: Gemini on Vertex AI, any OpenAI-compatible server, Anthropic Claude, and a fake.

- Gemini (Vertex AI REST, `:generateContent`): credentials from Application Default
  Credentials (`gcloud auth application-default login`), project and location from the
  environment. JSON output through `responseMimeType` + `responseSchema`; images inline.
- OpenAI-compatible (`/chat/completions`): llama.cpp `llama-server` is the recommended local
  server (json_schema output, GGUF LoRA adapters loaded without merging via `--lora`);
  Ollama, LM Studio and vLLM speak the same API. For example a Windows desktop serving LoRA
  fine-tunes of Qwen 7B or Llama 8B. Base URL and model from the environment; key optional.
  No LiteLLM: plain requests keeps the dependency surface small (LiteLLM 1.82.7/1.82.8 were
  malicious releases, PYSEC-2026-2).
- Anthropic (official `anthropic` SDK, optional extra): JSON output through
  `output_config.format`; key from ANTHROPIC_API_KEY.
- Fake: deterministic answers for tests and offline runs; never touches the network.

Providers raise TransientLLMError for what a retry can fix; LLMClient does the retrying,
so every attempt lands in the ledger.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import requests

from .base import (
    Completion,
    LLMConfigError,
    LLMError,
    LLMRequest,
    Provider,
    TransientLLMError,
    redact,
)
from .jsonschema import minimal_instance

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}


def _post_json(
    session: requests.Session,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    try:
        r = session.post(url, data=json.dumps(body), headers=headers, timeout=timeout_s)
    except requests.Timeout as exc:
        raise TransientLLMError(f"timeout: {redact(exc, secrets)}") from None
    except requests.ConnectionError as exc:
        raise TransientLLMError(f"connection error: {redact(exc, secrets)}") from None
    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            raise TransientLLMError("response was not JSON") from None
    text = redact(r.text[:600], secrets)
    if r.status_code in RETRY_STATUS:
        raise TransientLLMError(f"HTTP {r.status_code}: {text}")
    raise LLMError(f"HTTP {r.status_code}: {text}")


# -- Gemini on Vertex AI -----------------------------------------------------------------

_VERTEX_KEYS = ("description", "enum", "required", "minItems", "maxItems", "minimum", "maximum")


def to_vertex_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema -> the OpenAPI subset Vertex `responseSchema` accepts."""
    out: dict[str, Any] = {}
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if len(non_null) < len(t):
            out["nullable"] = True
        t = non_null[0] if non_null else "string"
    if t:
        out["type"] = str(t).upper()
    for k in _VERTEX_KEYS:
        if k in schema:
            out[k] = schema[k]
    if isinstance(schema.get("properties"), dict):
        out["properties"] = {k: to_vertex_schema(v) for k, v in schema["properties"].items()}
    if isinstance(schema.get("items"), dict):
        out["items"] = to_vertex_schema(schema["items"])
    return out


class GeminiVertexProvider(Provider):
    name = "gemini"

    def __init__(
        self,
        model: str = "gemini-3.5-flash-lite",
        *,
        project: str | None,
        location: str = "global",
        session: requests.Session | None = None,
        thinking_level: str | None = "low",
    ) -> None:
        if not project:
            raise LLMConfigError(
                "Gemini on Vertex AI needs a Google Cloud project: set PULSE_LLM_PROJECT "
                "(or GOOGLE_CLOUD_PROJECT) in the environment"
            )
        self.model = model
        self.project = project
        self.location = location or "global"
        self._session = session
        self.thinking_level = thinking_level or None
        self._thinking_ok = True

    def secrets(self) -> tuple[str, ...]:
        return (self.project,)

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            try:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession
            except ImportError as exc:
                raise LLMConfigError(
                    "the Gemini provider needs google-auth (pip install 'studio-pulse[gcp]')"
                ) from exc
            try:
                creds, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            except Exception as exc:  # DefaultCredentialsError, without importing it eagerly
                raise LLMConfigError(
                    "no Application Default Credentials: run gcloud auth application-default login"
                ) from exc
            self._session = AuthorizedSession(creds)
        return self._session

    def url(self) -> str:
        host = (
            "aiplatform.googleapis.com"
            if self.location == "global"
            else f"{self.location}-aiplatform.googleapis.com"
        )
        return (
            f"https://{host}/v1/projects/{self.project}/locations/{self.location}"
            f"/publishers/google/models/{self.model}:generateContent"
        )

    def body(self, req: LLMRequest) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [
            {"inlineData": {"mimeType": im.mime, "data": base64.b64encode(im.data).decode()}}
            for im in req.images
        ]
        parts.append({"text": req.prompt})
        gen: dict[str, Any] = {
            "temperature": req.temperature,
            "maxOutputTokens": req.max_output_tokens,
        }
        if req.schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = to_vertex_schema(req.schema)
        if self.thinking_level and self._thinking_ok:
            # Gemini 3 models take a thinking level; thinking tokens are billed as output.
            gen["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen,
        }
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        return body

    def complete(self, req: LLMRequest, *, timeout_s: float) -> Completion:
        headers = {"Content-Type": "application/json", "x-goog-user-project": self.project}
        try:
            d = _post_json(
                self.session, self.url(), self.body(req), headers, timeout_s, self.secrets()
            )
        except LLMError as exc:
            # A model that does not take thinkingConfig answers 400 before doing any work
            # (nothing billed): send the request once more without it.
            if not (
                self._thinking_ok
                and "thinking" in str(exc).lower()
                and str(exc).startswith("HTTP 400")
            ):
                raise
            self._thinking_ok = False
            d = _post_json(
                self.session, self.url(), self.body(req), headers, timeout_s, self.secrets()
            )
        cands = d.get("candidates") or []
        if not cands:
            block = (d.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(f"no candidates (blocked: {block})")
        c0 = cands[0]
        text = "".join(
            p.get("text", "")
            for p in (c0.get("content") or {}).get("parts") or []
            if not p.get("thought")
        )
        u = d.get("usageMetadata") or {}
        return Completion(
            text=text,
            input_tokens=int(u.get("promptTokenCount") or 0),
            output_tokens=int(u.get("candidatesTokenCount") or 0)
            + int(u.get("thoughtsTokenCount") or 0),
            model=d.get("modelVersion") or self.model,
            stop_reason=c0.get("finishReason"),
        )


# -- OpenAI-compatible --------------------------------------------------------------------


class OpenAICompatibleProvider(Provider):
    name = "openai_compatible"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        json_mode: str = "json_schema",
        session: requests.Session | None = None,
        price: tuple[float, float] | None = (0.0, 0.0),
    ) -> None:
        if not base_url:
            raise LLMConfigError(
                "the OpenAI-compatible provider needs PULSE_LLM_BASE_URL "
                "(for example http://192.168.1.20:11434/v1)"
            )
        if not model:
            raise LLMConfigError("the OpenAI-compatible provider needs PULSE_LLM_MODEL")
        if json_mode not in ("json_schema", "json_object", "none"):
            raise LLMConfigError("json_mode must be json_schema, json_object or none")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._key = api_key or None
        self.json_mode = json_mode
        self.session = session or requests.Session()
        self.price = price

    def secrets(self) -> tuple[str, ...]:
        return (self._key,) if self._key else ()

    def body(self, req: LLMRequest) -> dict[str, Any]:
        if req.images:
            content: Any = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{im.mime};base64,{base64.b64encode(im.data).decode()}"
                    },
                }
                for im in req.images
            ] + [{"type": "text", "text": req.prompt}]
        else:
            content = req.prompt
        messages = ([{"role": "system", "content": req.system}] if req.system else []) + [
            {"role": "user", "content": content}
        ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_output_tokens,
        }
        if req.schema is not None and self.json_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": req.schema},
            }
        elif req.schema is not None and self.json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def complete(self, req: LLMRequest, *, timeout_s: float) -> Completion:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        d = _post_json(
            self.session,
            f"{self.base_url}/chat/completions",
            self.body(req),
            headers,
            timeout_s,
            self.secrets(),
        )
        choices = d.get("choices") or []
        if not choices:
            raise LLMError("no choices in the response")
        msg = choices[0].get("message") or {}
        u = d.get("usage") or {}
        return Completion(
            text=msg.get("content") or "",
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            model=d.get("model") or self.model,
            stop_reason=choices[0].get("finish_reason"),
        )


# -- Anthropic Claude ---------------------------------------------------------------------

_ANTHROPIC_KEYS = ("type", "description", "enum", "const", "required")


def to_anthropic_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The structured-output subset: no numeric or length constraints, closed objects.

    The dropped constraints are still enforced by LLMClient's own validation."""
    out = {k: schema[k] for k in _ANTHROPIC_KEYS if k in schema}
    if isinstance(schema.get("properties"), dict):
        out["properties"] = {k: to_anthropic_schema(v) for k, v in schema["properties"].items()}
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        out.setdefault("required", list(out.get("properties", {})))
    if isinstance(schema.get("items"), dict):
        out["items"] = to_anthropic_schema(schema["items"])
    if "anyOf" in schema:
        out["anyOf"] = [to_anthropic_schema(x) for x in schema["anyOf"]]
    return out


CLAUDE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")


class AnthropicProvider(Provider):
    """Claude through the official SDK (`pip install 'studio-pulse[anthropic]'`).

    Sampling parameters are not sent (current API versions reject temperature); depth is set
    with `output_config.effort`. JSON comes from structured outputs (`output_config.format`),
    never from a forced tool_choice (Opus/Sonnet 5.5 answer that with a 400). The SDK's own
    retries are off so LLMClient's retries, budget and ledger see every attempt.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-haiku-5-5",
        *,
        api_key: str | None,
        effort: str | None = "low",
        client: Any = None,
    ) -> None:
        if client is None and not api_key:
            raise LLMConfigError("the Anthropic provider needs ANTHROPIC_API_KEY")
        self.model = model
        self._key = api_key
        self.effort = effort
        self._client = client

    def secrets(self) -> tuple[str, ...]:
        return (self._key,) if self._key else ()

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMConfigError(
                    "the Anthropic provider needs the anthropic package "
                    "(pip install 'studio-pulse[anthropic]')"
                ) from exc
            self._client = anthropic.Anthropic(api_key=self._key, max_retries=0)
        return self._client

    def params(self, req: LLMRequest) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": im.mime,
                    "data": base64.b64encode(im.data).decode(),
                },
            }
            for im in req.images
        ]
        content.append({"type": "text", "text": req.prompt})
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": req.max_output_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if req.system:
            params["system"] = req.system
        output_config: dict[str, Any] = {}
        if req.schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": to_anthropic_schema(req.schema),
            }
        if self.effort:
            output_config["effort"] = self.effort
        if output_config:
            params["output_config"] = output_config
        return params

    def complete(self, req: LLMRequest, *, timeout_s: float) -> Completion:
        client = self.client
        try:
            msg = client.messages.create(**self.params(req), timeout=timeout_s)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            kind = type(exc).__name__
            text = redact(f"{kind}: {exc}", self.secrets())
            transient = kind in (
                "RateLimitError",
                "InternalServerError",
                "APIConnectionError",
                "APITimeoutError",
                "OverloadedError",
            ) or (isinstance(status, int) and status in RETRY_STATUS)
            if transient:
                raise TransientLLMError(text) from None
            raise LLMError(text) from None
        if getattr(msg, "stop_reason", None) == "refusal":
            raise LLMError("the model declined the request (stop_reason refusal)")
        text = "".join(
            getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
        )
        u = msg.usage
        return Completion(
            text=text,
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            model=getattr(msg, "model", self.model),
            stop_reason=getattr(msg, "stop_reason", None),
        )


# -- Fake ---------------------------------------------------------------------------------


class FakeProvider(Provider):
    """Deterministic, offline. `answer(req)` returns the text (or a JSON-able value);
    without one, the smallest schema-valid JSON is returned. Token counts are derived from
    the text length so budgets and ledgers can be tested."""

    name = "fake"

    def __init__(
        self,
        answer: Callable[[LLMRequest], Any] | None = None,
        *,
        model: str = "fake-1",
        price: tuple[float, float] | None = (0.0, 0.0),
        errors: list[Exception] | None = None,
    ) -> None:
        self.model = model
        self.answer = answer
        self.price = price
        self.errors = list(errors or [])
        self.calls: list[LLMRequest] = []

    def complete(self, req: LLMRequest, *, timeout_s: float) -> Completion:
        self.calls.append(req)
        if self.errors:
            raise self.errors.pop(0)
        if self.answer is not None:
            out = self.answer(req)
        elif req.schema is not None:
            out = minimal_instance(req.schema)
        else:
            out = "ok"
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return Completion(
            text=text,
            input_tokens=max(1, len(req.system + req.prompt) // 4) + 258 * len(req.images),
            output_tokens=max(1, len(text) // 4),
            model=self.model,
            stop_reason="stop",
        )
