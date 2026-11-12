"""Number verifier: every number in user-facing text must match evidence.

The text is scanned for numbers in the language's notation (Turkish: `%60,9`,
`39.048`, `9,1 puan`; English: `60.9%`, `39,048`, `9.1 pp`). Each number must
equal some value of the cited evidence within the rounding its own decimals
allow (60,9 covers 0.6085..0.6095). Percentages are compared as shares;
plain numbers are compared with the values and, for shares, with
value x 100 (percentage points). Signs are ignored: direction is carried by
words ("dropped", "düştü"), which the fixed templates choose from the sign.

Not numbers: ISO dates, dotted version strings (1.8.0) and digits glued to
letters (D1, p95, ft_p50). "p < 0,001"-style bounds are accepted when some
p or q value of the evidence is below the bound.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .evidence import Evidence

_MASK = re.compile(
    r"“[^”]*”"  # untrusted data, quoted by fmt.data
    r"|\d{4}-\d{2}-\d{2}"  # ISO date
    r"|(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])"  # version 1.8.0
    r"|[A-Za-z_]+\d[\w.]*"  # identifiers with digits: D1, p95, ft_p50_ms, ev-3
)
_NUM = re.compile(
    r"(?P<bound>[<>≤≥]\s*)?"
    r"(?P<sign>[-−+]\s?)?"
    r"(?P<pre>%\s?)?"
    r"(?<![\w])(?P<num>\d+(?:[.,]\d+)*)"
    r"(?P<post>\s?%|\s?(?:puan|pp|percentage points?))?"
)


@dataclass(frozen=True)
class Token:
    text: str
    value: float  # in display units (60.9 for "%60,9")
    decimals: int
    percent: bool
    points: bool
    bound: str | None


def _parse(num: str, lang: str) -> tuple[float, int]:
    thousands, dec = (".", ",") if lang == "tr" else (",", ".")
    s = num
    # a single separator followed by exactly three digits is a thousands separator
    # only in the language's own notation; otherwise it is the other language's
    # decimal mark, which makes the number not parse as intended -> no match later.
    parts = s.split(dec)
    if len(parts) > 2:
        return (float("nan"), 0)
    int_part = parts[0].replace(thousands, "")
    frac = parts[1] if len(parts) == 2 else ""
    if not int_part.isdigit() or (frac and not frac.isdigit()):
        return (float("nan"), 0)
    return (float(int_part + ("." + frac if frac else "")), len(frac))


def tokens(text: str, lang: str) -> list[Token]:
    masked = _MASK.sub(lambda m: " " * len(m.group(0)), text)
    out: list[Token] = []
    for m in _NUM.finditer(masked):
        value, decimals = _parse(m.group("num"), lang)
        post = (m.group("post") or "").strip()
        percent = bool(m.group("pre")) or post == "%"
        points = bool(post) and post != "%"
        bound = (m.group("bound") or "").strip() or None
        out.append(Token(m.group(0).strip(), value, decimals, percent, points, bound))
    return out


def _matches(tok: Token, values: Iterable[tuple[float, str]]) -> bool:
    if tok.value != tok.value:  # NaN: unparsable in this language
        return False
    tol = 0.5 * 10 ** (-tok.decimals) + 1e-9
    for v, unit in values:
        a = abs(v)
        if tok.bound:
            if unit in ("p", "q") and tok.bound in ("<", "≤") and a < tok.value + 1e-12:
                return True
            continue
        if tok.percent:
            if abs(a * 100 - tok.value) <= tol:
                return True
            if unit == "id" and abs(a - tok.value) <= tol:  # "95% CI", "%95 GA"
                return True
            continue
        if tok.points:
            if unit in ("pct", "pp", "cell") and abs(a * 100 - tok.value) <= tol:
                return True
            continue
        if abs(a - tok.value) <= tol:
            return True
        if unit in ("pct", "pp") and abs(a * 100 - tok.value) <= tol:
            return True
    return False


_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")


def _identifier_numbers(evidence: list[Evidence]) -> set[str]:
    """Digit runs inside identifiers the evidence carries (versions, level lists, names)."""
    out: set[str] = set()
    for ev in evidence:
        for f in ev.facts.values():
            if f.unit in ("text", "version") and isinstance(f.value, str):
                out.update(_DIGITS.findall(f.value))
        for r in ev.rows:
            for c in r:
                if isinstance(c, str):
                    out.update(_DIGITS.findall(c))
    return out


def unsupported(text: str, evidence: Iterable[Evidence], lang: str) -> list[str]:
    """Numbers in `text` that no cited evidence supports (empty list: verified)."""
    evs = list(evidence)
    values: list[tuple[float, str]] = []
    for ev in evs:
        values.extend(ev.numbers())
    idents = _identifier_numbers(evs)
    bad = []
    for t in tokens(text, lang):
        raw = _DIGITS.search(t.text)
        if not t.percent and not t.points and not t.bound and raw and raw.group(0) in idents:
            continue
        if not _matches(t, values):
            bad.append(t.text)
    return bad


def verified(text: str, evidence: Iterable[Evidence], lang: str) -> bool:
    return not unsupported(text, list(evidence), lang)
