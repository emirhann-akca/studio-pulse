"""Labels of every number in a game dossier, in the radar's style (digest.py), plus the kinds a
dossier adds: LLM output, cited benchmarks, studio assumptions and rule scores."""

from __future__ import annotations

from typing import Any

BASES = (
    "observed",  # read from a chart, Lookup or a store page field Apple/Google document
    "store_reported",  # an undocumented Play page field (realInstalls) or a sample of its reviews
    "estimate",  # a range with low, high, method and n
    "llm",  # counted from LLM labels whose quotes were checked against the source text
    "benchmark",  # a third-party benchmark with its source
    "assumption",  # a studio assumption from the config or benchmarks.yaml
    "rule_score",  # a deterministic score of the decision rubric
    "platform_reported",  # what an ad platform's transparency data says
)

LABELS = {
    "tr": {
        "observed": "GÖZLEM",
        "store_reported": "MAĞAZA VERİSİ",
        "estimate": "TAHMİN",
        "llm": "LLM",
        "benchmark": "KIYAS",
        "assumption": "VARSAYIM",
        "rule_score": "KURAL PUANI",
        "platform_reported": "PLATFORM VERİSİ",
    },
    "en": {
        "observed": "OBSERVED",
        "store_reported": "STORE-REPORTED",
        "estimate": "ESTIMATE",
        "llm": "LLM",
        "benchmark": "BENCHMARK",
        "assumption": "ASSUMPTION",
        "rule_score": "RULE SCORE",
        "platform_reported": "PLATFORM-REPORTED",
    },
}


class LabelError(ValueError):
    """A number without a basis, or an estimate without a range."""


def num(value: Any, basis: str, **extra: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if basis not in BASES:
        raise LabelError(f"unknown basis {basis!r}")
    return {"value": value, "basis": basis, **extra}


def estimate(low: float, high: float, method: str, n: int, **extra: Any) -> dict[str, Any]:
    if low is None or high is None or not method or not n:
        raise LabelError("an estimate needs low, high, method and n")
    return {"low": low, "high": high, "method": method, "n": n, "basis": "estimate", **extra}


# Top-level keys that hold run metadata or raw inputs, not market numbers.
UNCHECKED = ("meta", "texts", "screenshots")


def check_labels(facts: dict[str, Any]) -> list[str]:
    """Every number outside meta/texts/screenshots must sit in a dict with a basis."""
    problems: list[str] = []

    def walk(node: Any, path: str, has_basis: bool) -> None:
        if isinstance(node, dict):
            b = has_basis or "basis" in node
            if node.get("basis") not in (None, *BASES):
                problems.append(f"{path}: unknown basis {node.get('basis')!r}")
            if node.get("basis") == "estimate" and not all(
                node.get(k) not in (None, "") for k in ("low", "high", "method", "n")
            ):
                problems.append(f"{path}: estimate without low/high/method/n")
            for k, v in node.items():
                if path == "$" and k in UNCHECKED:
                    continue
                walk(v, f"{path}.{k}", b)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", has_basis)
        elif isinstance(node, (int, float)) and not isinstance(node, bool) and not has_basis:
            problems.append(f"{path}: number {node} without a basis")

    walk(facts, "$", False)
    return problems
