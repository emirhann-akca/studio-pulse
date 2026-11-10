"""Firebase / GA4 BigQuery export connector.

Source: the standard GA4 export (`analytics_<property>.events_YYYYMMDD`).
Every query aggregates inside the source engine; only aggregates (and
per-window performance samples without any user identifier) are returned. User
ids never leave BigQuery.

Columns used: event_date, event_timestamp, event_name, event_params (key/value),
user_pseudo_id, user_first_touch_timestamp, platform, app_info.id,
app_info.version, event_value_in_usd. Each query is a full refresh of the
configured window, which also picks up events GA4 adds to a day's table late
(up to 72 hours after the day).

Every value that ends up inside the SQL text is validated first (event and
parameter names, app ids, the time zone): config files are trusted, but a
quote or backslash in an app id must not be able to change the query.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import duckdb

from .. import events as E
from ..warehouse import record_load, replace_slice
from .dialect import Dialect, check_ident, check_key, check_tz, sql_list

# Events that are logged without the user actively using the app.
PASSIVE_EVENTS = (
    "app_remove",
    "app_clear_data",
    "app_update",
    "os_update",
    "notification_receive",
    "notification_dismiss",
    "firebase_campaign",
)


@dataclass
class GA4Options:
    app_info_ids: list[str] = field(default_factory=list)
    iap_events: list[str] = field(default_factory=lambda: ["in_app_purchase"])
    ad_events: list[str] = field(default_factory=lambda: ["ad_impression"])
    new_user_events: list[str] = field(default_factory=lambda: ["first_open", "first_visit"])
    # Map a custom event's FPS parameter onto the perf model, e.g.
    # {"event": "camera_session_completed", "fps_param": "median_fps", "scene": "camera"}
    fps_events: list[dict[str, str]] = field(default_factory=list)
    churn_window_days: int = 7
    # The GA4 property's reporting time zone (event_date is already in it);
    # used for the first-touch fallback date. Set from the app's `timezone`.
    timezone: str = "UTC"
    queries: list[str] = field(
        default_factory=lambda: ["active_users", "cohorts", "event_counts", "levels", "level_max", "perf", "loads"]
    )

    @classmethod
    def from_source(cls, src: dict[str, Any]) -> GA4Options:
        o = cls()
        for k in ("app_info_ids", "iap_events", "ad_events", "new_user_events", "fps_events", "queries"):
            if k in src:
                setattr(o, k, list(src[k]))
        if "churn_window_days" in src:
            o.churn_window_days = int(src["churn_window_days"])
        for e in o.iap_events + o.ad_events + o.new_user_events:
            check_key(e)
        for i in o.app_info_ids:
            check_ident(i, "app_info_id")
        for f in o.fps_events:
            check_key(f["event"])
            check_key(f["fps_param"])
        return o


class GA4Queries:
    def __init__(self, d: Dialect, start: dt.date, end: dt.date, opts: GA4Options):
        self.d, self.start, self.end, self.o = d, start, end, opts
        check_tz(opts.timezone)

    # -- shared fragments -----------------------------------------------------
    def _where(self, start: dt.date | None = None, extra: str = "") -> str:
        parts = [self.d.ga4_window(start or self.start, self.end), "user_pseudo_id IS NOT NULL"]
        if self.o.app_info_ids:
            # Validated, not escaped: BigQuery string literals honour backslash
            # escapes, so stripping quotes alone would not be safe.
            ids = ", ".join(f"'{check_ident(i, 'app_info_id')}'" for i in self.o.app_info_ids)
            parts.append(f"app_info.id IN ({ids})")
        if extra:
            parts.append(extra)
        return " AND ".join(parts)

    def _active_filter(self) -> str:
        return f"event_name NOT IN ({sql_list(PASSIVE_EVENTS)})"

    # -- queries --------------------------------------------------------------
    def active_users(self) -> str:
        """Daily DAU/WAU/MAU per platform, plus same-day sessions, crashes, revenue."""
        d = self.d
        hist_start = self.start - dt.timedelta(days=29)
        iap, ad, new = sql_list(self.o.iap_events), sql_list(self.o.ad_events), sql_list(self.o.new_user_events)
        return f"""
