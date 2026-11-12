"""LLMClient: one provider plus everything a paid call needs around it.

- Content-hash cache: the key is SHA-256 over (provider, model, system, prompt, schema, image
  hashes, sampling); a request that was answered before is served from disk and never billed
  again. Only answers that passed validation are cached.
- Budget: a per-run cap in USD. Before each request the worst case (estimated input tokens plus
  max_output_tokens at the model's price) is checked against what this run has spent; a
  request that could cross the cap is not sent (BudgetExceeded). A model with no known price
  is refused while a cap is set.
- Ledger: one JSON line per attempt (tokens, cost, cached, repaired, ok, error) in a file, and
  a summary per run.
- Structured output: the answer is parsed and validated against the JSON schema; if invalid,
  one repair request quotes the problems and asks for corrected JSON. Still invalid ->
  LLMOutputError.
- Timeouts and retries: TransientLLMError (429, 5xx, timeouts) is retried with exponential
  backoff; anything else is raised at once.
- Secrets: every error text and ledger line passes through redact() with the provider's
  secrets; keys are never part of the cache key or the cache file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import pricing
from .base import (
    BudgetExceeded,
    LLMError,
    LLMOutputError,
    LLMRequest,
    LLMResponse,
    Provider,
    TransientLLMError,
    redact,
)
from .jsonschema import parse_json_text, validate

REPAIR_TEMPLATE = """{prompt}

Your previous answer could not be used:
{errors}

Previous answer:
{answer}

