"""B3: dimensional cohort pulls, hand-written, aggregated inside BigQuery.

`cohort_dims`: install cohorts by day with day-0 users and D1/D7/D30 retained
users per cell of GROUPING SETS over platform, country, app version, first-
touch source ("network") and device tier: the total, every single dimension
and every pair. The platform-only cohort tables could not say why D1 fell;
the research's mix/rate run on the demo found the cause only once version
was a cohort dimension.

`segment_retention` (B3b): the same for the values of GA4 user properties
(e.g. music_enabled), with a day-0 engagement stratum (sessions 1 / 2 / 3+) so
a correlation can be shown with and without the engagement mix.

Both run through the existing executors: on BigQuery that means one SELECT,
a dry run first, refused above the bytes cap and run with
maximum_bytes_billed. Privacy: no ids leave the query, and a cell with fewer
than `min_cell_users` (default 50) day-0 users is not returned at all.

Dimension values are taken on the user's install day: the version and
country of their first event, the tier of their first pulse_perf event (or a
model -> tier map from the config), the first-touch source of GA4's user-
scoped traffic_source (organic and (none) mediums become `organic`).
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from typing import Any

from ..connectors.dialect import Dialect, check_key, sql_list

DIMS = ("platform", "country", "app_version", "network", "device_tier")
DAYS = (1, 7, 30)
DEFAULT_MIN_CELL = 50
_MODEL_RE = re.compile(r"^[A-Za-z0-9 _\-\.\+\(\)/]{1,64}$")
_TIERS = ("low", "mid", "high")


def grouping_sets() -> list[tuple[str, ...]]:
    """(), each dimension, each pair: 16 sets, cohort_date added to all of them."""
    sets: list[tuple[str, ...]] = [()]
    sets += [(d,) for d in DIMS]
    sets += list(itertools.combinations(DIMS, 2))
    return sets


def _first_by(d: Dialect, expr: str, order: str) -> str:
    """The value of `expr` on the row with the smallest `order`, NULLs skipped."""
    if d.name == "bigquery":
        return f"ARRAY_AGG({expr} IGNORE NULLS ORDER BY {order} LIMIT 1)[SAFE_OFFSET(0)]"
    return f"arg_min({expr}, {order}) FILTER (WHERE {expr} IS NOT NULL)"


def _tier_case(tier_by_model: dict[str, list[str]] | None) -> str:
    if not tier_by_model:
        return "NULL"
    whens = []
    for tier, models in tier_by_model.items():
        if tier not in _TIERS:
            raise ValueError(f"tier_by_model: tier must be one of {_TIERS}, got {tier!r}")
        for m in models:
            if not _MODEL_RE.match(m) or "'" in m or "\\" in m:
                raise ValueError(f"tier_by_model: invalid model name {m!r}")
            whens.append(f"WHEN '{m}' THEN '{tier}'")
    return f"CASE device.mobile_model_name {' '.join(whens)} END"


def cohort_dims_sql(
    d: Dialect,
    start: dt.date,
    end: dt.date,
    *,
    where: str,
    new_user_events: list[str],
    timezone: str,
    min_cell_users: int = DEFAULT_MIN_CELL,
    tier_by_model: dict[str, list[str]] | None = None,
) -> str:
    """SQL for cohorts x GROUPING SETS of DIMS; `where` is GA4Queries._where's filter."""
    if min_cell_users < 1:
        raise ValueError("min_cell_users must be >= 1")
    new = sql_list(new_user_events)
    sets = ", ".join("(" + ", ".join(("cohort_date", *s)) + ")" for s in grouping_sets())
    flags = ",\n        ".join(
        f"MAX(CASE WHEN {d.days_between('a.d', 'c.cohort_date')} = {n} THEN 1 ELSE 0 END) AS r{n}"
        for n in DAYS
    )
    sums = ", ".join(f"SUM(r{n}) AS d{n}_users" for n in DAYS)
    ts_order = "ts"
    return f"""
WITH ev AS (
    SELECT
        {d.event_date()} AS d,
        user_pseudo_id AS u,
        UPPER(platform) AS platform,
        event_name,
        event_timestamp AS ts,
        {d.micros_to_date("user_first_touch_timestamp", timezone)} AS first_touch_d,
        app_info.version AS app_version,
        geo.country AS country,
        LOWER(traffic_source.source) AS ts_source,
        LOWER(traffic_source.medium) AS ts_medium,
        CASE WHEN event_name = 'pulse_perf' THEN {d.param_str("tier")} END AS perf_tier,
        {_tier_case(tier_by_model)} AS model_tier
    FROM {d.ga4_events()}
    WHERE {where}
),
cohort AS (
    SELECT u, platform,
           COALESCE(MIN(CASE WHEN event_name IN ({new}) THEN d END), MIN(first_touch_d)) AS cohort_date,
           MAX(ts_source) AS ts_source, MAX(ts_medium) AS ts_medium,
           {_first_by(d, "perf_tier", ts_order)} AS perf_tier,
           {_first_by(d, "model_tier", ts_order)} AS model_tier
    FROM ev
    GROUP BY u, platform
),
day0 AS (
    SELECT e.u, e.platform,
           {_first_by(d, "e.app_version", "e.ts")} AS app_version,
           {_first_by(d, "e.country", "e.ts")} AS country
    FROM ev e
    JOIN cohort c ON c.u = e.u AND c.platform = e.platform AND e.d = c.cohort_date
    GROUP BY e.u, e.platform
),
users AS (
    SELECT c.u, c.platform, c.cohort_date,
           COALESCE(z.country, '(not set)') AS country,
           COALESCE(z.app_version, '(not set)') AS app_version,
           CASE WHEN c.ts_source IS NULL OR COALESCE(c.ts_medium, '(none)') IN ('(none)', 'organic', '(not set)')
                THEN 'organic' ELSE c.ts_source END AS network,
           COALESCE(c.perf_tier, c.model_tier, 'unknown') AS device_tier
    FROM cohort c
    LEFT JOIN day0 z ON z.u = c.u AND z.platform = c.platform
    WHERE c.cohort_date BETWEEN {d.date_lit(start)} AND {d.date_lit(end)}
),
act AS (SELECT DISTINCT u, platform, d FROM ev),
per_user AS (
    SELECT c.cohort_date, c.platform, c.country, c.app_version, c.network, c.device_tier,
        {flags}
    FROM users c
    LEFT JOIN act a ON a.u = c.u AND a.platform = c.platform AND a.d > c.cohort_date
    GROUP BY c.u, c.cohort_date, c.platform, c.country, c.app_version, c.network, c.device_tier
)
SELECT
    cohort_date, platform, country, app_version, network, device_tier,
    COUNT(*) AS users, {sums},
    {d.days_between(d.date_lit(end), "cohort_date")} AS observed_days
FROM per_user
GROUP BY GROUPING SETS ({sets})
HAVING COUNT(*) >= {int(min_cell_users)}
"""