WITH ev AS (
    SELECT
        {d.event_date()} AS d,
        user_pseudo_id AS u,
        UPPER(platform) AS platform,
        event_name,
        event_timestamp AS ts,
        {d.param_int('ga_session_id')} AS sid,
        {d.param_num('engagement_time_msec')} AS eng_ms,
        {d.param_num('fatal')} AS fatal,
        event_value_in_usd AS value_usd
    FROM {d.ga4_events()}
    WHERE {self._where(hist_start, self._active_filter())}
),{self._money_day(iap, ad)},
activity_day AS (
    SELECT
        d, platform, u,
        COUNT(DISTINCT sid) AS sessions,
        SUM(COALESCE(eng_ms, 0)) / 1000.0 AS eng_sec,
        MAX(CASE WHEN event_name IN ({new}) THEN 1 ELSE 0 END) AS is_new,
        MAX(CASE WHEN event_name = 'app_exception' AND fatal = 1 THEN 1 ELSE 0 END) AS crashed,
        COUNT(DISTINCT CASE WHEN event_name = 'app_exception' AND fatal = 1 THEN sid END) AS crashed_sessions
    FROM ev
    GROUP BY d, platform, u
),
user_day AS (
    SELECT a.*, COALESCE(m.iap_usd, 0) AS iap_usd, COALESCE(m.ad_usd, 0) AS ad_usd
    FROM activity_day a
    LEFT JOIN money_day m ON m.d = a.d AND m.platform = a.platform AND m.u = a.u
),
bounds AS (SELECT MIN(d) AS first_d FROM user_day),
spine AS ({d.date_spine(self.start, self.end)})
SELECT
    s.day AS date,
    ud.platform,
    COUNT(DISTINCT CASE WHEN ud.d = s.day THEN ud.u END) AS dau,
    COUNT(DISTINCT CASE WHEN ud.d >= {d.add_days('s.day', -6)} THEN ud.u END) AS wau,
    COUNT(DISTINCT ud.u) AS mau,
    SUM(CASE WHEN ud.d = s.day THEN ud.is_new ELSE 0 END) AS new_users,
    SUM(CASE WHEN ud.d = s.day THEN ud.sessions ELSE 0 END) AS sessions,
    SUM(CASE WHEN ud.d = s.day THEN ud.eng_sec ELSE 0 END) AS engagement_sec,
    SUM(CASE WHEN ud.d = s.day THEN ud.crashed ELSE 0 END) AS crashed_users,
    SUM(CASE WHEN ud.d = s.day THEN ud.crashed_sessions ELSE 0 END) AS crashed_sessions,
    SUM(CASE WHEN ud.d = s.day THEN ud.iap_usd ELSE 0 END) AS iap_revenue_usd,
    SUM(CASE WHEN ud.d = s.day THEN ud.ad_usd ELSE 0 END) AS ad_revenue_usd,
    COUNT(DISTINCT CASE WHEN ud.d = s.day AND ud.iap_usd > 0 THEN ud.u END) AS payers,
    MIN({d.days_between('s.day', 'b.first_d')}) + 1 AS history_days
FROM spine s
JOIN user_day ud ON ud.d BETWEEN {d.add_days('s.day', -29)} AND s.day
CROSS JOIN bounds b
GROUP BY s.day, ud.platform
"""

    def cohorts(self) -> str:
        """Install cohorts x day N: active users, sessions, revenue, first payers."""
        d = self.d
        iap, ad, new = sql_list(self.o.iap_events), sql_list(self.o.ad_events), sql_list(self.o.new_user_events)
        return f"""
WITH ev AS (
    SELECT
        {d.event_date()} AS d,
        user_pseudo_id AS u,
        UPPER(platform) AS platform,
        event_name,
        event_timestamp AS ts,
        {d.micros_to_date('user_first_touch_timestamp', self.o.timezone)} AS first_touch_d,
        {d.param_int('ga_session_id')} AS sid,
        event_value_in_usd AS value_usd
    FROM {d.ga4_events()}
    WHERE {self._where(None, self._active_filter())}
),{self._money_day(iap, ad)},
cohort AS (
    SELECT u, platform,
           COALESCE(MIN(CASE WHEN event_name IN ({new}) THEN d END), MIN(first_touch_d)) AS cohort_date
    FROM ev
    GROUP BY u, platform
),
user_day AS (
    SELECT a.d, a.platform, a.u, a.sessions,
           COALESCE(m.iap_usd, 0) AS iap_usd, COALESCE(m.ad_usd, 0) AS ad_usd
    FROM (SELECT d, platform, u, COUNT(DISTINCT sid) AS sessions FROM ev GROUP BY d, platform, u) a
    LEFT JOIN money_day m ON m.d = a.d AND m.platform = a.platform AND m.u = a.u
),
first_pay AS (
    SELECT u, platform, MIN(d) AS pay_d
    FROM ev
    WHERE event_name IN ({iap}) AND COALESCE(value_usd, 0) > 0
    GROUP BY u, platform
),
activity AS (
    SELECT c.platform, c.cohort_date, {d.days_between('ud.d', 'c.cohort_date')} AS day_n,
           COUNT(*) AS users, SUM(ud.sessions) AS sessions,
           SUM(ud.iap_usd) AS iap_revenue_usd, SUM(ud.ad_usd) AS ad_revenue_usd
    FROM user_day ud
    JOIN cohort c ON c.u = ud.u AND c.platform = ud.platform
    WHERE c.cohort_date BETWEEN {d.date_lit(self.start)} AND {d.date_lit(self.end)}
      AND ud.d >= c.cohort_date
    GROUP BY 1, 2, 3
),
payers AS (
    SELECT c.platform, c.cohort_date, {d.days_between('p.pay_d', 'c.cohort_date')} AS day_n,
           COUNT(*) AS new_payers
    FROM first_pay p
    JOIN cohort c ON c.u = p.u AND c.platform = p.platform
    WHERE c.cohort_date BETWEEN {d.date_lit(self.start)} AND {d.date_lit(self.end)}
      AND p.pay_d >= c.cohort_date
    GROUP BY 1, 2, 3
)
SELECT
    COALESCE(a.platform, p.platform) AS platform,
    COALESCE(a.cohort_date, p.cohort_date) AS cohort_date,
    COALESCE(a.day_n, p.day_n) AS day_n,
    COALESCE(a.users, 0) AS users,
    COALESCE(a.sessions, 0) AS sessions,
    COALESCE(a.iap_revenue_usd, 0) AS iap_revenue_usd,
    COALESCE(a.ad_revenue_usd, 0) AS ad_revenue_usd,
    COALESCE(p.new_payers, 0) AS new_payers
