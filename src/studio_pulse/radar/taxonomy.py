"""The radar taxonomy, quote matching and the deterministic keyword tagger."""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DIMENSIONS = {
    # claim name in the LLM schema -> (taxonomy section, profile dimension)
    "mechanic_primary": ("mechanics", "mechanic_primary"),
    "mechanics_secondary": ("mechanics", "mechanic"),
    "twists": ("twists", "twist"),
    "themes": ("themes", "theme"),
    "meta_layers": ("meta_layers", "meta"),
    "monetization_features": ("monetization_features", "monetization_feature"),
}


@dataclass(frozen=True)
class Taxonomy:
    data: dict[str, Any]

    def values(self, section: str) -> list[str]:
        return list(self.data.get(section) or {})

    def keywords(self, section: str, value: str) -> list[str]:
        return list(((self.data.get(section) or {}).get(value) or {}).get("keywords") or [])

    def gamerefinery(self, mechanic: str | None) -> str | None:
        m = (self.data.get("mechanics") or {}).get(mechanic or "") or {}
        return m.get("gamerefinery")

    def forge(self, mechanic: str | None) -> str:
        m = (self.data.get("mechanics") or {}).get(mechanic or "") or {}
        return m.get("forge") or "other"

    def hooks(self, mechanic: str | None) -> list[str]:
        h = self.data.get("hooks") or {}
        return list(h.get(mechanic or "") or h.get("default") or [])


@functools.lru_cache(maxsize=4)
def load(path: str | None = None) -> Taxonomy:
    if path:
        text = Path(path).read_text(encoding="utf-8")
    else:
        text = (
            resources.files("studio_pulse.radar")
            .joinpath("taxonomy.yaml")
            .read_text(encoding="utf-8")
        )
    return Taxonomy(yaml.safe_load(text) or {})


# -- quotes and numbers ------------------------------------------------------------------

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def normalize(text: str) -> str:
    """Casefold, unify quotes/dashes, collapse whitespace (for verbatim-quote checks)."""
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTES).casefold()
    return " ".join(t.split())


def quote_in(quote: str, field_text: str | None) -> bool:
    q = normalize(quote)
    return bool(q) and len(re.sub(r"\W", "", q)) >= 3 and q in normalize(field_text or "")


# Tokens that contain a digit but are names, not quantities.
_ALLOWED_NUMERIC = re.compile(r"(?i)\b(?:match[- ]?3|merge[- ]?[23]|[23]d|2048)\b")
_DIGIT = re.compile(r"\d")


def has_number(text: str | None) -> bool:
    """True when `text` states a number (mechanic names such as match-3 or 3D excepted)."""
    return bool(_DIGIT.search(_ALLOWED_NUMERIC.sub("", text or "")))


# -- keyword tagger ------------------------------------------------------------------------


def _find(phrase: str, text: str) -> re.Match[str] | None:
    pat = r"(?<![\w])" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"(?![\w])"
    return re.search(pat, text, flags=re.I)


def keyword_claims(fields: dict[str, str], tax: Taxonomy) -> list[dict[str, Any]]:
    """Claims found by whole-word keyword match, each with the matched text as its quote.

    The primary mechanic is the earliest match in the most specific field (name, then store
    tags, then description); other matched mechanics become secondary. The tagger is a cheap
    baseline for weekly trends; an LLM profile, when present, replaces it.
    """
    order = [
        k
        for k in (
            "apple.name",
            "play.categories",
            "apple.description",
            "play.description",
            "play.summary",
        )
        if fields.get(k)
    ]
    claims: list[dict[str, Any]] = []
    mech_hits: list[tuple[int, int, str, str, str]] = []
    for section, dimension in (
        ("mechanics", None),
        ("twists", "twist"),
        ("themes", "theme"),
        ("meta_layers", "meta"),
        ("monetization_features", "monetization_feature"),
    ):
        for value in tax.values(section):
            best = None
            for fi, field in enumerate(order):
                for kw in tax.keywords(section, value):
                    m = _find(kw, fields[field])
                    if m and (best is None or (fi, m.start()) < best[:2]):
                        best = (fi, m.start(), field, m.group(0))
                if best is not None and best[0] == fi:
                    break
            if best is None:
                continue
            if section == "mechanics":
                mech_hits.append((best[0], best[1], value, best[2], best[3]))
            else:
                claims.append(
                    {"dimension": dimension, "value": value, "field": best[2], "quote": best[3]}
                )
    mech_hits.sort()
    for i, (_, _, value, field, quote) in enumerate(mech_hits):
        claims.insert(
            i,
            {
                "dimension": "mechanic_primary" if i == 0 else "mechanic",
                "value": value,
                "field": field,
                "quote": quote,
            },
        )
    return claims
