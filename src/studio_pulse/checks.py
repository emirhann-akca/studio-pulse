"""Data-quality checks (checks.yaml) run against the raw tables and the models.

Every check compiles to one SELECT that returns the failing rows; a check
passes when that query returns none. Types:

  not_null          columns: [..]                rows with a NULL in any column
  unique            columns: [..]                key values that occur more than once
  accepted_values   column, values: [..]         rows outside the list (NULLs pass)
  range             column, min / max            rows outside [min, max] (NULLs pass, NaN fails)
  relationship      column, to: table.column     values with no match in the parent table
  expression        condition: <SQL boolean>     rows where the condition is false
  sql               sql: <SELECT>                any rows returned
  freshness         max_lag_days, overrides,     configured sources that have not loaded in
                    sparse                       max_lag_days, never loaded, or whose newest
                                                 data date is more than max_lag_days before
                                                 as_of. Sources in `sparse` (Crashlytics: no
                                                 crash, no row) are judged by load time only
  row_count_drop    date_column, measure,        the newest day's measure (default COUNT(*)) per
                    group_by, window_days,       group is more than max_drop_pct below the median
                    max_drop_pct, min_baseline   of the previous window_days days

Common keys: name, table, where (extra SQL filter), severity (error | warn),
description, optional (a missing table is skipped instead of reported).

Severity decides the outcome, not the status: a failing `warn` check is shown
and notified but does not fail `pulse check` or `pulse run`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml

from .config import Config

CHECK_TYPES = (
    "not_null",
    "unique",
    "accepted_values",
    "range",
    "relationship",
    "expression",
    "sql",
    "freshness",
    "row_count_drop",
)
SEVERITIES = ("error", "warn")
KEEP_RUNS = 30
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CheckError(ValueError):
    pass


def _ident(value: str, what: str, check: str) -> str:
    if not isinstance(value, str) or not _IDENT.match(value):
        raise CheckError(f"check {check!r}: {what} {value!r} is not a plain identifier")
    return value


@dataclass
class Check:
    name: str
    type: str
    table: str | None = None
    columns: list[str] = field(default_factory=list)
    column: str | None = None
    values: list[Any] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    to: str | None = None
    condition: str | None = None
    sql: str | None = None
    where: str | None = None
    date_column: str = "date"
    measure: str = "COUNT(*)"
    group_by: list[str] = field(default_factory=lambda: ["app_id"])
    window_days: int = 14
    min_points: int = 5
    max_drop_pct: float | None = None
    min_baseline: float = 0
    max_lag_days: int = 3
    overrides: dict[str, int] = field(default_factory=dict)
    sparse: list[str] = field(default_factory=list)
    severity: str = "error"
    description: str = ""
    optional: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any], defaults: dict[str, Any]) -> Check:
        merged = {**defaults, **d}
        unknown = set(merged) - set(cls.__dataclass_fields__)
        if unknown:
            raise CheckError(f"check {d.get('name')!r}: unknown keys {sorted(unknown)}")
        if "name" not in merged or "type" not in merged:
            raise CheckError(f"every check needs a name and a type: {d}")
        c = cls(**merged)
        n = c.name
        _ident(n, "name", n)
        if c.type not in CHECK_TYPES:
            raise CheckError(f"check {n!r}: type must be one of {', '.join(CHECK_TYPES)}")
        if c.severity not in SEVERITIES:
            raise CheckError(f"check {n!r}: severity must be error or warn")
        needs_table = c.type not in ("sql", "freshness")
        if needs_table:
            _ident(c.table or "", "table", n)
        if c.type in ("not_null", "unique"):
            if not c.columns:
                raise CheckError(f"check {n!r}: needs columns")
            for col in c.columns:
                _ident(col, "column", n)
        if c.type in ("accepted_values", "range", "relationship"):
            _ident(c.column or "", "column", n)
        if c.type == "accepted_values" and not c.values:
            raise CheckError(f"check {n!r}: needs values")
        if c.type == "range" and c.min is None and c.max is None:
            raise CheckError(f"check {n!r}: needs min and/or max")
        if c.type == "relationship":
            parts = (c.to or "").split(".")
            if len(parts) != 2:
                raise CheckError(f"check {n!r}: to must be parent_table.column")
            for p in parts:
                _ident(p, "to", n)
        if c.type == "expression" and not c.condition:
            raise CheckError(f"check {n!r}: needs condition")
        if c.type == "sql":
            body = (c.sql or "").strip().rstrip(";").strip()
            if not re.match(r"(?is)^(select|with)\b", body) or ";" in body:
                raise CheckError(f"check {n!r}: sql must be a single SELECT")
            c.sql = body
        if c.type == "row_count_drop":
            _ident(c.date_column, "date_column", n)
            for col in c.group_by:
                _ident(col, "group_by", n)
            if c.max_drop_pct is None:
                raise CheckError(f"check {n!r}: needs max_drop_pct")
        return c

    def failing_sql(self, cfg: Config | None, as_of: dt.date) -> tuple[str, list[Any]]:
        where = f" AND ({self.where})" if self.where else ""
        t = self.table
        if self.type == "not_null":
            cond = " OR ".join(f"{c} IS NULL" for c in self.columns)
            return f"SELECT * FROM {t} WHERE ({cond}){where}", []
        if self.type == "unique":
            cols = ", ".join(self.columns)
            return (
                f"SELECT {cols}, COUNT(*) AS copies FROM {t} WHERE TRUE{where} "
                f"GROUP BY {cols} HAVING COUNT(*) > 1",
                [],
            )
        if self.type == "accepted_values":
            ph = ", ".join("?" * len(self.values))
            return (
                f"SELECT {self.column}, COUNT(*) AS n_rows FROM {t} "
                f"WHERE {self.column} IS NOT NULL AND {self.column} NOT IN ({ph}){where} "
                f"GROUP BY {self.column}",
                list(self.values),
            )
        if self.type == "range":
            conds, params = [], []
            if self.min is not None:
                conds.append(f"{self.column} < ?")
                params.append(self.min)
            if self.max is not None:
                conds.append(f"{self.column} > ?")
                params.append(self.max)
            if self.min is not None or self.max is not None:
                conds.append(f"isnan(CAST({self.column} AS DOUBLE))")
            return (
                f"SELECT * FROM {t} WHERE {self.column} IS NOT NULL AND ({' OR '.join(conds)}){where}",
                params,
            )
        if self.type == "relationship":
            pt, pc = (self.to or "").split(".")
            return (
                f"SELECT {self.column}, COUNT(*) AS n_rows FROM {t} WHERE {self.column} IS NOT NULL "
                f"AND {self.column} NOT IN (SELECT {pc} FROM {pt} WHERE {pc} IS NOT NULL){where} "
                f"GROUP BY {self.column}",
                [],
            )
        if self.type == "expression":
            return f"SELECT * FROM {t} WHERE ({self.condition}) IS FALSE{where}", []
        if self.type == "sql":
            return f"SELECT * FROM ({self.sql})", []
        if self.type == "row_count_drop":
            return self._drop_sql(where)
        if self.type == "freshness":
            return self._freshness_sql(cfg, as_of)
        raise CheckError(self.type)

    def _drop_sql(self, where: str) -> tuple[str, list[Any]]:
        g = ", ".join(self.group_by)
        join = " AND ".join(f"b.{c} IS NOT DISTINCT FROM c.{c}" for c in self.group_by)
        sql = f"""
