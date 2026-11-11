"""ROAS, payback and creative-scorecard models on a hand-computed scenario.

One game app, ten install cohorts (2026-09-01 .. 09-10) of 100 users each.
Every cohort earns $10 on day 0 and $2 on each of days 1..40, so its
cumulative revenue is 10 + 2N and its cumulative ARPU (10 + 2N) / 100.

Three networks buy installs every one of those ten days:
  meta      network_installs basis: $40/day, 20 installs (two ads)
            revenue(N) = 20 x (10 + 2N) / 100 = 2 + 0.4N  -> ROAS 0.05 / 0.12 / 0.35
  applovin  network_installs basis: $5/day, 10 installs
            revenue(N) = 1 + 0.2N  -> ROAS(N) = (1 + 0.2N) / 5, payback day 20
  google    GA4 first-touch basis: $20/day, the network claims 25 installs,
            GA4 attributes 30 users earning 4 on day 0 and 1 on days 1..40
            revenue(N) = 4 + N  -> ROAS 0.2 / 0.55 / 1.7, payback day 16
All networks: revenue 7 + 1.6N on $65/day -> payback day 37 (7 + 1.6 x 37 = 66.2).
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from studio_pulse import runner, warehouse

D = dt.date(2026, 9, 1)
DAYS = 10
CF_A = "cf1.puzzle-ads.dust-blower.main.main.main.9x16.en.6453fdb366"
CF_B = "cf1.puzzle-ads.dust-blower.blower.lvl21.fill.9x16.tr.91c97d00dc"


def build(con) -> None:
    cohort_rows, campaign_rows, spend = [], [], []
    for c in range(DAYS):
        cd = D + dt.timedelta(days=c)
        for n in range(41):
            cohort_rows.append(
                {
                    "platform": "ANDROID",
                    "cohort_date": cd,
                    "day_n": n,
                    "users": 100 if n == 0 else 50,
                    "sessions": 0,
                    "iap_revenue_usd": 6.0 if n == 0 else 1.0,
                    "ad_revenue_usd": 4.0 if n == 0 else 1.0,
                    "new_payers": 0,
                }
            )
        for n in range(41):
            campaign_rows.append(
                {
                    "platform": "ANDROID",
                    "cohort_date": cd,
                    "day_n": n,
                    "ts_source": "google",
                    "ts_medium": "cpc",
                    "ts_campaign": "uac_tier1 ",
                    "users": 30 if n == 0 else 10,
                    "iap_revenue_usd": 3.0 if n == 0 else 0.5,
                    "ad_revenue_usd": 1.0 if n == 0 else 0.5,
                }
            )
        base = {"date": cd, "currency": "USD", "platform": "ANDROID", "country": "US"}
        spend += [
            {
                **base,
                "network": "meta",
                "campaign_id": "m-c1",
                "campaign_name": "Meta_AEO",
                "ad_id": "m1",
                "ad_name": CF_A,
                "creative_id": "6453fdb366",
                "spend": 30.0,
                "impressions": 10000,
                "clicks": 100,
                "network_installs": 15.0,
            },
            {
                **base,
                "network": "meta",
                "campaign_id": "m-c1",
                "campaign_name": "Meta_AEO",
                "ad_id": "m2",
                "ad_name": "Legacy ad",
                "creative_id": None,
                "spend": 10.0,
                "impressions": 400,
                "clicks": 8,
                "network_installs": 5.0,
            },
            {
                **base,
                "network": "applovin",
                "campaign_id": "al1",
                "campaign_name": "AL_Puzzle",
                "ad_id": "cs1",
                "ad_name": CF_A,
                "creative_id": "6453fdb366",
                "spend": 5.0,
                "impressions": 2000,
                "clicks": 20,
                "network_installs": 10.0,
            },
            {
                **base,
                "network": "google_ads",
                "campaign_id": "111",
                "campaign_name": "UAC_Tier1",
                "ad_id": "333",
                "ad_name": CF_B,
                "creative_id": "91c97d00dc",
                "spend": 20.0,
                "impressions": 5000,
                "clicks": 60,
                "network_installs": 25.0,
            },
        ]
    warehouse.replace_slice(con, "raw_ga4_cohort_days", cohort_rows, app_id="g", source="ga4")
    warehouse.replace_slice(
        con, "raw_ga4_campaign_cohorts", campaign_rows, app_id="g", source="ga4"
    )
    warehouse.replace_slice(con, "raw_ua_spend", spend, app_id="g", source="ua")
    warehouse.upsert_ua_networks(
        con,
        [
            ("g", "meta", "network_installs", []),
            ("g", "applovin", "network_installs", []),
            ("g", "google_ads", "ga4", ["google"]),
        ],
    )
    warehouse.load_fx_rates(con, {}, dt.date(2000, 1, 1))
    runner.run_models(con)


@pytest.fixture()
def ua(con):
    build(con)
    return con


def one(con, sql, params=()):
    cur = con.execute(sql, list(params))
    cols = [c[0] for c in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row, strict=False)) if row else None


def test_cohort_ltv_and_curve_agree(ua):
    r = one(ua, "SELECT * FROM fct_cohort_ltv WHERE cohort_date = ? AND day_n = 7", [D])
    assert (r["cohort_size"], r["cum_revenue_usd"]) == (100, 24.0)
    curve = one(ua, "SELECT * FROM fct_ltv_curve WHERE day_n = 30")
    assert curve["cohorts"] == 10 and curve["cumulative_arpu_usd"] == pytest.approx(0.70)


def test_daily_delivery_metrics(ua):
    r = one(ua, "SELECT * FROM fct_ua_daily WHERE network = 'meta' AND date = ?", [D])
    assert r["spend_usd"] == 40.0 and r["network_installs"] == 20.0
    assert r["cpi_usd"] == pytest.approx(2.0)
    assert r["ctr"] == pytest.approx(108 / 10400)
    assert r["ipm"] == pytest.approx(20 / 10400 * 1000)
    assert r["spend_share"] == pytest.approx(40 / 65)


def test_cohort_roas_per_basis(ua):
    m = one(ua, "SELECT * FROM fct_ua_cohort_roas WHERE network = 'meta' AND cohort_date = ?", [D])
    assert m["attribution"] == "network_installs" and m["cpi_usd"] == pytest.approx(2.0)
    assert (m["roas_d0"], m["roas_d7"], m["roas_d30"]) == pytest.approx((0.05, 0.12, 0.35))
    g = one(
        ua, "SELECT * FROM fct_ua_cohort_roas WHERE network = 'google_ads' AND cohort_date = ?", [D]
    )
    assert g["attribution"] == "ga4" and g["installs"] == 30 and g["network_installs"] == 25
    assert g["cpi_usd"] == pytest.approx(20 / 30)
    assert (g["revenue_d0_usd"], g["revenue_d7_usd"], g["revenue_d30_usd"]) == pytest.approx(
        (4.0, 11.0, 34.0)
    )
    assert (g["roas_d0"], g["roas_d7"], g["roas_d30"]) == pytest.approx((0.2, 0.55, 1.7))


def test_roas_curve_and_payback_day(ua):
    s = {
        (r[0], r[1]): r[2:]
        for r in ua.execute(
            "SELECT network, campaign_key, roas_d7, roas_d30, payback_day, attribution, "
            "blended_roas_d7, cpi_30d_usd, network_cpi_30d_usd, spend_30d_usd FROM fct_ua_summary"
        ).fetchall()
    }
    assert s[("applovin", "(all)")][2] == 20  # (1 + 0.2 x 20) / 5 = 1.0 exactly
    assert s[("google_ads", "(all)")][2] == 16  # (4 + 16) / 20 = 1.0
    assert s[("meta", "(all)")][2] is None  # 0.45 at day 40: not paid back
    assert s[("meta", "m-c1")][:2] == pytest.approx((0.12, 0.35))
    app = s[("(all)", "(all)")]
    assert app[2] == 37 and app[3] == "mixed"
    assert app[0] == pytest.approx((7 + 1.6 * 7) / 65)
    assert app[1] == pytest.approx((7 + 1.6 * 30) / 65)
    assert app[4] == pytest.approx(24 / 65)  # all users of the cohorts / spend
    # installs on each network's basis: meta 200 + applovin 100 network installs + 300 GA4 users
    assert app[5] == pytest.approx(650 / 600) and app[6] == pytest.approx(650 / 550)
    assert app[7] == 650
    # day 36 is short of payback, day 37 reaches it
    r36, r37 = ua.execute(
        "SELECT roas FROM fct_ua_roas_curve WHERE network = '(all)' AND day_n IN (36, 37) "
        "ORDER BY day_n"
    ).fetchall()
    assert r36[0] < 1 <= r37[0]


def test_curve_drops_points_with_fewer_than_seven_cohorts(ua):
    days = [
        r[0]
        for r in ua.execute(
            "SELECT day_n FROM fct_ua_roas_curve WHERE network = 'meta' AND campaign_key = '(all)' "
            "ORDER BY day_n"
        ).fetchall()
    ]
    # cohort 09-10 reaches day 40; 09-01 reaches day 49; day 43 still has 7 cohorts
    assert days[0] == 0 and days[-1] == 43


def test_creative_scorecard(ua):
    rows = {
        (r["network"], r["creative_key"]): r
        for r in (
            dict(zip([c[0] for c in cur.description], x, strict=False))
            for cur in [ua.execute("SELECT * FROM fct_creative_scorecard")]
            for x in cur.fetchall()
        )
    }
    a = rows[("(all)", "6453fdb366")]
    assert (a["impressions"], a["network_installs"], a["spend_usd"]) == (120000, 250.0, 350.0)
    assert a["ipm"] == pytest.approx(250 / 120000 * 1000)
    assert a["ctr"] == pytest.approx(1200 / 120000) and a["cpi_usd"] == pytest.approx(1.4)
    # D7 revenue: (15 + 10) installs x 0.24 D7 ARPU x 10 days = 60 on $350
    assert a["d7_roas"] == pytest.approx(60 / 350)
    b = rows[("(all)", "91c97d00dc")]
    assert b["ipm"] == pytest.approx(5.0) and b["ipm_rank"] == 1 and a["ipm_rank"] == 2
    assert b["d7_roas"] == pytest.approx(60 / 200)
    # Wilson 95% interval for 250 installs in 50,000 impressions, computed by hand
    z, n, p = 1.96, 50000, 250 / 50000
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    assert b["ipm_low"] == pytest.approx(1000 * (centre - half))
    assert b["ipm_high"] == pytest.approx(1000 * (centre + half))
    legacy = rows[("(all)", "meta:m2")]
    assert legacy["impressions"] == 4000 and legacy["status"] == "insufficient data"
    assert legacy["ipm_rank"] is None and legacy["creative_id"] is None
    assert rows[("meta", "6453fdb366")]["ipm_rank"] == 1


def test_ga4_campaign_cohorts_query(con, tmp_path):
    """First-touch campaigns from the GA4 export, organic traffic left out."""
    from conftest import D0, GA4Builder

    from studio_pulse.connectors import ga4
    from studio_pulse.connectors.dialect import DuckDBDialect
    from studio_pulse.connectors.executors import DuckDBExecutor

    paid, organic = ("google", "cpc", "UAC_Tier1"), ("google-play", "organic", "(organic)")
    b = GA4Builder()
    for u in ("p1", "p2"):
        b.add(0, "first_open", u, traffic=paid).add(
            0, "session_start", u, ga_session_id=1, traffic=paid
        )
    b.add(0, "in_app_purchase", "p1", value_usd=2.5, traffic=paid)
    b.add(2, "session_start", "p1", ga_session_id=2, traffic=paid)
    b.add(2, "ad_impression", "p1", value_usd=0.25, value=0.25, traffic=paid)
    b.add(0, "first_open", "o1", traffic=organic).add(
        0, "session_start", "o1", ga_session_id=3, traffic=organic
    )
    ex = DuckDBExecutor({"ga4_events": b.write(tmp_path / "e.parquet")})
    ga4.ingest(
        con,
        app_id="g",
        source="ga4_files",
        dialect=DuckDBDialect(),
        executor=ex,
        start=D0,
        end=D0 + dt.timedelta(days=3),
        opts=ga4.GA4Options(queries=["campaign_cohorts"]),
        log=lambda *_: None,
    )
    got = con.execute(
        "SELECT day_n, ts_source, ts_medium, ts_campaign, users, iap_revenue_usd, ad_revenue_usd "
        "FROM raw_ga4_campaign_cohorts ORDER BY day_n"
    ).fetchall()
    assert got == [
        (0, "google", "cpc", "UAC_Tier1", 2, 2.5, 0.0),
        (2, "google", "cpc", "UAC_Tier1", 1, 0.0, 0.25),
    ]
