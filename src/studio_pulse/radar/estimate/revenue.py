"""Top-grossing rank -> daily iOS revenue band per country, from public figures only.

INACTIVE BY DEFAULT: no compliant source publishes a top-grossing rank (apps.apple.com has no
grossing chart; the legacy iTunes RSS is disallowed by robots.txt, and its grossing ranks are
never used here). The model runs only when a compliant grossing list reaches
stg_mr_rank_input (raw_mr_rank_extra); the default revenue estimate is rpd.py.

    R_c(r) = A_us * m_c * r^(-b)                          (revenue_anchors.yaml has the sources)

- b: uniform over the published range of rank-revenue exponents (Garg & Telang 2013 and
  later fits), not fitted: three anchors cannot pin a slope.
- A_us: each US anchor (a game with a disclosed quarterly revenue) gives daily revenue x
  its observed rank^b; every undisclosed step (store share, App Store share, US share) is a
  range drawn uniformly in log space. A_us is their geometric mean, plus the uncertainty of
  that mean (sd / sqrt(n)).
- m_c: country c's iOS game spend / the US's (market sizes and iOS-share ranges).
- the band adds the anchors' scatter around the curve (at least `sigma_floor`), because two
  games at the same rank do not earn the same.
A country without a public market size gets status `no_source` and no number.

Checks: leave one US anchor out (predict it from the others), the KR anchor held out
entirely, and the market check (the curve's implied top-1000 total vs the public market).
With three to four anchors these are sanity checks, not an accuracy estimate.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import duckdb
import yaml

from .settings import EstimateSettings
from .stats import log_uniform, quantile

GROSSING = "grossing"  # list_kind of the rank input whose ranks measure revenue


def load_anchors() -> dict[str, Any]:
    text = (
        resources.files("studio_pulse.radar.estimate")
        .joinpath("revenue_anchors.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def _usd(amount: float, currency: str, fx: dict[str, float], period: str = "2025") -> float:
    if currency == "USD":
        return amount
    if currency == "EUR":
        return amount * fx["usd_per_eur_2025"]
    if currency == "GBP":
        return amount * fx["usd_per_gbp_2025"]
    if currency == "KRW":
        return amount / fx["krw_per_usd_2q26"]
    raise ValueError(f"no exchange rate for {currency}")


def _range(rng: random.Random, v: Any) -> float:
    if isinstance(v, (list, tuple)):
        return log_uniform(rng, float(v[0]), float(v[1]))
    return float(v)


def market_draw(doc: dict[str, Any], cc: str, rng: random.Random) -> float | None:
    """One draw of country cc's yearly iOS game spend in USD (None: no public figure)."""
    m = (doc.get("market_size") or {}).get(cc)
    if not m:
        return None
    amount = _range(rng, m["amount"])
    usd = _usd(amount, m["currency"], doc["fx"])
    return usd * _range(rng, m["ios_share"]) * _range(rng, m.get("gross_up", 1.0))


def anchor_days(a: dict[str, Any]) -> int:
    p = a["period"]
    return (dt.date.fromisoformat(p["end"]) - dt.date.fromisoformat(p["start"])).days + 1


def anchor_daily_draw(doc: dict[str, Any], a: dict[str, Any], rng: random.Random) -> float:
    v = _usd(float(a["amount"]), a["currency"], doc["fx"]) / anchor_days(a)
    for f in a.get("factors") or []:
        v *= _range(rng, f["range"])
    return v


def anchor_daily_mid(doc: dict[str, Any], a: dict[str, Any]) -> float:
    """The anchor's own central value: every range at its geometric middle."""
    v = _usd(float(a["amount"]), a["currency"], doc["fx"]) / anchor_days(a)
    for f in a.get("factors") or []:
        lo, hi = f["range"]
        v *= math.sqrt(float(lo) * float(hi))
    return v