WITH daily AS (
    SELECT {g}, {self.date_column} AS d, {self.measure} AS v
    FROM {self.table} WHERE {self.date_column} IS NOT NULL{where}
    GROUP BY ALL
),
eval_day AS (SELECT MAX(d) AS d FROM daily),
base AS (
    SELECT {g}, median(v) AS baseline, COUNT(*) AS points
    FROM daily, eval_day e
    WHERE daily.d < e.d AND daily.d >= e.d - {int(self.window_days)}
    GROUP BY ALL
),
cur AS (SELECT daily.* FROM daily, eval_day e WHERE daily.d = e.d)
SELECT b.*, (SELECT d FROM eval_day) AS date, COALESCE(c.v, 0) AS value,
       1 - COALESCE(c.v, 0) / b.baseline AS drop_share
FROM base b LEFT JOIN cur c ON {join}
WHERE b.points >= ? AND b.baseline > 0 AND b.baseline >= ?
  AND COALESCE(c.v, 0) < b.baseline * (1 - ? / 100.0)
"""
        return sql, [int(self.min_points), float(self.min_baseline), float(self.max_drop_pct)]

    def _freshness_sql(self, cfg: Config | None, as_of: dt.date) -> tuple[str, list[Any]]:
        expected: list[tuple[str, str, str]] = []
        if cfg is not None:
            for app in cfg.apps:
                for kind, src in app.sources.items():
                    src = src or {}
                    if src.get("enabled", True) is False:
                        continue
                    expected.append((app.id, str(src.get("name", kind)), kind))
        params: list[Any] = [x for e in expected for x in e]
        if expected:
            expected_cte = (
                "expected(app_id, source, kind) AS (VALUES "
                + ", ".join("(?, ?, ?)" for _ in expected)
                + ")"
            )
        else:  # no config: judge every source that has ever loaded
            expected_cte = (
                "expected AS (SELECT DISTINCT app_id, source, source AS kind FROM meta_loads)"
            )
        lag_case = "CASE kind " + " ".join("WHEN ? THEN ?" for _ in self.overrides) + " ELSE ? END"
        if not self.overrides:
            lag_case = "?"
        for k, v in self.overrides.items():
            params += [k, int(v)]
        params += [int(self.max_lag_days), as_of, list(self.sparse)]
        sql = f"""
