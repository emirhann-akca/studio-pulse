"""Creative IDs from ad names: a pluggable parser with creative-forge's format as default.

Contract (any parser): a callable `parse(ad_name: str) -> Mapping | None`.
Return None when the name is not one of your creatives; otherwise a mapping
with at least `creative_id` (a string) and optionally `concept`. Connectors
call it once per distinct ad name and store `creative_id` / `creative_concept`
next to the network's own ad id, so the creative scorecard groups every
network's spend for one creative.

The default parser understands the ad names creative-forge writes:

    cf1.<spec>.<concept>.<hook>.<segment>.<end_card>.<format>.<language>.<creative_id>
    cf1.puzzle-ads.dust-blower.blower.lvl21.fill.9x16.tr.4be1d0c2a9

where creative_id = first 10 hex digits of sha256("cf1|spec|concept|hook|
segment|end_card|format|language"). The name is found anywhere inside a
longer network name ("... - Copy (2)"), case-insensitively, and a name whose
id does not match its components is rejected, so a hand-edited name is never
booked against the wrong creative.

Configure another parser with `ua: {creative_parser: ...}` (or per source):
  "regex:<pattern>"          a regular expression with a named group
                             `creative_id` (and optionally `concept`)
  "package.module:function"  any importable callable following the contract
  "none"                     do not parse; creative_id stays empty
"""

from __future__ import annotations

import hashlib
import importlib
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

Parser = Callable[[str], Mapping[str, Any] | None]

PREFIX = "cf1"
ID_LEN = 10
_COMPONENT = r"[a-z0-9][a-z0-9_-]{0,63}"
_FORMAT = r"9x16|4x5"
_LANG = r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?"
CF1_PATTERN = (
    rf"{PREFIX}\.(?P<spec>{_COMPONENT})\.(?P<concept>{_COMPONENT})\.(?P<hook>{_COMPONENT})"
    rf"\.(?P<segment>{_COMPONENT})\.(?P<end_card>{_COMPONENT})\.(?P<format>{_FORMAT})"
    rf"\.(?P<language>{_LANG})\.(?P<creative_id>[0-9a-f]{{{ID_LEN}}})"
)
_FIND = re.compile(CF1_PATTERN, re.IGNORECASE)
_PARTS = ("spec", "concept", "hook", "segment", "end_card", "format", "language")


def cf1_id(
    spec: str, concept: str, hook: str, segment: str, end_card: str, fmt: str, lang: str
) -> str:
    """creative-forge's deterministic creative id for these seven components."""
    joined = "|".join((PREFIX, spec, concept, hook, segment, end_card, fmt, lang))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:ID_LEN]


def cf1_name(
    spec: str, concept: str, hook: str, segment: str, end_card: str, fmt: str, lang: str
) -> str:
    parts = (spec, concept, hook, segment, end_card, fmt, lang)
    return ".".join((PREFIX, *parts, cf1_id(*parts)))


def parse_cf1(ad_name: str) -> dict[str, str] | None:
    """The first valid creative-forge name inside `ad_name`, or None."""
    if not ad_name:
        return None
    for m in _FIND.finditer(ad_name):
        g = {k: v.lower() for k, v in m.groupdict().items()}
        if cf1_id(*(g[p] for p in _PARTS)) == g["creative_id"]:
            return g
    return None


def regex_parser(pattern: str) -> Parser:
    rx = re.compile(pattern)
    if "creative_id" not in rx.groupindex:
        raise ValueError("creative_parser regex needs a named group (?P<creative_id>...)")

    def parse(ad_name: str) -> dict[str, str] | None:
        m = rx.search(ad_name or "")
        if not m or not m.group("creative_id"):
            return None
        return {k: v for k, v in m.groupdict().items() if v is not None}

    return parse


@lru_cache(maxsize=16)
def load_parser(spec: str | None) -> Parser | None:
    """Resolve a creative_parser setting (see the module docstring)."""
    if spec in (None, "", "default", "cf1"):
        return parse_cf1
    if spec == "none":
        return None
    if spec.startswith("regex:"):
        return regex_parser(spec[len("regex:") :])
    module, sep, attr = spec.partition(":")
    if not sep or not module or not attr:
        raise ValueError(
            f"creative_parser {spec!r}: use 'regex:<pattern>', 'module:function' or 'none'"
        )
    fn = getattr(importlib.import_module(module), attr)
    if not callable(fn):
        raise ValueError(f"creative_parser {spec!r} is not callable")
    return fn


def annotate(rows: list[dict[str, Any]], parser: Parser | None) -> None:
    """Set creative_id / creative_concept on each row from its ad_name (in place)."""
    cache: dict[str, Mapping[str, Any] | None] = {}
    for r in rows:
        name = r.get("ad_name") or ""
        if name not in cache:
            cache[name] = parser(name) if (parser and name) else None
        hit = cache[name]
        r["creative_id"] = str(hit["creative_id"]) if hit and hit.get("creative_id") else None
        r["creative_concept"] = str(hit["concept"]) if hit and hit.get("concept") else None
