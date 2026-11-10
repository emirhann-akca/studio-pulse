"""Query executors for source-side extraction.

BigQueryExecutor is deliberately conservative:
  * only single SELECT/WITH statements are accepted (assert_read_only);
  * every query is dry-run first and refused if the estimate exceeds the cap;
  * the real run sets maximum_bytes_billed, so BigQuery itself refuses to bill
    more than the cap even if the estimate was wrong;
  * no destination table, no DDL/DML, nothing is ever written to BigQuery.

DuckDBExecutor runs the same SQL against local GA4/Crashlytics-export-shaped
Parquet or JSON files (demo data and test fixtures).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from .dialect import DUCKDB_MACROS

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|EXPORT|LOAD|CALL|"
    r"DECLARE|BEGIN|COMMIT|ROLLBACK|SET|EXECUTE)\b",
    re.IGNORECASE,
)


class ReadOnlyViolation(ValueError):
    pass


class BytesCapExceeded(RuntimeError):
    pass


def assert_read_only(sql: str) -> None:
    """Refuse anything that is not a single SELECT / WITH ... SELECT."""
    stripped = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)  # drop string literals
    stripped = re.sub(r"--[^\n]*", "", stripped)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    body = stripped.strip().rstrip(";").strip()
    if ";" in body:
        raise ReadOnlyViolation("multiple statements are not allowed")
    if not re.match(r"^(WITH|SELECT)\b", body, re.IGNORECASE):
        raise ReadOnlyViolation("only SELECT queries are allowed")
    m = _FORBIDDEN.search(body)
    if m:
        raise ReadOnlyViolation(f"forbidden keyword in query: {m.group(1)}")


@dataclass
class QueryStat:
    name: str
    estimated_bytes: int | None
    billed_bytes: int | None
    rows: int | None
    dry_run_only: bool = False


@dataclass
class BigQueryExecutor:
    """Runs extraction SQL on BigQuery with a hard bytes cap."""

    billing_project: str
    max_bytes_billed: int = 2_000_000_000
    location: str | None = None
    dry_run_only: bool = False
    client: Any = None
    log: Callable[[str], None] = print
    stats: list[QueryStat] = field(default_factory=list)

    def _client(self) -> Any:
        if self.client is None:
            from google.cloud import bigquery  # lazy: optional dependency

            self.client = bigquery.Client(project=self.billing_project, location=self.location)
        return self.client

    @staticmethod
    def _job_config(**kwargs: Any) -> Any:
        from google.cloud import bigquery

        return bigquery.QueryJobConfig(**kwargs)

    def dry_run(self, sql: str) -> int:
        assert_read_only(sql)
        job = self._client().query(sql, job_config=self._job_config(dry_run=True, use_query_cache=False))
        return int(job.total_bytes_processed or 0)

    def run(self, name: str, sql: str) -> pa.Table | None:
        assert_read_only(sql)
        estimate = self.dry_run(sql)
        self.log(f"    [bq] {name}: dry-run estimate {estimate / 1e6:,.1f} MB (cap {self.max_bytes_billed / 1e6:,.0f} MB)")
        if estimate > self.max_bytes_billed:
            self.stats.append(QueryStat(name, estimate, None, None))
            raise BytesCapExceeded(
                f"{name}: estimated {estimate:,} bytes exceeds cap {self.max_bytes_billed:,}"
            )
        if self.dry_run_only:
            self.stats.append(QueryStat(name, estimate, None, None, dry_run_only=True))
            return None
        cfg = self._job_config(maximum_bytes_billed=self.max_bytes_billed, use_query_cache=True)
        job = self._client().query(sql, job_config=cfg)
        result = job.result()
        table = result.to_arrow(create_bqstorage_client=False)
        billed = int(job.total_bytes_billed or 0)
        self.stats.append(QueryStat(name, estimate, billed, table.num_rows))
        self.log(f"    [bq] {name}: {table.num_rows} rows, billed {billed / 1e6:,.1f} MB")
        return table


@dataclass
class DuckDBExecutor:
    """Runs extraction SQL against local export-shaped files."""

    views: dict[str, str | Path | list[str | Path]]
    log: Callable[[str], None] = lambda _msg: None
    stats: list[QueryStat] = field(default_factory=list)
    dry_run_only: bool = False

    def __post_init__(self) -> None:
        self.con = duckdb.connect()
        # Naive timestamps in export files are UTC; never let the machine's
        # local time zone leak into a TIMESTAMP <-> TIMESTAMPTZ cast.
        self.con.execute("SET TimeZone = 'UTC'")
        for stmt in DUCKDB_MACROS:
            self.con.execute(stmt)
        for view, src in self.views.items():
            paths = src if isinstance(src, list) else [src]
            files = [str(p) for p in paths]
            reader = self._reader(files)
            self.con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {reader}")

    @staticmethod
    def _reader(files: list[str]) -> str:
        quoted = "[" + ", ".join("'" + f.replace("'", "''") + "'" for f in files) + "]"
        if all(f.endswith(".parquet") for f in files):
            return f"read_parquet({quoted}, union_by_name = true)"
        return f"read_json({quoted}, format = 'newline_delimited', union_by_name = true)"

    def run(self, name: str, sql: str) -> pa.Table | None:
        assert_read_only(sql)
        table = self.con.sql(sql).to_arrow_table()
        self.stats.append(QueryStat(name, None, None, table.num_rows))
        return table