WITH {expected_cte},
latest AS (
    SELECT * FROM meta_loads
    QUALIFY row_number() OVER (PARTITION BY app_id, source, table_name ORDER BY loaded_at DESC) = 1
),
loaded AS (
    SELECT app_id, source, MAX(max_date) AS data_through, MAX(loaded_at) AS last_loaded_at
    FROM latest GROUP BY 1, 2
),
j AS (
    SELECT e.app_id, e.source, e.kind, l.data_through, l.last_loaded_at,
           {lag_case} AS max_lag_days, CAST(? AS DATE) AS as_of,
           list_contains(CAST(? AS VARCHAR[]), e.kind) AS sparse
    FROM expected e LEFT JOIN loaded l ON l.app_id = e.app_id AND l.source = e.source
    WHERE e.app_id IS NOT NULL
),
judged AS (
    SELECT *,
           date_diff('day', data_through, as_of) AS lag_days,
           date_diff('day', CAST(last_loaded_at AS DATE), as_of) AS load_lag_days,
           CASE WHEN last_loaded_at IS NULL THEN 'never loaded'
                WHEN date_diff('day', CAST(last_loaded_at AS DATE), as_of) > max_lag_days
                    THEN 'not loaded recently'
                WHEN sparse THEN NULL
                WHEN data_through IS NULL THEN 'loaded, but no dated rows'
                WHEN date_diff('day', data_through, as_of) > max_lag_days THEN 'stale'
           END AS problem
    FROM j
)
SELECT app_id, source, kind, data_through, last_loaded_at, lag_days, load_lag_days,
       max_lag_days, problem
FROM judged WHERE problem IS NOT NULL
ORDER BY 1, 2
"""
        return sql, params


@dataclass
class CheckResult:
    name: str
    type: str
    table: str | None
    severity: str
    status: str  # pass | fail | error
    failing_rows: int
    message: str
    sample: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.severity == "error" and self.status != "pass"


def load_checks(path: str | Path) -> list[Check]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    checks = [Check.from_dict(c, defaults) for c in data.get("checks") or []]
    names = [c.name for c in checks]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise CheckError(f"check names must be unique: {dupes}")
    return checks


def _message(c: Check, n: int, sample: list[dict[str, Any]]) -> str:
    if n == 0:
        return "ok"
    if c.type == "not_null":
        return f"{n} row(s) with NULL in {', '.join(c.columns)}"
    if c.type == "unique":
        return f"{n} duplicated key(s) on ({', '.join(c.columns)})"
    if c.type == "accepted_values":
        bad = ", ".join(str(r.get(c.column)) for r in sample)
        return f"{n} unexpected value(s) in {c.column}: {bad}"
    if c.type == "range":
        lo = "-inf" if c.min is None else c.min
        hi = "inf" if c.max is None else c.max
        return f"{n} row(s) with {c.column} outside [{lo}, {hi}]"
    if c.type == "relationship":
        return f"{n} {c.column} value(s) missing from {c.to}"
    if c.type == "freshness":
        parts = []
        for r in sample:
            if r.get("problem") == "stale":
                parts.append(
                    f"{r['app_id']}/{r['source']} data through {r['data_through']} "
                    f"({r['lag_days']} d, limit {r['max_lag_days']})"
                )
            elif r.get("problem") == "not loaded recently":
                parts.append(
                    f"{r['app_id']}/{r['source']} last loaded {str(r['last_loaded_at'])[:10]} "
                    f"({r['load_lag_days']} d, limit {r['max_lag_days']})"
                )
            else:
                parts.append(f"{r['app_id']}/{r['source']} {r['problem']}")
        return f"{n} source(s) stale: " + "; ".join(parts)
    if c.type == "row_count_drop":
        parts = []
        for r in sample:
            grp = "/".join(str(r.get(g)) for g in c.group_by)
            parts.append(
                f"{grp} {r['date']}: {r['value']:,.0f} vs median {r['baseline']:,.0f} "
                f"(-{r['drop_share'] * 100:.0f}%)"
            )
        return f"{n} group(s) dropped more than {c.max_drop_pct:.0f}%: " + "; ".join(parts)
    return f"{n} failing row(s)"


def _json_safe(v: Any) -> Any:
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def run_check(
    con: duckdb.DuckDBPyConnection, c: Check, cfg: Config | None, as_of: dt.date
) -> CheckResult:
    try:
        sql, params = c.failing_sql(cfg, as_of)
        n = con.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        sample: list[dict[str, Any]] = []
        if n:
            cur = con.execute(f"SELECT * FROM ({sql}) LIMIT 5", params)
            cols = [d[0] for d in cur.description]
            sample = [
                {k: _json_safe(v) for k, v in zip(cols, row, strict=False)}
                for row in cur.fetchall()
            ]
        status = "fail" if n else "pass"
        msg = _message(c, n, sample)
    except duckdb.CatalogException as exc:
        if c.optional:
            return CheckResult(
                c.name,
                c.type,
                c.table,
                c.severity,
                "pass",
                0,
                "skipped: table missing",
                [],
                c.description,
            )
        n, status, sample, msg = 0, "error", [], f"cannot run: {exc}".splitlines()[0]
    except duckdb.Error as exc:
        n, status, sample, msg = 0, "error", [], f"cannot run: {exc}".splitlines()[0]
    return CheckResult(
        c.name, c.type, c.table, c.severity, status, int(n), msg, sample, c.description
    )


def run_checks(
    con: duckdb.DuckDBPyConnection,
    checks: list[Check],
    cfg: Config | None = None,
    as_of: dt.date | None = None,
) -> list[CheckResult]:
    as_of = as_of or (cfg.as_of if cfg and cfg.as_of else dt.date.today())
    return [run_check(con, c, cfg, as_of) for c in checks]


def store_results(con: duckdb.DuckDBPyConnection, results: list[CheckResult]) -> str:
    """Append this run's results; keep the newest KEEP_RUNS runs."""
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
    con.executemany(
        "INSERT INTO meta_check_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [
                run_id,
                now,
                r.name,
                r.type,
                r.table,
                r.severity,
                r.status,
                r.failing_rows,
                r.message,
                json.dumps(r.sample, default=str) if r.sample else None,
            ]
            for r in results
        ],
    )
    con.execute(
        "DELETE FROM meta_check_results WHERE run_id NOT IN ("
        "SELECT run_id FROM (SELECT run_id, MAX(checked_at) AS t FROM meta_check_results "
        "GROUP BY 1 ORDER BY t DESC, run_id DESC LIMIT ?))",
        [KEEP_RUNS],
    )
    return run_id