FROM activity a
FULL OUTER JOIN payers p
    ON a.platform = p.platform AND a.cohort_date = p.cohort_date AND a.day_n = p.day_n
"""

    @staticmethod
    def _money_day(iap: str, ad: str) -> str:
        """Purchase and ad revenue per user and day, each event counted once.

        The export can hold the same event twice (client retries); for revenue
        that would double count, so rows identical in user, timestamp, name and
        value are collapsed before summing. Only monetary events are deduped,
        which keeps the DISTINCT small.
        """
        return f"""
money AS (
    SELECT DISTINCT d, platform, u, event_name, ts, value_usd
    FROM ev
    WHERE event_name IN ({iap}) OR event_name IN ({ad})
),
money_day AS (
    SELECT d, platform, u,
           SUM(CASE WHEN event_name IN ({iap}) THEN COALESCE(value_usd, 0) ELSE 0 END) AS iap_usd,
           SUM(CASE WHEN event_name IN ({ad}) THEN COALESCE(value_usd, 0) ELSE 0 END) AS ad_usd
    FROM money
    GROUP BY d, platform, u
)"""

    def event_counts(self) -> str:
        d = self.d
        return f"""
SELECT {d.event_date()} AS date, event_name, COUNT(*) AS events, COUNT(DISTINCT user_pseudo_id) AS users
FROM {d.ga4_events()}
WHERE {self._where()}
GROUP BY 1, 2
"""

    def _level_events(self) -> str:
        d = self.d
        names = sql_list((E.LEVEL_START, E.LEVEL_COMPLETE, E.LEVEL_FAIL, E.LEVEL_END))
        return f"""
    SELECT
        {d.event_date()} AS d,
        user_pseudo_id AS u,
        CASE
            WHEN event_name = '{E.LEVEL_START}' THEN 'start'
            WHEN event_name = '{E.LEVEL_COMPLETE}' THEN 'complete'
            WHEN event_name = '{E.LEVEL_FAIL}' THEN 'fail'
            WHEN {d.param_num('success')} = 1 OR LOWER({d.param_str('success')}) IN ('true', 'yes') THEN 'complete'
            ELSE 'fail'
        END AS action,
        CAST(COALESCE({d.param_num(E.PARAM_LEVEL)}, {d.param_num('level_name')}) AS {d.int_type}) AS level
    FROM {d.ga4_events()}
    WHERE {self._where(None, f"event_name IN ({names})")}"""

    def levels(self) -> str:
        return f"""
WITH lv AS ({self._level_events()}
)
SELECT d AS date, level, action, COUNT(*) AS events, COUNT(DISTINCT u) AS users
FROM lv
WHERE level IS NOT NULL
GROUP BY 1, 2, 3
"""

    def level_max(self) -> str:
        d = self.d
        churn_cutoff = self.end - dt.timedelta(days=self.o.churn_window_days)
        return f"""
WITH lv AS ({self._level_events()}
),
mx AS (
    SELECT u, MAX(level) AS max_level FROM lv WHERE action = 'start' AND level IS NOT NULL GROUP BY u
),
last_seen AS (
    SELECT user_pseudo_id AS u, MAX({d.event_date()}) AS last_d
    FROM {d.ga4_events()}
    WHERE {self._where(None, self._active_filter())}
    GROUP BY 1
)
SELECT
    m.max_level AS level,
    COUNT(*) AS users_max_here,
    SUM(CASE WHEN l.last_d <= {d.date_lit(churn_cutoff)} THEN 1 ELSE 0 END) AS users_churned
