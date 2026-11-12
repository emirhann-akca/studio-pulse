"""Shared pieces of the insight jobs."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import duckdb

from ..catalog import Catalog
from ..evidence import Evidence, Fact, Insight

Q_LEVEL = 0.10  # Benjamini-Hochberg false discovery rate for everything scanned in a run
MIN_N = 50  # smallest sample a proportion finding may rest on


@dataclass
class Candidate:
    """A tested hypothesis: insight is None when the effect was too small to report."""

    p: float | None
    insight: Insight | None


@dataclass
class Context:
    con: duckdb.DuckDBPyConnection
    as_of: dt.date
    catalog: Catalog
    rules: list[Any] = field(default_factory=list)
    trust: dict[str, Any] = field(default_factory=dict)  # app_id -> trust.Trust
    apps: list[str] = field(default_factory=list)

    def blocked(self, app_id: str | None) -> bool:
        t = self.trust.get(app_id or "")
        return bool(t and t.status == "block")

    def small(self, app_id: str | None) -> bool:
        t = self.trust.get(app_id or "")
        return bool(t and t.small)


def plain(v: Any) -> Any:
    if v is None or isinstance(v, (bool, str, dt.date)):
        return v
    if isinstance(v, int):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f):
        return None
    return int(f) if f.is_integer() and abs(f) < 2**53 and not isinstance(v, float) else f


def query(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> tuple[list[str], list[list[Any]]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description or []]
    return cols, [[plain(v) for v in r] for r in cur.fetchall()]


def has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return bool(row and row[0])


def app_ids(con: duckdb.DuckDBPyConnection) -> list[str]:
    for sql in (
        "SELECT app_id FROM meta_apps ORDER BY 1",
        "SELECT DISTINCT app_id FROM fct_app_daily ORDER BY 1",
    ):
        try:
            rows = con.execute(sql).fetchall()
        except duckdb.Error:
            continue
        if rows:
            return [r[0] for r in rows]
    return []


def ev_id(kind: str, app_id: str | None, *parts: Any) -> str:
    bits = [kind, app_id or "all", *[str(p) for p in parts if p is not None]]
    return ":".join(bits)


def fact(value: Any, unit: str) -> Fact:
    return Fact(plain(value), unit)


def two_sided_p_from_z(z: float | None) -> float | None:
    from ...stats.dist import norm_sf_two_sided

    return None if z is None or math.isnan(z) else norm_sf_two_sided(z)


def make_evidence(kind: str, app_id: str | None, key: Any, method: str, **kw: Any) -> Evidence:
    return Evidence(id=ev_id(kind, app_id, key), kind=kind, app_id=app_id, method=method, **kw)