def latest_results(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    try:
        rows = con.execute(
            "SELECT name, check_type, table_name, severity, status, failing_rows, message, sample "
            "FROM meta_check_results WHERE run_id = ("
            "SELECT run_id FROM meta_check_results ORDER BY checked_at DESC, run_id DESC LIMIT 1) "
            "ORDER BY CASE status WHEN 'pass' THEN 1 ELSE 0 END, "
            "CASE severity WHEN 'error' THEN 0 ELSE 1 END, name"
        ).fetchall()
    except duckdb.CatalogException:
        return []
    return [
        CheckResult(
            r[0], r[1], r[2], r[3], r[4], int(r[5] or 0), r[6], json.loads(r[7]) if r[7] else []
        )
        for r in rows
    ]


def summarize(results: list[CheckResult]) -> dict[str, int]:
    return {
        "checks": len(results),
        "passed": sum(r.status == "pass" for r in results),
        "errors": sum(r.is_blocking for r in results),
        "warnings": sum(r.severity == "warn" and r.status != "pass" for r in results),
    }


def format_console(results: list[CheckResult]) -> str:
    s = summarize(results)
    lines = [
        f"Data quality: {s['passed']}/{s['checks']} checks passed, "
        f"{s['errors']} error(s), {s['warnings']} warning(s)."
    ]
    for r in results:
        if r.status == "pass":
            continue
        tag = "ERROR" if r.severity == "error" else "WARN"
        if r.status == "error":
            tag += "!"
        lines.append(f"  [{tag:<6}] {r.name:<36} {r.message}")
    return "\n".join(lines)


def as_alerts(results: list[CheckResult], as_of: dt.date) -> list[Any]:
    """Failed checks as Alert objects, so notifications carry them too."""
    from .alerts import Alert

    out = []
    for r in results:
        if r.status == "pass":
            continue
        out.append(
            Alert(
                rule=f"check:{r.name}",
                app_id="(data quality)",
                severity="critical" if r.severity == "error" else "warning",
                metric="data_quality",
                date=as_of,
                value=float(r.failing_rows),
                baseline=None,
                threshold=0.0,
                message=r.message,
            )
        )
    return out
