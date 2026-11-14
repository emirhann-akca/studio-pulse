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
    per_country = []
    apple = 0
    for cc in s.countries:
        lists = s.charts_for(cc)
        rss = len(lists)
        uniq = distinct.get(cc) or min(100 * len(lists), 0.62 * sum(c.limit for c in lists))
        lookup = math.ceil(uniq / max(1, s.lookup_batch))
        rechecks = 2
        watch = 1 if watched_apple else 0
        n = rss + lookup + rechecks + watch
        apple += n
        per_country.append(
            {"country": cc, "rss": rss, "lookup": lookup, "rechecks_max": rechecks, "watch": watch}
        )
    apple += math.ceil(len(s.publishers) / max(1, s.publisher_every_days))
    play = min(
        s.play_budget,
        (candidates + watched_play + watched_apple + (es.panel_size if es.enabled else 0))
        if s.play_enabled
        else 0,
    )
    seconds = max(apple * s.apple_spacing_s, play * s.play_spacing_s)
    return {
        "countries": s.countries,
        "apple_requests_per_day": apple,
        "apple_daily_budget": s.apple_budget,
        "play_requests_per_day": play,
        "play_daily_budget": s.play_budget,
        "minutes_at_spacing": round((apple * s.apple_spacing_s + play * s.play_spacing_s) / 60, 1),
        "minutes_lower_bound_per_host": round(seconds / 60, 1),
        "within_budget": apple <= s.apple_budget and play <= s.play_budget,
        "per_country": per_country,
        "assumptions": {
            "distinct_charted_per_country": {k: round(v) for k, v in distinct.items()}
            or "~62% of the summed list lengths (no snapshot yet)",
            "play_candidates": candidates,
            "watched": {"apple": watched_apple, "play": watched_play},
            "panel_size": es.panel_size if es.enabled else 0,
        },
    }
