"""Radar tables in the DuckDB warehouse: schema, idempotent day loads, parameters."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from importlib import resources
from typing import Any

import duckdb
import pyarrow as pa

PERSONAL_FIELDS = ("developerEmail", "developerAddress", "privacyPolicy", "developerWebsite")


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    sql = resources.files("studio_pulse.radar").joinpath("schema.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    for stmt in body.split(";"):
        if stmt.strip():
            con.execute(stmt)


def columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]


def insert(con: duckdb.DuckDBPyConnection, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = columns(con, table)
    data = pa.Table.from_pylist([{c: r.get(c) for c in cols} for r in rows])
    con.register("_mr_rows", data)
    try:
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _mr_rows")
    finally:
        con.unregister("_mr_rows")
    return len(rows)


def replace(
    con: duckdb.DuckDBPyConnection,
    table: str,
    where: str,
    params: list[Any],
    rows: Iterable[Mapping[str, Any]],
) -> int:
    """Delete the slice `where`, then insert `rows` (one transaction)."""
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(f"DELETE FROM {table} WHERE {where}", params)
        n = insert(con, table, rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def set_params(con: duckdb.DuckDBPyConnection, params: Mapping[str, float]) -> None:
    con.execute("DELETE FROM raw_mr_params")
    if params:
        con.executemany(
            "INSERT INTO raw_mr_params VALUES (?, ?)", [[k, float(v)] for k, v in params.items()]
        )


def set_config(con: duckdb.DuckDBPyConnection, values: Mapping[str, Any]) -> None:
    con.execute("DELETE FROM raw_mr_config")
    rows = []
    for k, v in values.items():
        for item in v if isinstance(v, (list, tuple)) else [v]:
            rows.append([k, str(item)])
    if rows:
        con.executemany("INSERT INTO raw_mr_config VALUES (?, ?)", rows)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
