"""Firebase Crashlytics BigQuery export connector.

The Crashlytics export has one table per app and platform
(`firebase_crashlytics.<bundle_id_with_underscores>_<ANDROID|IOS>`), partitioned
by event_timestamp, one row per crash/non-fatal event. It contains crashes
only - no session counts - so crash-free users and sessions are computed in
fct_app_daily against GA4 DAU and sessions (see docs/METRICS.md).

Only daily aggregates are pulled: fatal/non-fatal event counts, distinct
crashing installations and distinct fatal issues.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb

from ..warehouse import record_load, replace_slice
from .dialect import Dialect


def daily_query(d: Dialect, table: str, start: dt.date, end: dt.date) -> str:
    end_excl = end + dt.timedelta(days=1)
    return f"""
SELECT
    {d.ts_to_date('event_timestamp')} AS date,
    UPPER(platform) AS platform,
    SUM(CASE WHEN is_fatal THEN 1 ELSE 0 END) AS fatal_events,
    SUM(CASE WHEN is_fatal THEN 0 ELSE 1 END) AS nonfatal_events,
    COUNT(DISTINCT CASE WHEN is_fatal THEN installation_uuid END) AS crashed_installs,
    COUNT(DISTINCT CASE WHEN is_fatal THEN issue_id END) AS fatal_issues
FROM {d.table(table)}
WHERE event_timestamp >= {d.ts_lit(start)} AND event_timestamp < {d.ts_lit(end_excl)}
GROUP BY 1, 2
"""


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    dialect: Dialect,
    executor: Any,
    tables: list[str],
    start: dt.date,
    end: dt.date,
    log: Any = print,
) -> int:
    import pyarrow as pa

    parts = []
    billed = 0
    for t in tables:
        before = len(executor.stats)
        res = executor.run(f"{app_id}.crashlytics.{t}", daily_query(dialect, t, start, end))
        if len(executor.stats) > before and executor.stats[-1].billed_bytes:
            billed += executor.stats[-1].billed_bytes
        if res is not None:
            parts.append(res)
    if not parts:
        return 0
    table = pa.concat_tables(parts, promote_options="permissive") if len(parts) > 1 else parts[0]
    n = replace_slice(con, "raw_crashlytics_daily", table, app_id=app_id, source=source)
    record_load(con, app_id=app_id, source=source, table="raw_crashlytics_daily", rows=n, bytes_billed=billed or None)
    log(f"    crashlytics   -> raw_crashlytics_daily  {n:>7} rows")
    return n