def anchor_ranks(con: duckdb.DuckDBPyConnection | None, a: dict[str, Any]) -> list[int]:
    ranks = [int(r) for r in (a.get("ranks") or {}).values() if r]
    if ranks or con is None:
        return ranks
    ids = a["app_id"] if isinstance(a["app_id"], list) else [a["app_id"]]
    try:
        rows = con.execute(
            "SELECT MIN(rank) FROM stg_mr_rank_input WHERE country = ? AND list_kind = ? "
            "AND all_games AND source <> 'apple_rss' AND list_contains(?::VARCHAR[], app_id) "
            "GROUP BY snapshot_date "
            "ORDER BY snapshot_date",
            [a["country"], GROSSING, ids],
        ).fetchall()
    except duckdb.Error:
        return []
    return [int(r[0]) for r in rows]


@dataclass
class Draw:
    b: float
    log_a_us: float  # includes the uncertainty of the anchors' mean
    sigma: float  # game-to-game spread (natural log)
    log_us_market: float


@dataclass
class RevenueModel:
    draws: list[Draw]
    anchors: list[str]  # calibration anchors used
    slope: tuple[float, float] = (0.6, 0.95)
    country_markets: dict[str, list[float]] = field(default_factory=dict)  # log iOS spend/draw

    def country_draws(self, cc: str, rank: int, rng: random.Random) -> list[float] | None:
        logm = self.country_markets.get(cc)
        if logm is None:
            return None
        lr = math.log(rank)
        return [
            d.log_a_us + (lm - d.log_us_market) - d.b * lr + rng.gauss(0.0, d.sigma)
            for d, lm in zip(self.draws, logm, strict=True)
        ]


def build(
    doc: dict[str, Any],
    anchors: list[dict[str, Any]],
    ranks: dict[str, list[int]],
    countries: list[str],
    es: EstimateSettings,
    rng: random.Random,
) -> RevenueModel | None:
    cal = [a for a in anchors if a.get("country") == "us" and ranks.get(a["id"])]
    if len(cal) < 2:
        return None
    lo, hi = float(doc["slope"]["low"]), float(doc["slope"]["high"])
    floor = float(doc.get("sigma_floor", 0.5))
    draws: list[Draw] = []
    markets: dict[str, list[float]] = {cc: [] for cc in countries}
    for _ in range(es.draws):
        b = rng.uniform(lo, hi)
        impl = [
            math.log(anchor_daily_draw(doc, a, rng)) + b * math.log(rng.choice(ranks[a["id"]]))
            for a in cal
        ]
        n = len(impl)
        mean = sum(impl) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in impl) / (n - 1)) if n > 1 else 0.0
        sigma = max(sd, floor)
        us = market_draw(doc, "us", rng)
        if us is None:
            return None
        draws.append(Draw(b, mean + rng.gauss(0.0, sigma / math.sqrt(n)), sigma, math.log(us)))
        for cc in countries:
            m = market_draw(doc, cc, rng)
            if m is not None:
                markets[cc].append(math.log(m))
    model = RevenueModel(draws, [a["id"] for a in cal], (lo, hi))
    model.country_markets = {cc: v for cc, v in markets.items() if len(v) == len(draws)}
    return model


def _band(vals: list[float], es: EstimateSettings) -> tuple[float, float, float]:
    return (
        math.exp(quantile(vals, es.band_lo)),
        math.exp(quantile(vals, 0.5)),
        math.exp(quantile(vals, es.band_hi)),
    )


