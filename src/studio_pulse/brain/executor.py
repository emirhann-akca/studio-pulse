"""A locked, read-only DuckDB session for SQL the package did not write.

Layer 2 and 3 of the SQL protection (layer 1 is guard.py):

  * the warehouse file is opened read_only (DuckDB refuses every write);
  * enable_external_access = false: no file, URL or extension reads
    (read_csv('/etc/passwd') fails in the engine even if the AST guard missed it);
  * autoload / autoinstall of extensions and community extensions off;
  * memory_limit and threads capped, temp directory disabled;
  * lock_configuration = true: the query cannot switch any of this back on;
  * a row cap (fetchmany) and a wall-clock timeout: a watchdog thread calls
    con.interrupt(), which works the same on Windows, macOS and Linux (no signals).

DuckDB's own security page says these settings are not a sandbox; they are a
second wall behind the guard, and the warehouse holds aggregates only (no
user rows), so the worst case of a bypass is reading an aggregate table.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from . import guard

DEFAULT_TIMEOUT_S = 10.0

# Tables a question may read: aggregates only. raw_* (loader input), stg_store_reviews
# (review text, untrusted and personal), meta_loads/meta_run (bookkeeping) are not;
# nor are the per-install SDK tables (stg_pulse_events, stg_sdk_installs, ...) or
# fct_sdk_errors, whose stack frames and messages are free text from the app.
ALLOWED_TABLES = (
    "fct_app_daily", "fct_app_summary", "fct_retention", "fct_retention_summary",
    "fct_cohort_ltv", "fct_ltv_curve", "fct_level_funnel", "fct_perf_daily",
    "fct_perf_by_tier", "fct_perf_by_version", "fct_load_daily", "fct_load_times",
    "fct_ua_daily", "fct_ua_summary", "fct_ua_cohort_roas", "fct_ua_roas_curve",
    "fct_creative_scorecard", "fct_review_daily", "fct_review_topics",
    "fct_rating_by_version", "fct_android_vitals_daily", "fct_source_freshness",
    "fct_experiment_arms", "fct_experiment_tests", "fct_pltv_summary", "fct_pltv_curve",
    "stg_cohort_days", "stg_cohort_dims", "stg_segment_retention", "meta_apps",
    # studio SDK aggregates (collector): apps and store status, level funnel with quits,
    # where players fail, settings turned off, purchases, ad revenue, crash-free sessions
    "fct_sdk_apps", "fct_sdk_level_funnel", "fct_sdk_fail_heatmap", "fct_sdk_settings",
    "fct_sdk_settings_when", "fct_sdk_purchases", "fct_sdk_ad_revenue_daily",
    "fct_sdk_errors_daily", "fct_sdk_data_quality",
)  # fmt: skip


class QueryTimeout(RuntimeError):
    pass


@dataclass
class Result:
    sql: str
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    seconds: float
    tables: tuple[str, ...] = ()

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]


@dataclass
class LockedSession:
    """Open with `LockedSession.open(path)`; use as a context manager."""

    con: duckdb.DuckDBPyConnection
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_rows: int = guard.MAX_ROWS
    allowed: tuple[str, ...] = ALLOWED_TABLES
    _schema: dict[str, dict[str, str]] | None = field(default=None, repr=False)

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_rows: int = guard.MAX_ROWS,
        memory_limit: str = "1GB",
        threads: int = 2,
        allowed: tuple[str, ...] = ALLOWED_TABLES,
    ) -> LockedSession:
        if str(path) == ":memory:":
            raise ValueError("the locked session reads a warehouse file")
        config = {
            "enable_external_access": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "allow_community_extensions": False,
            "memory_limit": memory_limit,
            "threads": threads,
            "max_temp_directory_size": "0B",
        }
        # DuckDB refuses a second in-process connection to the same file with another
        # configuration; the UI's other pages hold theirs for one request, so wait briefly.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                con = duckdb.connect(str(path), read_only=True, config=config)
                break
            except duckdb.ConnectionException:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        con.execute("SET TimeZone = 'UTC'")
        con.execute("SET lock_configuration = true")
        return cls(con, timeout_s=timeout_s, max_rows=max_rows, allowed=allowed)

    def __enter__(self) -> LockedSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.con.close()
        except duckdb.Error:
            pass

    def schema(self) -> dict[str, dict[str, str]]:
        """{table: {column: type}} of the allow-listed tables present in the file."""
        if self._schema is None:
            rows = self.con.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
            ).fetchall()
            allowed = set(self.allowed)
            out: dict[str, dict[str, str]] = {}
            for t, c, ty in rows:
                if t in allowed:
                    out.setdefault(t, {})[c] = ty
            self._schema = out
        return self._schema

    def run_untrusted(self, sql: str) -> Result:
        """Guard, then run with the row cap and timeout."""
        checked = guard.check(sql, self.schema(), max_rows=self.max_rows)
        res = self._run(checked.sql, [])
        res.tables = checked.tables
        return res

    def run_trusted(self, sql: str, params: list[Any] | None = None) -> Result:
        """SQL built by the package from the catalog (bound params), same limits."""
        return self._run(sql, params or [])

    def _run(self, sql: str, params: list[Any]) -> Result:
        fired = threading.Event()

        def stop() -> None:
            fired.set()
            try:
                self.con.interrupt()
            except duckdb.Error:
                pass

        timer = threading.Timer(self.timeout_s, stop)
        t0 = time.perf_counter()
        timer.start()
        try:
            cur = self.con.execute(sql, params)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(self.max_rows + 1)
        except duckdb.InterruptException:
            raise QueryTimeout(f"query stopped after {self.timeout_s:g} s") from None
        except duckdb.Error:
            if fired.is_set():
                raise QueryTimeout(f"query stopped after {self.timeout_s:g} s") from None
            raise
        finally:
            timer.cancel()
        truncated = len(rows) > self.max_rows
        return Result(
            sql=sql,
            columns=columns,
            rows=[tuple(_plain(v) for v in r) for r in rows[: self.max_rows]],
            truncated=truncated,
            seconds=time.perf_counter() - t0,
        )


def _plain(v: Any) -> Any:
    """JSON-friendly cell: HUGEINT/Decimal -> int/float, dates stay dates."""
    if isinstance(v, bool) or v is None or isinstance(v, (str, float, dt.date)):
        return v
    if isinstance(v, int):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return int(f) if f.is_integer() and abs(f) < 2**53 else f
