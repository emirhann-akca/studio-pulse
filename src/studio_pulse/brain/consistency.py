"""Question-evidence consistency: does the evidence answer the question that was asked?

verify.py proves every number in an answer equals a value of its evidence.
That is not enough: "What is the CPI on Meta for quartz_merge?" was answered
with the blend of TikTok and Unity Ads, every number verified, because the
network filter was dropped on the way. This module reads the evidence SQL
(sqlglot, the bound parameters substituted) and its result rows, and checks
each filter the question names (intent.py) against them:

  * app, ad network, platform, store, country, app version, device tier,
    level: an equality / IN predicate on the dimension's column with exactly
    the asked values, or result rows whose column holds only those values, or
    a table whose every row has the value (Android vitals are Android);
  * the period: the evidence's date range (predicates on date / cohort_date,
    or first_day / last_day / date in the rows, or the window of a snapshot
    table) equals the asked one, cut at the newest data day.

A filter that is not covered is a Gap; the router turns gaps into a refusal
that names them instead of an answer to another question.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from .intent import COUNTRIES, NETWORK_ALIASES, NETWORK_NAMES, Intent
from .semantic import DIM_COLUMNS, MODELS

logging.getLogger("sqlglot").setLevel(logging.ERROR)

TIME_COLUMNS = ("date", "cohort_date")
_STORE_ALIASES = {
    "google_play": ("google_play", "play", "google play", "play_store", "android"),
    "app_store": ("app_store", "app store", "apple", "ios", "itunes"),
}


@dataclass
class Gap:
    dim: str
    asked: list[str]
    reason: str  # not_applied | other_value | period
    found: list[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.dim} = {', '.join(self.asked)}"


@dataclass
class Coverage:
    tables: set[str] = field(default_factory=set)
    preds: dict[str, set[str]] = field(default_factory=dict)  # column -> values (lower)
    lows: dict[str, list[dt.date]] = field(default_factory=dict)
    highs: dict[str, list[dt.date]] = field(default_factory=dict)
    rows: dict[str, set[str]] = field(default_factory=dict)  # result column -> values (lower)
    row_dates: dict[str, list[dt.date]] = field(default_factory=dict)
    unparsed: bool = False


def _literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dt.datetime):
        return f"TIMESTAMP '{v.isoformat(sep=' ')}'"
    if isinstance(v, dt.date):
        return f"DATE '{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def bind(sql: str, params: list[Any]) -> str:
    """The SQL with each `?` (outside quotes) replaced by its parameter, for reading only."""
    out = []
    i = 0
    quote = None
    for ch in sql:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?" and i < len(params):
            out.append(_literal(params[i]))
            i += 1
        else:
            out.append(ch)
    return "".join(out)


def _col_name(node: exp.Expression) -> str | None:
    while isinstance(node, (exp.Cast, exp.TryCast, exp.Lower, exp.Upper, exp.Paren)):
        node = node.this
    if isinstance(node, exp.Column):
        return node.name.lower()
    return None


def _value(node: exp.Expression) -> Any:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Cast, exp.TryCast)) and isinstance(node.this, exp.Literal):
        to = node.to.sql().upper()
        if to.startswith("DATE"):
            try:
                return dt.date.fromisoformat(node.this.this[:10])
            except ValueError:
                return None
        return node.this.this
    if isinstance(node, exp.Literal):
        return node.this
    if isinstance(node, exp.Boolean):
        return str(node.this).lower()
    return None


def _date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def coverage_of(sql: str, params: list[Any], columns: list[str], rows: list[list[Any]]) -> Coverage:
    cov = Coverage()
    for ci, c in enumerate(columns or []):
        name = c.lower()
        vals = [r[ci] for r in rows if ci < len(r) and r[ci] is not None]
        if not vals:
            continue
        if name in TIME_COLUMNS or name in ("first_day", "last_day", "first_cohort", "last_cohort"):
            ds = [d for d in (_date(v) for v in vals) if d]
            if ds:
                cov.row_dates[name] = ds
        cov.rows[name] = {str(v).lower() for v in vals}
    if not sql:
        return cov
    try:
        tree = sqlglot.parse_one(bind(sql, params), read="duckdb")
    except Exception:
        cov.unparsed = True
        return cov
    cov.tables = {t.name.lower() for t in tree.find_all(exp.Table)}
    for node in tree.find_all(exp.EQ, exp.In, exp.GTE, exp.GT, exp.LTE, exp.LT, exp.Between):
        if isinstance(node, exp.In):
            col = _col_name(node.this)
            if col:
                vals = [_value(e) for e in node.expressions]
                cov.preds.setdefault(col, set()).update(
                    str(v).lower() for v in vals if v is not None
                )
            continue
        if isinstance(node, exp.Between):
            col = _col_name(node.this)
            if col in TIME_COLUMNS:
                lo, hi = _date(_value(node.args["low"])), _date(_value(node.args["high"]))
                if lo:
                    cov.lows.setdefault(col, []).append(lo)
                if hi:
                    cov.highs.setdefault(col, []).append(hi)
            continue
        left, right = node.this, node.expression
        col, val = _col_name(left), _value(right)
        flip = False
        if col is None:
            col, val, flip = _col_name(right), _value(left), True
        if col is None or val is None:
            continue
        if isinstance(node, exp.EQ):
            cov.preds.setdefault(col, set()).add(str(val).lower())
            d = _date(val)
            if col in TIME_COLUMNS and d:
                cov.lows.setdefault(col, []).append(d)
                cov.highs.setdefault(col, []).append(d)
            continue
        d = _date(val)
        if col not in TIME_COLUMNS or d is None:
            continue
        lower = isinstance(node, (exp.GTE, exp.GT)) != flip
        if lower:
            cov.lows.setdefault(col, []).append(
                d + dt.timedelta(days=1) if isinstance(node, exp.GT) and not flip else d
            )
        else:
            cov.highs.setdefault(col, []).append(
                d - dt.timedelta(days=1) if isinstance(node, exp.LT) and not flip else d
            )
    return cov


def aliases(dim: str, value: str) -> set[str]:
    v = str(value)
    if dim == "network":
        return {a.lower() for a in NETWORK_ALIASES.get(v, (v,))} | {v.lower()}
    if dim == "country":
        en, others, trs = COUNTRIES.get(v, (v, (), ()))
        return {v.lower(), en.lower(), *others, *trs}
    if dim == "store":
        return set(_STORE_ALIASES.get(v, (v,)))
    return {v.lower()}


def display(dim: str, value: str) -> str:
    if dim == "network":
        return NETWORK_NAMES.get(value, value)
    if dim == "country":
        return COUNTRIES.get(value, (value, (), ()))[0]
    if dim == "platform":
        return {"IOS": "iOS", "ANDROID": "Android"}.get(value, value)
    if dim == "store":
        return {"google_play": "Google Play", "app_store": "App Store"}.get(value, value)
    return str(value)


def _covered(dim: str, asked: list[str], cov: Coverage) -> tuple[bool, list[str]]:
    col = DIM_COLUMNS.get(dim, dim)
    want = [aliases(dim, a) for a in asked]
    every = set().union(*want) if want else set()
    for model in cov.tables:
        implied = dict(MODELS[model].implied) if model in MODELS else {}
        if dim in implied and implied[dim].lower() in every:
            return True, []
    pv = cov.preds.get(col, set())
    if pv:
        if pv <= every and all(w & pv for w in want):
            return True, []
        if not (pv & every):
            return False, sorted(pv)
    rv = cov.rows.get(col, set())
    if rv and rv <= every and all(w & rv for w in want):
        return True, []
    return False, sorted(pv) if pv else []


def _span(cov: Coverage) -> tuple[dt.date | None, dt.date | None, str | None]:
    for col in TIME_COLUMNS:
        lo = max(cov.lows[col]) if cov.lows.get(col) else None
        hi = min(cov.highs[col]) if cov.highs.get(col) else None
        if lo or hi:
            return lo, hi, col
    lo = min(
        cov.row_dates.get("first_day", [])
        or cov.row_dates.get("date", [])
        or cov.row_dates.get("cohort_date", []),
        default=None,
    )
    hi = max(
        cov.row_dates.get("last_day", [])
        or cov.row_dates.get("date", [])
        or cov.row_dates.get("cohort_date", []),
        default=None,
    )
    col = (
        "date"
        if (cov.row_dates.get("date") or cov.row_dates.get("first_day"))
        else ("cohort_date" if cov.row_dates.get("cohort_date") else None)
    )
    return lo, hi, col


def check_period(
    intent: Intent, cov: Coverage, newest: dt.date | None, first: dt.date | None = None
) -> Gap | None:
    p = intent.period
    if p is None or p.latest:
        return None
    lo, hi, col = _span(cov)
    window = None
    for t in cov.tables:
        spec = MODELS.get(t)
        if spec and spec.time is None and spec.window_days:
            window = spec.window_days
    if lo is None and hi is None:
        timed = {MODELS[t].time for t in cov.tables if t in MODELS and MODELS[t].time}
        if timed:  # a dated table, but no literal date range to read: cannot be checked
            if timed == {"cohort_date"} and p.days and not (p.start and p.end):
                return None  # "last week" of a cohort metric: the newest complete cohorts
            return Gap("period", [p.label or "period"], "period")
        if p.days and window and p.days == window:
            return None
        if p.start and p.end and p.start == p.end and newest and p.start == newest:
            return None  # "yesterday" on a snapshot of the newest day
        return Gap("period", [p.label or "period"], "period")
    if col == "cohort_date" and p.days and not (p.start and p.end):
        return None  # "last week" of a cohort metric: the newest complete cohorts are fair
    if p.start and p.end:
        want_lo, want_hi = p.start, min(p.end, newest) if newest else p.end
        if first and want_lo < first:
            want_lo = first
        ok_lo = (
            lo is None or lo == want_lo or (first is not None and lo == first and want_lo <= first)
        )
        ok_hi = hi is None or hi == want_hi
        if col == "cohort_date":  # cohorts complete inside the period are a fair reading
            ok_lo = lo is None or lo >= p.start
            ok_hi = hi is None or hi <= p.end
        return None if ok_lo and ok_hi else Gap("period", [p.label or "period"], "period")
    if p.days and lo and hi:
        if (hi - lo).days + 1 == p.days or (
            first is not None and lo == first and (hi - lo).days + 1 < p.days
        ):
            return None
        return Gap("period", [p.label or f"last {p.days} days"], "period")
    return None


def check(
    intent: Intent,
    *,
    sql: str,
    params: list[Any],
    columns: list[str],
    rows: list[list[Any]],
    n_apps: int,
    newest: dt.date | None = None,
    first: dt.date | None = None,
    skip: set[str] | None = None,
) -> list[Gap]:
    """Every filter of the question that the evidence does not cover."""
    cov = coverage_of(sql, params, columns, rows)
    if cov.unparsed:
        return []
    gaps: list[Gap] = []
    skip = skip or set()
    if intent.apps and n_apps > 1 and "app" not in skip:
        ok, found = _covered("app", intent.apps, cov)
        if not ok:
            gaps.append(
                Gap("app", list(intent.apps), "other_value" if found else "not_applied", found)
            )
    for dim, vals in intent.filters().items():
        if dim in skip:
            continue
        ok, found = _covered(dim, vals, cov)
        if not ok:
            gaps.append(
                Gap(
                    dim,
                    [display(dim, v) for v in vals],
                    "other_value" if found else "not_applied",
                    found,
                )
            )
    if "period" not in skip:
        g = check_period(intent, cov, newest, first)
        if g:
            gaps.append(g)
    return gaps