FROM mx m
JOIN last_seen l ON l.u = m.u
GROUP BY 1
"""

    def perf(self) -> str:
        d = self.d
        names = [E.PERF_EVENT] + [f["event"] for f in self.o.fps_events]
        ft50 = [f"WHEN event_name = '{E.PERF_EVENT}' THEN {d.param_num(E.PARAM_FT_P50)}"]
        scene = [f"WHEN event_name = '{E.PERF_EVENT}' THEN {d.param_str(E.PARAM_SCENE)}"]
        for f in self.o.fps_events:
            ev, p = check_key(f["event"]), check_key(f["fps_param"])
            sc = check_key(f.get("scene", ev))
            ft50.append(f"WHEN event_name = '{ev}' THEN 1000.0 / NULLIF({d.param_num(p)}, 0)")
            scene.append(f"WHEN event_name = '{ev}' THEN '{sc}'")
        is_perf = f"event_name = '{E.PERF_EVENT}'"

        def only_perf(expr: str) -> str:
            return f"CASE WHEN {is_perf} THEN {expr} END"

        return f"""
SELECT
    {d.event_date()} AS date,
    UPPER(platform) AS platform,
    app_info.version AS app_version,
    COALESCE({only_perf(d.param_str(E.PARAM_TIER))}, 'unknown') AS device_tier,
    CASE {' '.join(scene)} END AS scene,
    {only_perf(d.param_num(E.PARAM_WINDOW_S))} AS window_s,
    CAST({only_perf(d.param_num(E.PARAM_FRAMES))} AS {d.int_type}) AS frames,
    CASE {' '.join(ft50)} END AS ft_p50_ms,
    {only_perf(d.param_num(E.PARAM_FT_P95))} AS ft_p95_ms,
    {only_perf(d.param_num(E.PARAM_FT_MAX))} AS ft_max_ms,
    CAST({only_perf(d.param_num(E.PARAM_SLOW_FRAMES))} AS {d.int_type}) AS slow_frames,
    {only_perf(d.param_num(E.PARAM_MEM_ALLOC))} AS mem_alloc_mb,
    {only_perf(d.param_num(E.PARAM_MEM_RESERVED))} AS mem_reserved_mb
FROM {d.ga4_events()}
WHERE {self._where(None, f"event_name IN ({sql_list(names)})")}
"""

    def loads(self) -> str:
        d = self.d
        return f"""
SELECT
    {d.event_date()} AS date,
    UPPER(platform) AS platform,
    app_info.version AS app_version,
    COALESCE({d.param_str(E.PARAM_TIER)}, 'unknown') AS device_tier,
    {d.param_str(E.PARAM_KIND)} AS kind,
    {d.param_str(E.PARAM_SCENE)} AS scene,
    {d.param_num(E.PARAM_DURATION)} AS duration_ms
FROM {d.ga4_events()}
WHERE {self._where(None, f"event_name = '{E.LOAD_EVENT}'")}
"""


# query name -> (raw table, date column used for freshness)
_TARGETS = {
    "active_users": ("raw_ga4_active_users", "date"),
    "cohorts": ("raw_ga4_cohort_days", "cohort_date"),
    "event_counts": ("raw_ga4_event_counts", "date"),
    "levels": ("raw_level_daily", "date"),
    "level_max": ("raw_level_max", None),
    "perf": ("raw_perf_windows", "date"),
    "loads": ("raw_load_times", "date"),
}


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    dialect: Dialect,
    executor: Any,
    start: dt.date,
    end: dt.date,
    opts: GA4Options,
    log: Any = print,
) -> dict[str, int]:
    """Run every configured GA4 query and replace this source's slice."""
    q = GA4Queries(dialect, start, end, opts)
    loaded: dict[str, int] = {}
    for name in opts.queries:
        if name not in _TARGETS:
            raise ValueError(f"unknown GA4 query {name!r}")
        table, date_col = _TARGETS[name]
        sql = getattr(q, name)()
        before = len(executor.stats)
        result = executor.run(f"{app_id}.ga4.{name}", sql)
        if result is None:  # dry-run only
            continue
        constants: dict[str, Any] = {}
        if name == "level_max":
            constants = {"as_of_date": end, "churn_window_days": opts.churn_window_days}
        n = replace_slice(con, table, result, app_id=app_id, source=source, constants=constants)
        billed = executor.stats[-1].billed_bytes if len(executor.stats) > before else None
        record_load(con, app_id=app_id, source=source, table=table, rows=n, date_col=date_col, bytes_billed=billed, detail=name)
        loaded[name] = n
        log(f"    ga4 {name:<13} -> {table:<22} {n:>7} rows")
    return loaded