def validate(
    doc: dict[str, Any],
    anchors: list[dict[str, Any]],
    ranks: dict[str, list[int]],
    countries: list[str],
    es: EstimateSettings,
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Out-of-sample checks; each row: scheme, country, n, mape, median_abs_log, coverage."""
    rows: list[dict[str, Any]] = []
    us = [a for a in anchors if a.get("country") == "us" and ranks.get(a["id"])]
    small = EstimateSettings(**{**es.__dict__, "draws": max(400, es.draws // 4)})
    ape, alog, inside, detail = [], [], 0, []
    for held in us:
        rest = [a for a in us if a is not held]
        m = build(doc, rest, ranks, ["us"], small, rng)
        if m is None:
            continue
        r = round(math.exp(sum(math.log(x) for x in ranks[held["id"]]) / len(ranks[held["id"]])))
        lo, mid, hi = _band(m.country_draws("us", r, rng) or [], es)
        actual = anchor_daily_mid(doc, held)
        ape.append(abs(mid - actual) / actual)
        alog.append(abs(math.log(mid / actual)))
        inside += lo <= actual <= hi
        detail.append(f"{held['id']}: rank {r}, band {lo:,.0f}-{hi:,.0f}, disclosed {actual:,.0f}")
    if ape:
        rows.append(
            {
                "scheme": "loo_anchor",
                "country": "us",
                "n": len(ape),
                "mape": quantile(ape, 0.5),
                "median_abs_log": quantile(alog, 0.5),
                "coverage": inside / len(ape),
                "note": "; ".join(detail),
            }
        )
    full = build(doc, anchors, ranks, sorted(set(countries) | {"us"}), es, rng)
    if full is None:
        return rows
    for a in anchors:
        if a.get("role") != "validate" or not ranks.get(a["id"]):
            continue
        cc = a["country"]
        r = min(ranks[a["id"]])
        vals = full.country_draws(cc, r, rng)
        if vals is None:
            continue
        lo, mid, hi = _band(vals, es)
        actual = anchor_daily_mid(doc, a)
        rows.append(
            {
                "scheme": "holdout_anchor",
                "country": cc,
                "n": 1,
                "mape": abs(mid - actual) / actual,
                "median_abs_log": abs(math.log(mid / actual)),
                "coverage": float(lo <= actual <= hi),
                "note": f"{a['id']}: rank {r}, band {lo:,.0f}-{hi:,.0f}, disclosed {actual:,.0f}",
            }
        )
    # the curve summed over ranks 1..1000 vs the public US iOS market (per day)
    tot = []
    for d in full.draws:
        harm = sum(k ** (-d.b) for k in range(1, 1001))
        tot.append(d.log_a_us + math.log(harm) - (d.log_us_market - math.log(365)))
    ratio_lo, ratio_mid, ratio_hi = _band(tot, es)
    rows.append(
        {
            "scheme": "market_sum",
            "country": "us",
            "n": 1,
            "mape": abs(ratio_mid - 1.0),
            "median_abs_log": abs(math.log(ratio_mid)),
            "coverage": float(ratio_lo <= 1.0 <= ratio_hi),
            "note": (
                f"top-1000 sum / public US iOS game spend per day: {ratio_mid:.2f} "
                f"({ratio_lo:.2f}-{ratio_hi:.2f}); above 1 means the curve is too high"
            ),
        }
    )
    return rows


def game_bands(
    con: duckdb.DuckDBPyConnection,
    day: dt.date,
    model: RevenueModel,
    es: EstimateSettings,
    rng: random.Random,
) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT app_id, country, list_key, rank FROM stg_mr_rank_input WHERE snapshot_date = ? "
        "AND list_kind = ? AND all_games AND source <> 'apple_rss' ORDER BY country, rank",
        [day, GROSSING],
    ).fetchall()
    out = []
    for app_id, cc, lk, rank in rows:
        vals = model.country_draws(cc, rank, rng)
        if vals is None:
            continue
        lo, mid, hi = _band(vals, es)
        out.append(
            {
                "snapshot_date": day,
                "app_id": app_id,
                "play_id": None,
                "country": cc,
                "list_key": lk,
                "kind": "ios_revenue",
                "rank": rank,
                "low": lo,
                "mid": mid,
                "high": hi,
                "unit": "USD/day",
                "method": (
                    f"public anchors ({len(model.anchors)} US) x rank^-b, b uniform "
                    f"{model.slope[0]:.2f}-{model.slope[1]:.2f} (published fits), "
                    f"{cc.upper()} market size; {es.band_pct}% band"
                ),
                "n": len(model.anchors),
                "basis": "estimate",
            }
        )
    return out
