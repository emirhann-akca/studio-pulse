"""Numbers are formatted by code, never by a model.

Turkish: the percent sign comes first and touches the number, decimal comma,
thousands dot (`%60,9`, `39.048`; MEB yazım kılavuzu). English: `60.9%`,
`39,048`. Percentage-point differences are `9,1 puan` / `9.1 pp`.

The number of decimals depends on the unit and the size of the value; the
verifier (verify.py) derives its tolerance from the decimals it sees, so any
string produced here verifies against the value it was made from.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

LANGS = ("tr", "en")


def _group(int_part: str, sep: str) -> str:
    out = []
    while len(int_part) > 3:
        out.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    out.insert(0, int_part)
    return sep.join(out)


def plain(x: float, decimals: int, lang: str) -> str:
    """x with `decimals` decimals and the language's separators (no unit)."""
    if decimals < 0:
        decimals = 0
    s = f"{abs(x):.{decimals}f}"
    if "." in s:
        ip, fp = s.split(".")
    else:
        ip, fp = s, ""
    thousands, dec = (".", ",") if lang == "tr" else (",", ".")
    body = _group(ip, thousands) + (dec + fp if fp else "")
    neg = x < 0 and float(s) != 0
    return ("-" if neg else "") + body


def pct_decimals(share: float) -> int:
    """Decimals that keep small rates and near-100% rates readable (1.15%, 98.65%)."""
    a = abs(share) * 100
    if a == 0:
        return 1
    if a < 0.1:
        return 3
    if a < 10 or a > 95:
        return 2
    return 1


def pct(share: float, lang: str, decimals: int | None = None) -> str:
    d = pct_decimals(share) if decimals is None else decimals
    body = plain(abs(share) * 100, d, lang)
    sign = "-" if share < 0 and float(f"{abs(share) * 100:.{d}f}") != 0 else ""
    return f"{sign}%{body}" if lang == "tr" else f"{sign}{body}%"


def pp(diff: float, lang: str, decimals: int = 1) -> str:
    """Percentage points: a difference of two shares."""
    body = plain(diff * 100, decimals, lang)
    return f"{body} puan" if lang == "tr" else f"{body} pp"


def usd(x: float, lang: str) -> str:
    a = abs(x)
    d = 3 if a < 1 else 2 if a < 100 else 0
    return ("-" if x < 0 else "") + "$" + plain(a, d, lang)


def count(x: float, lang: str) -> str:
    return plain(round(x), 0, lang)


_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_CTRL = re.compile(r"[\x00-\x1f\x7f“”]")
DATA_MAX = 60


def data(value: Any) -> str:
    """Untrusted text from players or ad networks (campaign names, review labels).

    Quoted so the reader and the verifier see it as data: links removed,
    control characters and quote marks dropped, cut at 60 characters. The
    verifier ignores numbers inside the quotes; a model never sees it outside
    the JSON facts marked untrusted.
    """
    s = _URL.sub("[link]", str(value))
    s = " ".join(_CTRL.sub(" ", s).split())
    if len(s) > DATA_MAX:
        s = s[: DATA_MAX - 1].rstrip() + "…"
    return f"“{s}”"


def number(value: Any, unit: str, lang: str) -> str:
    """Format one value by its catalog unit."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if unit == "data":
        return data(value)
    if unit == "date" or isinstance(value, dt.date):
        return value.isoformat() if isinstance(value, dt.date) else str(value)
    if unit in ("id", "text", "version"):
        return str(value)
    x = float(value)
    if unit == "pct":
        return pct(x, lang)
    if unit == "pp":
        return pp(x, lang)
    if unit == "usd":
        return usd(x, lang)
    if unit in ("count", "days"):
        return count(x, lang)
    if unit == "ms":
        return plain(x, 1, lang) + " ms"
    if unit == "mb":
        return plain(x, 0, lang) + " MB"
    if unit in ("p", "q"):
        if x < 0.001:
            return f"{unit} < " + plain(0.001, 3, lang)
        return f"{unit} = " + plain(x, 3, lang)
    if unit == "z":
        return plain(x, 1, lang)
    # ratio and anything else: keep three significant digits for small values
    a = abs(x)
    d = 2 if a >= 1 or a == 0 else 3 if a >= 0.1 else 4
    return plain(x, d, lang)


def interval(lo: Any, hi: Any, unit: str, lang: str) -> str:
    return f"{number(lo, unit, lang)}–{number(hi, unit, lang)}"