def segment_retention_sql(
    d: Dialect,
    start: dt.date,
    end: dt.date,
    *,
    where: str,
    properties: list[str],
    new_user_events: list[str],
    timezone: str,
    min_cell_users: int = DEFAULT_MIN_CELL,
) -> str:
    """Install cohorts x the day-0 value of each GA4 user property, with an engagement stratum."""
    if not properties:
        raise ValueError("segment_retention needs at least one user property")
    props = sql_list([check_key(p) for p in properties])
    new = sql_list(new_user_events)
    val = (
        "COALESCE(up.value.string_value, CAST(up.value.int_value AS {s}), "
        "CAST(up.value.double_value AS {s}), CAST(up.value.float_value AS {s}))"
    ).format(s="STRING" if d.name == "bigquery" else "VARCHAR")
    if d.name == "bigquery":
        src = (
            f"{d.ga4_events()}, UNNEST(user_properties) AS up WHERE {where} AND up.key IN ({props})"
        )
    else:
        src = (
            f"(SELECT *, unnest(user_properties) AS up FROM {d.ga4_events()} WHERE {where}) "
            f"WHERE up.key IN ({props})"
        )
    flags = ",\n        ".join(
        f"MAX(CASE WHEN {d.days_between('a.d', 'c.cohort_date')} = {n} THEN 1 ELSE 0 END) AS r{n}"
        for n in (1, 7)
    )
    return f"""
WITH ev AS (
    SELECT
        {d.event_date()} AS d,
        user_pseudo_id AS u,
        UPPER(platform) AS platform,
        event_name,
        event_timestamp AS ts,
        {d.micros_to_date("user_first_touch_timestamp", timezone)} AS first_touch_d,
        {d.param_int("ga_session_id")} AS sid
    FROM {d.ga4_events()}
    WHERE {where}
),
props AS (
    SELECT {d.event_date()} AS d, user_pseudo_id AS u, UPPER(platform) AS platform,
           event_timestamp AS ts, up.key AS property, {val} AS value
    FROM {src}
),
cohort AS (
    SELECT u, platform,
           COALESCE(MIN(CASE WHEN event_name IN ({new}) THEN d END), MIN(first_touch_d)) AS cohort_date
    FROM ev GROUP BY u, platform
),
seg AS (
    SELECT c.u, c.platform, c.cohort_date, p.property,
           {_first_by(d, "p.value", "p.ts")} AS value
    FROM cohort c
    JOIN props p ON p.u = c.u AND p.platform = c.platform AND p.d = c.cohort_date
    WHERE c.cohort_date BETWEEN {d.date_lit(start)} AND {d.date_lit(end)}
    GROUP BY c.u, c.platform, c.cohort_date, p.property
),
eng AS (
    SELECT e.u, e.platform, COUNT(DISTINCT e.sid) AS s0
    FROM ev e JOIN cohort c ON c.u = e.u AND c.platform = e.platform AND e.d = c.cohort_date
    GROUP BY e.u, e.platform
),
act AS (SELECT DISTINCT u, platform, d FROM ev),
per_user AS (
    SELECT c.cohort_date, c.property, COALESCE(c.value, '(not set)') AS value,
           CASE WHEN COALESCE(g.s0, 0) <= 1 THEN '1' WHEN g.s0 = 2 THEN '2' ELSE '3+' END AS day0_sessions,
        {flags}
    FROM seg c
    LEFT JOIN eng g ON g.u = c.u AND g.platform = c.platform
    LEFT JOIN act a ON a.u = c.u AND a.platform = c.platform AND a.d > c.cohort_date
    GROUP BY c.u, c.platform, c.cohort_date, c.property, c.value, g.s0
)
SELECT cohort_date, property, value, day0_sessions,
       COUNT(*) AS users, SUM(r1) AS d1_users, SUM(r7) AS d7_users,
       {d.days_between(d.date_lit(end), "cohort_date")} AS observed_days
FROM per_user
GROUP BY GROUPING SETS ((cohort_date, property, value), (cohort_date, property, value, day0_sessions))
HAVING COUNT(*) >= {int(min_cell_users)}
"""


def options_from_source(src: dict[str, Any]) -> dict[str, Any]:
    """cohort_dims / segment options of a GA4 source (validated)."""
    out: dict[str, Any] = {
        "min_cell_users": int(src.get("min_cell_users", DEFAULT_MIN_CELL)),
        "segments": [check_key(p) for p in src.get("segments") or []],
        "tier_by_model": src.get("tier_by_model") or None,
    }
    if out["min_cell_users"] < 1:
        raise ValueError("min_cell_users must be >= 1")
    if out["tier_by_model"] is not None:
        _tier_case(out["tier_by_model"])  # validates
    return out
