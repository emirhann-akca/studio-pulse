"""Portfolio-wide dashboard pages: UA (spend, ROAS, payback, creatives) and experiments."""

from __future__ import annotations

from html import escape
from typing import Any

import duckdb

from .charts import (
    Series,
    data_table,
    empty_chart,
    fmt_compact,
    fmt_int,
    fmt_num,
    fmt_pct,
    fmt_usd,
    line_chart,
    tile,
)

NETWORK_LABELS = {
    "meta": "Meta",
    "google_ads": "Google Ads",
    "tiktok": "TikTok",
    "applovin": "AppLovin",
    "unity_ads": "Unity Ads",
    "mintegral": "Mintegral",
    "ironsource": "ironSource",
    "(all)": "All networks",
}
BASIS_LABELS = {
    "ga4": "GA4 first touch",
    "network_installs": "network installs x cohort ARPU (modeled)",
    "mixed": "mixed bases",
}


def q(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict]:
    try:
        cur = con.execute(sql, params or [])
    except duckdb.CatalogException:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _net(n: str | None) -> str:
    return NETWORK_LABELS.get(n or "", n or "–")


def _payback(day: Any, observed: Any) -> str:
    if day is not None:
        return f"day {day}"
    return f"not within {observed} days" if observed is not None else "–"


def _pred_payback(con: duckdb.DuckDBPyConnection, app_id: str, cpi: float | None) -> str:
    """First day the predicted ARPU curve (pltv) reaches the CPI, if a model exists."""
    if not cpi:
        return "–"
    rows = q(
        con,
        "SELECT day_n, predicted_arpu_usd FROM fct_pltv_curve WHERE app_id = ? ORDER BY day_n",
        [app_id],
    )
    for r in rows:
        if r["predicted_arpu_usd"] is not None and r["predicted_arpu_usd"] >= cpi:
            return f"day {r['day_n']} (model)"
    return f"beyond day {rows[-1]['day_n']} (model)" if rows else "–"


