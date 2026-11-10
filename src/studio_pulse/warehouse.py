"""DuckDB warehouse: connection, raw schema and idempotent slice loads."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa


def connect(path: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    # Source timestamps are UTC. DuckDB defaults to the machine's time zone,
    # which made TIMESTAMPTZ -> TIMESTAMP casts (e.g. RevenueCat Parquet) shift
    # purchase dates depending on where the pipeline ran.
    con.execute("SET TimeZone = 'UTC'")
    init_schema(con)
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    sql = resources.files("studio_pulse").joinpath("raw_schema.sql").read_text(encoding="utf-8")
    for stmt in _split_sql(sql):
        con.execute(stmt)


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)


def _to_arrow(rows: pa.Table | Iterable[Mapping[str, Any]]) -> pa.Table:
    if isinstance(rows, pa.Table):
        return rows
    rows = list(rows)
    return pa.Table.from_pylist(rows) if rows else pa.table({})


def replace_slice(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: pa.Table | Iterable[Mapping[str, Any]],
    *,
    app_id: str,
    source: str,
    where: str | None = None,
    params: list[Any] | None = None,
    constants: Mapping[str, Any] | None = None,
) -> int:
    """Delete the (app_id, _source[, where]) slice of `table`, then insert `rows`.

    Columns are matched by name; missing columns become NULL. Re-running the
    same load therefore never duplicates data.
    """
    data = _to_arrow(rows)
    cond = "app_id = ? AND _source = ?"
    args: list[Any] = [app_id, source]
    if where:
        cond += f" AND ({where})"
        args += list(params or [])
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(f"DELETE FROM {table} WHERE {cond}", args)
        n = 0
        if data.num_rows:
            consts = {"app_id": app_id, "_source": source, "_loaded_at": utcnow(), **(constants or {})}
            const_cols = [c for c in consts if c not in data.column_names]
            select = ", ".join(
                [f'"{c}"' for c in data.column_names] + [f"? AS \"{c}\"" for c in const_cols]
            )
            con.register("_pulse_incoming", data)
            try:
                con.execute(
                    f"INSERT INTO {table} BY NAME SELECT {select} FROM _pulse_incoming",
                    [consts[c] for c in const_cols],
                )
            finally:
                con.unregister("_pulse_incoming")
            n = data.num_rows
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def record_load(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    table: str,
    rows: int,
    date_col: str | None = "date",
    bytes_billed: int | None = None,
    detail: str | None = None,
) -> None:
    min_d = max_d = None
    if date_col:
        min_d, max_d = con.execute(
            f"SELECT MIN({date_col}), MAX({date_col}) FROM {table} WHERE app_id = ? AND _source = ?",
            [app_id, source],
        ).fetchone()
    con.execute(
        "INSERT INTO meta_loads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [app_id, source, table, rows, min_d, max_d, utcnow(), bytes_billed, detail],
    )


def upsert_apps(con: duckdb.DuckDBPyConnection, apps: Iterable[Any], is_demo: bool) -> None:
    con.execute("DELETE FROM meta_apps")
    for a in apps:
        con.execute(
            "INSERT INTO meta_apps (app_id, name, genre, platforms, is_demo, timezone) VALUES (?, ?, ?, ?, ?, ?)",
            [a.id, a.name, a.genre, ",".join(a.platforms), is_demo, getattr(a, "timezone", "UTC")],
        )


def load_fx_rates(con: duckdb.DuckDBPyConnection, rates: Mapping[str, float], valid_from: dt.date) -> None:
    """Replace the fallback FX table with a flat rate per currency."""
    con.execute("DELETE FROM raw_fx_rates")
    for cur, rate in sorted(rates.items()):
        con.execute("INSERT INTO raw_fx_rates VALUES (?, ?, ?)", [cur, rate, valid_from])
    if "USD" not in rates:
        con.execute("INSERT INTO raw_fx_rates VALUES ('USD', 1.0, ?)", [valid_from])


def set_meta(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    con.execute("DELETE FROM meta_run WHERE key = ?", [key])
    con.execute("INSERT INTO meta_run VALUES (?, ?)", [key, value])


def get_meta(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta_run WHERE key = ?", [key]).fetchone()
    return row[0] if row else None
