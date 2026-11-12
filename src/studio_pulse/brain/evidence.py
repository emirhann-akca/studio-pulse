"""Evidence and insight objects, and their table meta_insights.

An Evidence is what a deterministic job computed: the SQL (or method) that
produced it, the rows it read, named values with units, n, effect, interval,
p and BH q. An Insight points at one or more Evidence objects, names a
narration template, and carries recommendations that cite evidence ids.
Nothing in an Insight is free text written by a model.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

SEVERITIES = ("critical", "warning", "info")
_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Fact:
    """One named number (or identifier) a template may print."""

    value: Any
    unit: str  # catalog unit, or pp | p | z | id | date | text | version


@dataclass
class Evidence:
    id: str
    kind: str
    app_id: str | None
    method: str
    sql: str = ""
    params: list[Any] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    facts: dict[str, Fact] = field(default_factory=dict)
    n: float | None = None
    effect: float | None = None
    effect_unit: str | None = None
    ci: tuple[float, float] | None = None
    p: float | None = None
    q: float | None = None
    metric: str | None = None
    definition: str | None = None
    aggregation: str | None = None
    caveats: list[str] = field(default_factory=list)
    as_of: dt.date | None = None

    def numbers(self) -> list[tuple[float, str]]:
        """Every number in this evidence as (value, unit): facts, cells, n, effect, ci, p, q."""
        out: list[tuple[float, str]] = []

        def add(v: Any, unit: str) -> None:
            if isinstance(v, bool) or v is None:
                return
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                out.append((float(v), unit))

        for f in self.facts.values():
            add(f.value, f.unit)
        for r in self.rows:
            for c in r:
                add(c, "cell")
        add(self.n, "count")
        add(self.effect, self.effect_unit or "ratio")
        if self.ci:
            add(self.ci[0], self.effect_unit or "ratio")
            add(self.ci[1], self.effect_unit or "ratio")
        add(self.p, "p")
        add(self.q, "p")
        return out

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["facts"] = {k: {"value": v.value, "unit": v.unit} for k, v in self.facts.items()}
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Evidence:
        d = dict(d)
        d["facts"] = {k: Fact(v["value"], v["unit"]) for k, v in (d.get("facts") or {}).items()}
        if d.get("ci") is not None:
            d["ci"] = tuple(d["ci"])
        if isinstance(d.get("as_of"), str):
            d["as_of"] = dt.date.fromisoformat(d["as_of"])
        return cls(**d)


@dataclass
class Recommendation:
    action: str  # key of a fixed action template (narrate.ACTIONS)
    evidence: list[str]  # cited evidence ids (at least one)


@dataclass
class Insight:
    id: str
    kind: str
    app_id: str | None
    severity: str  # critical | warning | info
    template: str  # narrate.TEMPLATES key
    evidence: list[Evidence]
    recommendations: list[Recommendation] = field(default_factory=list)
    causal: bool = True  # False: correlational wording is forced
    blocked: bool = False  # data-trust gate said: do not explain
    score: float = 0.0
    rank: int = 0
    p: float | None = None  # the insight's deciding test (BH-adjusted together)
    q: float | None = None

    @property
    def main(self) -> Evidence:
        return self.evidence[0]

    def facts(self) -> dict[str, Fact]:
        out: dict[str, Fact] = {}
        for ev in reversed(self.evidence):
            out.update(ev.facts)
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "app_id": self.app_id,
            "severity": self.severity,
            "template": self.template,
            "evidence": [e.to_json() for e in self.evidence],
            "recommendations": [asdict(r) for r in self.recommendations],
            "causal": self.causal,
            "blocked": self.blocked,
            "score": self.score,
            "rank": self.rank,
            "p": self.p,
            "q": self.q,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Insight:
        return cls(
            id=d["id"],
            kind=d["kind"],
            app_id=d.get("app_id"),
            severity=d["severity"],
            template=d["template"],
            evidence=[Evidence.from_json(e) for e in d["evidence"]],
            recommendations=[Recommendation(**r) for r in d.get("recommendations") or []],
            causal=d.get("causal", True),
            blocked=d.get("blocked", False),
            score=d.get("score", 0.0),
            rank=d.get("rank", 0),
            p=d.get("p"),
            q=d.get("q"),
        )


def rank(insights: list[Insight]) -> list[Insight]:
    """Blocked (data problems) first, then severity, then score; ranks from 1."""
    ordered = sorted(
        insights,
        key=lambda i: (not i.blocked, _SEV_RANK.get(i.severity, 9), -i.score, i.id),
    )
    for k, ins in enumerate(ordered, 1):
        ins.rank = k
    return ordered


# -- storage ---------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS meta_insights (
    run_id      VARCHAR NOT NULL,
    created_at  TIMESTAMP NOT NULL,
    as_of       DATE,
    rank        INTEGER,
    insight_id  VARCHAR NOT NULL,
    app_id      VARCHAR,
    kind        VARCHAR,
    severity    VARCHAR,
    blocked     BOOLEAN,
    metric      VARCHAR,
    method      VARCHAR,
    n           DOUBLE,
    effect      DOUBLE,
    ci_low      DOUBLE,
    ci_high     DOUBLE,
    p_value     DOUBLE,
    q_value     DOUBLE,
    sql         VARCHAR,
    payload     VARCHAR      -- the whole Insight as JSON (evidence included)
)
"""
KEEP_RUNS = 30


def _json_default(o: Any) -> Any:
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return str(o)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False, sort_keys=True)


def store(con: duckdb.DuckDBPyConnection, insights: list[Insight], as_of: dt.date | None) -> str:
    con.execute(DDL)
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
    rows = []
    for i in insights:
        ev = i.main
        rows.append(
            [
                run_id, now, as_of, i.rank, i.id, i.app_id, i.kind, i.severity, i.blocked,
                ev.metric, ev.method, ev.n, ev.effect,
                ev.ci[0] if ev.ci else None, ev.ci[1] if ev.ci else None,
                i.p, i.q, ev.sql, dumps(i.to_json()),
            ]
        )  # fmt: skip
    if rows:
        con.executemany(
            "INSERT INTO meta_insights VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    else:  # an empty run still records that the jobs ran
        con.execute(
            "INSERT INTO meta_insights (run_id, created_at, as_of, insight_id, payload) "
            "VALUES (?, ?, ?, '(none)', NULL)",
            [run_id, now, as_of],
        )
    con.execute(
        "DELETE FROM meta_insights WHERE run_id NOT IN (SELECT run_id FROM ("
        "SELECT run_id, MAX(created_at) AS t FROM meta_insights GROUP BY 1 "
        "ORDER BY t DESC, run_id DESC LIMIT ?))",
        [KEEP_RUNS],
    )
    return run_id


def latest(con: duckdb.DuckDBPyConnection | None) -> tuple[dict[str, Any] | None, list[Insight]]:
    """(run info, insights ranked) of the newest stored run; (None, []) if none."""
    if con is None:
        return None, []
    try:
        head = con.execute(
            "SELECT run_id, created_at, as_of FROM meta_insights "
            "ORDER BY created_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
    except duckdb.Error:
        return None, []
    if not head:
        return None, []
    rows = con.execute(
        "SELECT payload FROM meta_insights WHERE run_id = ? AND payload IS NOT NULL ORDER BY rank",
        [head[0]],
    ).fetchall()
    insights = [Insight.from_json(json.loads(r[0])) for r in rows]
    return {"run_id": head[0], "created_at": head[1], "as_of": head[2]}, insights
