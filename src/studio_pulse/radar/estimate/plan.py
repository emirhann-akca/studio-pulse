"""Expected requests per day for a radar config (`pulse radar plan`, docs/RADAR.md).

Apple (itunes.apple.com): one RSS request per list and country, a batched Lookup per 100
charted apps per storefront (the five default lists of 100 hold ~250-320 distinct apps), up
to two rechecks per storefront when a rating count looks wrong, one Lookup per storefront for
the watchlist, publisher catalogues weekly. Google Play: the radar's own candidates, watched
games and the calibration panel, never more than the daily budget (at most 200). Every
request waits at least 3 s after the previous one to the same host.
"""

from __future__ import annotations

import math
from typing import Any

import duckdb

from ..settings import RadarSettings
from .settings import EstimateSettings


def request_plan(
    s: RadarSettings, es: EstimateSettings, con: duckdb.DuckDBPyConnection | None = None
) -> dict[str, Any]:
    distinct = {}
    watched_apple = watched_play = 0
    candidates = 50
    if con is not None:

        def q(sql: str, params: list[Any] | None = None) -> list[tuple]:
            try:
                return con.execute(sql, params or []).fetchall()
            except duckdb.Error:  # a table that does not exist yet
                return []

        distinct = dict(
            q(
                "SELECT country, AVG(n) FROM (SELECT snapshot_date, country, "
                "COUNT(DISTINCT app_id) AS n FROM raw_mr_chart WHERE snapshot_date >= "
                "(SELECT MAX(snapshot_date) - 7 FROM raw_mr_chart) AND rank <= requested_limit "
                "GROUP BY 1, 2) GROUP BY 1"
            )
        )
        for st, n in q(
            "SELECT store, COUNT(*) FROM raw_mr_watch WHERE removed_at IS NULL GROUP BY 1"
        ):
            if st == "apple":
                watched_apple = int(n)
            else:
                watched_play = int(n)
        row = q(
            "SELECT COUNT(*) FROM fct_mr_breakout_daily WHERE snapshot_date = "
            "(SELECT MAX(snapshot_date) FROM fct_mr_breakout_daily) AND age_days <= ?",
            [s.params["late_age_max"]],
        )
        if row:
            candidates = int(row[0][0])
    web = s.apple_chart_source == "web"
    web_charts = ("topfreeapplications", "toppaidapplications")
    per_country = []
    apple = pages = 0
    for cc in s.countries:
        lists = s.charts_for(cc)
        if web:  # one apps.apple.com page per genre holds its top-free and top-paid lists
            usable = [c for c in lists if c.chart in web_charts]
            chart_req = len({c.genre for c in usable})
            pages += chart_req
        else:  # legacy RSS (replay only): one request per list
            usable = lists
            chart_req = len(lists)
            apple += chart_req
        uniq = distinct.get(cc) or min(100 * len(usable), 0.62 * sum(c.limit for c in usable))
        lookup = math.ceil(uniq / max(1, s.lookup_batch))
        rechecks = 2
        watch = 1 if watched_apple else 0
        apple += lookup + rechecks + watch
        per_country.append(
            {
                "country": cc,
                "chart_requests": chart_req,
                "chart_host": "apps.apple.com" if web else "itunes.apple.com (rss, replay only)",
                "lookup": lookup,
                "rechecks_max": rechecks,
                "watch": watch,
            }
        )
    apple += math.ceil(len(s.publishers) / max(1, s.publisher_every_days))
    play = min(
        s.play_budget,
        (candidates + watched_play + watched_apple + (es.panel_size if es.enabled else 0))
        if s.play_enabled
        else 0,
    )
    total = apple + pages + play
    seconds = max(apple * s.apple_spacing_s, pages * 3.0, play * s.play_spacing_s)
    return {
        "countries": s.countries,
        "chart_source": s.apple_chart_source,
        "apps_apple_com_pages_per_day": pages,
        "apps_apple_com_daily_budget": s.apple_web_budget,
        "itunes_requests_per_day": apple,
        "itunes_daily_budget": s.apple_budget,
        "play_requests_per_day": play,
        "play_daily_budget": s.play_budget,
        "minutes_at_spacing": round(total * 3.0 / 60, 1),
        "minutes_lower_bound_per_host": round(seconds / 60, 1),
        "within_budget": apple <= s.apple_budget
        and pages <= s.apple_web_budget
        and play <= s.play_budget,
        "per_country": per_country,
        "assumptions": {
            "distinct_charted_per_country": {k: round(v) for k, v in distinct.items()}
            or "~62% of the summed list lengths (no snapshot yet)",
            "play_candidates": candidates,
            "watched": {"apple": watched_apple, "play": watched_play},
            "panel_size": es.panel_size if es.enabled else 0,
            "lists_not_on_the_web": sorted(
                {c.key for c in s.charts if c.chart not in web_charts} if web else set()
            ),
        },
    }