Return only the corrected JSON object, nothing else."""


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        *,
        cache_dir: str | Path | None = None,
        ledger_path: str | Path | None = None,
        budget_usd: float | None = None,
        prices: Mapping[str, Any] | None = None,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        run_id: str | None = None,
    ) -> None:
        self.provider = provider
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.budget_usd = budget_usd
        self.prices = dict(prices or {})
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.sleep = sleep
        self.run_id = run_id or dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
        self.spent_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.requests = 0
        self.cache_hits = 0
        self.entries: list[dict[str, Any]] = []

    # -- helpers ---------------------------------------------------------------------
    @property
    def price(self) -> tuple[float, float] | None:
        return pricing.price_for(self.provider.model, self.prices, self.provider.price)

    def key(self, req: LLMRequest) -> str:
        material = {
            "provider": self.provider.name,
            "model": self.provider.model,
            **req.cache_material(),
        }
        blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_file(self, key: str) -> Path | None:
        return self.cache_dir / key[:2] / f"{key}.json" if self.cache_dir else None

    def _redact(self, text: Any) -> str:
        return redact(text, self.provider.secrets())

    def _ledger(self, **row: Any) -> None:
        row = {
            "ts": _now(),
            "run_id": self.run_id,
            "provider": self.provider.name,
            "model": self.provider.model,
            **row,
        }
        if row.get("error"):
            row["error"] = self._redact(row["error"])[:400]
        self.entries.append(row)
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def estimate_usd(self, req: LLMRequest) -> float | None:
        price = self.price
        if price is None:
            return None
        chars = len(req.system) + len(req.prompt) + len(json.dumps(req.schema or {}))
        tokens_in = pricing.estimate_tokens(chars, len(req.images))
        return pricing.cost(price, tokens_in, req.max_output_tokens)

    def _reserve(self, req: LLMRequest) -> None:
        if self.budget_usd is None:
            return
        est = self.estimate_usd(req)
        if est is None:
            raise BudgetExceeded(
                f"no price for model {self.provider.model!r} and a budget is set: "
                "add it under llm.prices"
            )
        if self.spent_usd + est > self.budget_usd + 1e-12:
            raise BudgetExceeded(
                f"budget ${self.budget_usd:.4f}: spent ${self.spent_usd:.4f}, this request "
                f"could cost up to ${est:.4f}; not sent"
            )

    def _call(self, req: LLMRequest, purpose: str, key: str, repair: bool) -> Any:
        attempt = 0
        while True:
            self._reserve(req)
            attempt += 1
            t0 = time.perf_counter()
            try:
                self.requests += 1
                c = self.provider.complete(req, timeout_s=self.timeout_s)
            except TransientLLMError as exc:
                self._ledger(
                    purpose=purpose,
                    key=key,
                    attempt=attempt,
                    ok=False,
                    cached=False,
                    repaired=repair,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    error=str(exc),
                )
                if attempt > self.max_retries:
                    raise TransientLLMError(self._redact(exc)) from None
                self.sleep(min(self.backoff_s * 2 ** (attempt - 1), 60.0))
                continue
            except LLMError as exc:
                self._ledger(
                    purpose=purpose,
                    key=key,
                    attempt=attempt,
                    ok=False,
                    cached=False,
                    repaired=repair,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    error=str(exc),
                )
                raise type(exc)(self._redact(exc)) from None
            usd = pricing.cost(self.price, c.input_tokens, c.output_tokens)
            self.spent_usd += usd
            self.input_tokens += c.input_tokens
            self.output_tokens += c.output_tokens
            self._ledger(
                purpose=purpose,
                key=key,
                attempt=attempt,
                ok=True,
                cached=False,
                repaired=repair,
                input_tokens=c.input_tokens,
                output_tokens=c.output_tokens,
                cost_usd=round(usd, 8),
                seconds=round(time.perf_counter() - t0, 3),
                stop_reason=c.stop_reason,
            )
            return c

    # -- public ------------------------------------------------------------------------
    def generate(self, req: LLMRequest) -> LLMResponse:
        key = self.key(req)
        cf = self._cache_file(key)
        if cf and cf.exists():
            data = json.loads(cf.read_text(encoding="utf-8"))
            self.cache_hits += 1
            self._ledger(
                purpose=req.purpose,
                key=key,
                attempt=0,
                ok=True,
                cached=True,
                repaired=False,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
            )
            return LLMResponse(
                text=data["text"],
                data=data.get("data"),
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                model=data.get("model", self.provider.model),
                provider=self.provider.name,
                cached=True,
                repaired=bool(data.get("repaired")),
                key=key,
            )
        c = self._call(req, req.purpose, key, repair=False)
        tokens_in, tokens_out = c.input_tokens, c.output_tokens
        text, data, repaired = c.text, None, False
        if req.schema is not None:
            data, errors = self._check(text, req.schema)
            if errors:
                fix = LLMRequest(
                    prompt=REPAIR_TEMPLATE.format(
                        prompt=req.prompt,
                        errors="\n".join(f"- {e}" for e in errors[:12]),
                        answer=text[:4000],
                    ),
                    system=req.system,
                    schema=req.schema,
                    images=req.images,
                    temperature=req.temperature,
                    max_output_tokens=req.max_output_tokens,
                    purpose=f"{req.purpose}:repair",
                )
                c2 = self._call(fix, fix.purpose, key, repair=True)
                tokens_in += c2.input_tokens
                tokens_out += c2.output_tokens
                text, repaired = c2.text, True
                data, errors = self._check(text, req.schema)
                if errors:
                    self._ledger(
                        purpose=req.purpose,
                        key=key,
                        attempt=0,
                        ok=False,
                        cached=False,
                        repaired=True,
                        input_tokens=0,
                        output_tokens=0,
                        cost_usd=0.0,
                        error="invalid after repair: " + "; ".join(errors[:5]),
                    )
                    raise LLMOutputError(
                        "answer invalid after one repair: " + "; ".join(errors[:5])
                    )
        usd = pricing.cost(self.price, tokens_in, tokens_out)
        if cf:
            cf.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "text": text,
                "data": data,
                "model": c.model,
                "repaired": repaired,
                "created_at": _now(),
            }
            fd, tmp = tempfile.mkstemp(dir=cf.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, cf)
        return LLMResponse(
            text=text,
            data=data,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=usd,
            model=c.model,
            provider=self.provider.name,
            cached=False,
            repaired=repaired,
            key=key,
        )

    @staticmethod
    def _check(text: str, schema: dict[str, Any]) -> tuple[Any, list[str]]:
        try:
            data = parse_json_text(text)
        except ValueError as exc:
            return None, [f"not valid JSON: {exc}"]
        return data, validate(data, schema)

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.spent_usd, 6),
            "budget_usd": self.budget_usd,
        }