def ua_page(con: duckdb.DuckDBPyConnection, apps: list[dict[str, Any]]) -> tuple[str, str]:
    summ = q(con, "SELECT * FROM fct_ua_summary ORDER BY app_id")
    intro = (
        '<p class="note">Spend, installs and impressions are what each network reports. ROAS uses '
        "one of two bases, set per network in the config: <strong>GA4 first touch</strong> (users "
        "whose GA4 first-touch campaign is the network's campaign, and their GA4 purchase and ad "
        "revenue) or <strong>network installs x cohort ARPU</strong>, a model that assumes bought "
        "users monetise like the average new user of the same day (no MMP, so iOS and most "
        "non-Google networks cannot be attributed per user). Payback is the first day the pooled "
        "ROAS curve reaches 100%; formulas in docs/METRICS.md.</p>"
    )
    if not summ:
        return "UA", intro + (
            '<p class="note">No ad-network spend loaded yet. Add a <code>meta_ads</code>, '
            "<code>google_ads</code>, <code>tiktok_ads</code> or <code>ua_csv</code> source to an "
            "app (pulse.example.yaml).</p>"
        )
    names = {a["app_id"]: a["name"] for a in apps}
    parts = [intro]
    for app_id in sorted(
        {r["app_id"] for r in summ}, key=lambda a: list(names).index(a) if a in names else 99
    ):
        rows = [r for r in summ if r["app_id"] == app_id]
        top = next(r for r in rows if r["network"] == "(all)")
        tiles = [
            tile("Spend (30 days)", fmt_usd(top["spend_30d_usd"])),
            tile(
                "Installs (30 days)",
                fmt_compact(top["installs_30d"]),
                title="On each network's attribution basis",
            ),
            tile("CPI (30 days)", fmt_usd(top["cpi_30d_usd"])),
            tile(
                "D7 ROAS",
                fmt_pct(top["roas_d7"], 0),
                title=BASIS_LABELS.get(top["attribution"], ""),
            ),
            tile("D30 ROAS", fmt_pct(top["roas_d30"], 0)),
            tile("Payback (observed)", _payback(top["payback_day"], top["curve_days"])),
            tile(
                "Payback (predicted)",
                _pred_payback(con, app_id, top["cpi_30d_usd"]),
                title="First day the pLTV model's predicted cumulative ARPU reaches the CPI",
            ),
        ]
        daily = q(
            con,
            "SELECT date, network, SUM(spend_usd) AS spend FROM fct_ua_daily WHERE app_id = ? "
            "GROUP BY 1, 2 ORDER BY 1",
            [app_id],
        )
        dates = sorted({r["date"] for r in daily})
        idx = {d: i for i, d in enumerate(dates)}
        nets = sorted({r["network"] for r in daily})
        spend_series = []
        for slot, n in enumerate(nets, start=1):
            vals: list[float | None] = [None] * len(dates)
            for r in daily:
                if r["network"] == n:
                    vals[idx[r["date"]]] = r["spend"]
            spend_series.append(Series(_net(n), vals, slot=min(slot, 8)))
        curve = q(
            con,
            "SELECT network, day_n, roas, blended_roas FROM fct_ua_roas_curve "
            "WHERE app_id = ? AND campaign_key = '(all)' ORDER BY day_n",
            [app_id],
        )
        cdays = sorted({r["day_n"] for r in curve})
        cidx = {d: i for i, d in enumerate(cdays)}
        roas_series = []
        for slot, n in enumerate(
            ["(all)", *sorted({r["network"] for r in curve} - {"(all)"})], start=1
        ):
            vals = [None] * len(cdays)
            for r in curve:
                if r["network"] == n:
                    vals[cidx[r["day_n"]]] = r["roas"]
            roas_series.append(Series(_net(n), vals, slot=min(slot, 8)))
        blended = [None] * len(cdays)
        for r in curve:
            if r["network"] == "(all)":
                blended[cidx[r["day_n"]]] = r["blended_roas"]
        roas_series.append(Series("Blended (all new users)", blended, dashed=True, slot=8))
        charts = (
            '<div class="grid2">'
            + line_chart(
                "Spend per day by network",
                dates,
                spend_series,
                fmt_usd,
                "USD, converted with the configured FX rates",
            )
            + (
                line_chart(
                    "ROAS by days since install",
                    [f"D{d}" for d in cdays],
                    roas_series,
                    fmt_pct,
                    "Pooled over the cohorts complete at each day; 100% = payback",
                    x_fmt=str,
                    x_name="Day",
                )
                if cdays
                else empty_chart("ROAS by days since install", "Fewer than 7 complete cohorts.")
            )
            + "</div>"
        )
        table = data_table(
            [
                "Network",
                "Campaign",
                "Basis",
                "Spend 30d",
                "Installs 30d",
                "CPI",
                "CTR",
                "IPM",
                "D0 ROAS",
                "D7 ROAS",
                "D30 ROAS",
                "Payback",
            ],
            [
                [
                    _net(r["network"]),
                    "" if r["campaign_key"] == "(all)" else escape(str(r["campaign_name"])),
                    BASIS_LABELS.get(r["attribution"], r["attribution"] or "–"),
                    fmt_usd(r["spend_30d_usd"]),
                    fmt_int(r["installs_30d"]),
                    fmt_usd(r["cpi_30d_usd"]),
                    fmt_pct(r["ctr_30d"], 2),
                    fmt_num(r["ipm_30d"]),
                    fmt_pct(r["roas_d0"], 0),
                    fmt_pct(r["roas_d7"], 0),
                    fmt_pct(r["roas_d30"], 0),
                    _payback(r["payback_day"], r["curve_days"]),
                ]
                for r in rows
            ],
            left_cols=(0, 1, 2),
        )
        cards = q(
            con,
            "SELECT * FROM fct_creative_scorecard WHERE app_id = ? AND network = '(all)' "
            "ORDER BY status, ipm_rank NULLS LAST, impressions DESC",
            [app_id],
        )
        min_imps = cards[0]["min_impressions"] if cards else 5000
        creative_table = data_table(
            [
                "Rank",
                "Creative",
                "Concept",
                "Impressions",
                "Installs",
                "IPM (95% CI)",
                "CTR",
                "CPI",
                "Spend",
                "D7 ROAS (modeled)",
                "Status",
            ],
            [
                [
                    str(r["ipm_rank"]) if r["ipm_rank"] else "–",
                    escape(r["creative_id"] or r["creative_key"]),
                    escape(r["creative_concept"] or "–"),
                    fmt_int(r["impressions"]),
                    fmt_int(r["network_installs"]),
                    f"{fmt_num(r['ipm'])} ({fmt_num(r['ipm_low'])}–{fmt_num(r['ipm_high'])})"
                    if r["ipm"] is not None
                    else "–",
                    fmt_pct(r["ctr"], 2),
                    fmt_usd(r["cpi_usd"]),
                    fmt_usd(r["spend_usd"]),
                    fmt_pct(r["d7_roas"], 0),
                    r["status"],
                ]
                for r in cards
            ],
            left_cols=(1, 2, 10),
        )
        parts.append(
            f"<h2>{escape(names.get(app_id, app_id))}</h2>"
            f'<div class="tiles">{"".join(tiles)}</div>'
            + charts
            + "<h3>Networks and campaigns</h3>"
            + table
            + "<h3>Creative scorecard</h3>"
            + f'<p class="note">One row per creative across networks (creative-forge ids parsed '
            f"from ad names; other ads by network ad id). Ranked by installs per 1,000 impressions "
            f"only with at least {fmt_int(min_imps)} impressions; below that a creative is "
            f"<em>insufficient data</em>, never ranked.</p>" + creative_table
        )
    return "UA", "".join(parts)


def experiments_page(con: duckdb.DuckDBPyConnection, apps: list[dict[str, Any]]) -> tuple[str, str]:
    from ..stats.experiments import report_html

    return "Experiments", report_html(con, apps)
