"""Chart rank -> daily installs, calibrated on the studio's own Google Play observations.

Ground truth. Google Play's details page carries `realInstalls`, one worldwide counter per
package. It is refreshed irregularly: between 13 and 14 Nov 2026 (29 hours) 18 of 30
packages the radar read kept exactly the same count, 13 of them while their Play rating count
grew (one gained 644 ratings and 0 installs), and four of those that changed moved by only
26-87 installs, two of them while gaining 1,700-2,000 ratings. A one-day difference therefore measures the refresh, not
the installs. The radar only uses windows between two observed refreshes (the counter
changed) at least `min_window_days` apart, and drops a window whose Play rating count grew
by more than `max_ratings_per_install` per new install (a counter that lagged the ratings).

Model. For every Apple top-free list (country x genre) with enough games:
    log(installs/day worldwide on Android) = a - b * log(mean rank over the window)
fitted by least squares with a cluster bootstrap over games. The prediction band carries
both the curve's uncertainty and the scatter of games around it. The target is the
game's worldwide Google Play installs (the only count Play publishes), so a list's curve
says "games at this rank in this country's iPhone list get this many Android installs a day
worldwide". iOS downloads are that times the iOS:Android download ratio of the countries
where the game charts today (ios_android.yaml), a public-data conversion that the studio's
own data cannot check.

Accuracy: leave-one-game-out and a time split (fit on earlier windows, predict later ones),
reported as MAPE of the band's middle, median |log error| and the share of held-out windows
inside the band. A list below `min_games` games gets status `insufficient` and no number.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import duckdb
import yaml

from .settings import EstimateSettings
from .stats import Point, PowerLawFit, fit_power_law, log_uniform, quantile

FREE = "free"  # list_kind of the rank input whose ranks measure downloads


# -- iOS : Android ----------------------------------------------------------------------------


def load_platform_split() -> dict[str, Any]:
    text = (
        resources.files("studio_pulse.radar.estimate")
        .joinpath("ios_android.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def ios_share_range(split: dict[str, Any], cc: str) -> tuple[float, float] | None:
    """(low, high) share of game downloads on the App Store in `cc`, or None if unknown."""
    c = (split.get("countries") or {}).get(cc)
    if not c:
        return None
    if c.get("download_ios"):
        lo, hi = c["download_ios"]
        return float(lo), float(hi)
    usage = [float(v) / 100 for v in (c.get("usage_ios") or {}).values() if v is not None]
    if not usage:
        return None
    k_lo, k_hi = (float(x) for x in split["k_range"])
    cap = float(split.get("max_share", 0.9))
    return min(usage) * k_lo, min(max(usage) * k_hi, cap)


def ratio_range(split: dict[str, Any], countries: Iterable[str]) -> tuple[float, float] | None:
    """iOS downloads per Android download: the extremes over `countries` (None if none known)."""
    rs = [ios_share_range(split, cc) for cc in countries]
    rs = [r for r in rs if r]
    if not rs:
        return None
    lo = min(r[0] for r in rs)
    hi = max(r[1] for r in rs)
    return lo / (1 - lo), hi / (1 - hi)


# -- ground truth: Play counter windows ---------------------------------------------------------


@dataclass
class Obs:
    day: dt.date
    ts: dt.datetime
    installs: int
    ratings: int | None


@dataclass
class Window:
    play_id: str
    t0: dt.datetime
    t1: dt.datetime
    installs0: int
    installs1: int
    ratings0: int | None
    ratings1: int | None
    status: str
    apple_ids: list[str] = field(default_factory=list)

    @property
    def days(self) -> float:
        return (self.t1 - self.t0).total_seconds() / 86400

    @property
    def rate(self) -> float | None:
        if self.days <= 0 or self.installs1 <= self.installs0:
            return None
        return (self.installs1 - self.installs0) / self.days


def play_series(con: duckdb.DuckDBPyConnection) -> dict[str, list[Obs]]:
    rows = con.execute(
        """
        SELECT app_id, snapshot_date, fetched_at, real_installs, rating_count
        FROM raw_mr_app
        WHERE store = 'play' AND real_installs IS NOT NULL AND fetched_at IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id, snapshot_date ORDER BY fetched_at DESC) = 1
        ORDER BY app_id, fetched_at
        """
    ).fetchall()
    out: dict[str, list[Obs]] = {}
    for pkg, day, ts, inst, rat in rows:
        out.setdefault(pkg, []).append(Obs(day, ts, int(inst), None if rat is None else int(rat)))
    return out


def counter_staleness(series: dict[str, list[Obs]]) -> dict[str, int]:
    """Consecutive reads at least 18 h apart: how many kept the same install count while the
    Play rating count grew (the counter was not refreshed)."""
    pairs = stale = 0
    for obs in series.values():
        for p, q in zip(obs, obs[1:], strict=False):
            if (q.ts - p.ts).total_seconds() < 18 * 3600:
                continue
            pairs += 1
            if q.installs == p.installs and (q.ratings or 0) > (p.ratings or 0):
                stale += 1
    return {"pairs": pairs, "stale": stale}


def refresh_windows(series: dict[str, list[Obs]], es: EstimateSettings) -> list[Window]:
    out: list[Window] = []
    for pkg, obs in sorted(series.items()):
        start: Obs | None = None
        for prev, cur in zip(obs, obs[1:], strict=False):
            if cur.installs == prev.installs:
                continue  # not refreshed (or no installs): never a window endpoint
            if start is None:
                start = cur
                continue
            days = (cur.ts - start.ts).total_seconds() / 86400
            if days < es.min_window_days:
                if cur.installs < start.installs:
                    start = cur  # a correction downwards restarts the window
                continue
            if days > es.max_window_days:
                status = "too_long"
            elif cur.installs <= start.installs:
                status = "decreased"
            elif (
                cur.ratings is not None
                and start.ratings is not None
                and cur.ratings - start.ratings
                > es.max_ratings_per_install * (cur.installs - start.installs)
            ):
                status = "implausible"
            else:
                status = "ok"
            out.append(
                Window(
                    pkg,
                    start.ts,
                    cur.ts,
                    start.installs,
                    cur.installs,
                    start.ratings,
                    cur.ratings,
                    status,
                )
            )
            start = cur
    return out


def apple_ids_for(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """Play package -> Apple ids (the radar's verified mapping, else the same bundle id)."""
    rows = con.execute(
        """
        SELECT play_id, apple_id FROM raw_mr_xref WHERE status = 'mapped' AND play_id IS NOT NULL
        UNION
        SELECT bundle_id, app_id FROM stg_mr_rank_input
        WHERE bundle_id IS NOT NULL AND app_id IS NOT NULL
        """
    ).fetchall()
    out: dict[str, list[str]] = {}
    for pkg, aid in rows:
        out.setdefault(pkg, [])
        if aid not in out[pkg]:
            out[pkg].append(aid)
    return out


@dataclass
class Exposure:
    country: str
    list_key: str
    days_observed: int
    days_listed: int
    mean_rank: float


def exposures(con: duckdb.DuckDBPyConnection, w: Window, apple_ids: list[str]) -> list[Exposure]:
    if not apple_ids:
        return []
    d0, d1 = w.t0.date(), w.t1.date()
    lists = con.execute(
        "SELECT snapshot_date, country, list_key, list_len FROM stg_mr_rank_lists "
        "WHERE list_kind = ? AND snapshot_date BETWEEN ? AND ?",
        [FREE, d0, d1],
    ).fetchall()
    ranks = {
        (d, lk): r
        for d, lk, r in con.execute(
            "SELECT snapshot_date, list_key, MIN(rank) FROM stg_mr_rank_input "
            "WHERE list_kind = ? AND snapshot_date BETWEEN ? AND ? "
            "AND list_contains(?::VARCHAR[], app_id) GROUP BY 1, 2",
            [FREE, d0, d1, apple_ids],
        ).fetchall()
    }
    by: dict[tuple[str, str], list[float]] = {}
    listed: dict[tuple[str, str], int] = {}
    for d, cc, lk, ln in lists:
        r = ranks.get((d, lk))
        by.setdefault((cc, lk), []).append(math.log(r if r is not None else (ln or 100) + 1))
        listed[(cc, lk)] = listed.get((cc, lk), 0) + (r is not None)
    return [
        Exposure(cc, lk, len(v), listed[(cc, lk)], math.exp(sum(v) / len(v)))
        for (cc, lk), v in sorted(by.items())
    ]


# -- calibration, backtest, per-game bands -------------------------------------------------------


@dataclass
class Curve:
    country: str
    list_key: str
    status: str  # ok | insufficient
    n_games: int
    n_obs: int
    fit: PowerLawFit | None = None
    note: str = ""


def points_by_list(
    con: duckdb.DuckDBPyConnection, windows: list[Window], es: EstimateSettings
) -> tuple[dict[str, list[tuple[Point, Window]]], list[tuple[Window, Exposure]]]:
    ids = apple_ids_for(con)
    out: dict[str, list[tuple[Point, Window]]] = {}
    exp_rows: list[tuple[Window, Exposure]] = []
    for w in windows:
        w.apple_ids = ids.get(w.play_id, [])
        if w.status != "ok":
            continue
        for e in exposures(con, w, w.apple_ids):
            exp_rows.append((w, e))
            if e.days_observed and e.days_listed / e.days_observed >= es.min_listed_share:
                out.setdefault(e.list_key, []).append((Point(w.play_id, e.mean_rank, w.rate), w))
    return out, exp_rows


def calibrate(
    pts: dict[str, list[tuple[Point, Window]]],
    lists: Iterable[str],
    es: EstimateSettings,
    rng: random.Random,
) -> dict[str, Curve]:
    curves: dict[str, Curve] = {}
    for lk in sorted(set(lists) | set(pts)):
        cc = lk.split(":")[0]
        p = [x for x, _ in pts.get(lk, [])]
        n_games = len({x.group for x in p})
        c = Curve(cc, lk, "insufficient", n_games, len(p))
        ranks = [x.rank for x in p]
        if n_games < es.min_games:
            c.note = f"n={n_games} games < {es.min_games}"
        elif max(ranks) / min(ranks) < 2:
            c.note = "ranks too close together to fit a slope"
        else:
            c.fit = fit_power_law(p, boot=es.bootstrap, rng=rng)
            c.status = "ok"
        curves[lk] = c
    return curves


def band(draws: list[float], es: EstimateSettings) -> tuple[float, float, float]:
    return (
        math.exp(quantile(draws, es.band_lo)),
        math.exp(quantile(draws, 0.5)),
        math.exp(quantile(draws, es.band_hi)),
    )


@dataclass
class Score:
    n: int = 0
    inside: int = 0
    ape: list[float] = field(default_factory=list)
    alog: list[float] = field(default_factory=list)

    def add(self, lo: float, mid: float, hi: float, actual: float) -> None:
        self.n += 1
        self.inside += lo <= actual <= hi
        self.ape.append(abs(mid - actual) / actual)
        self.alog.append(abs(math.log(mid / actual)))

    def row(self) -> dict[str, Any]:
        if not self.n:
            return {"n": 0, "mape": None, "median_abs_log": None, "coverage": None}
        return {
            "n": self.n,
            "mape": quantile(self.ape, 0.5),
            "median_abs_log": quantile(self.alog, 0.5),
            "coverage": self.inside / self.n,
        }


def backtest(
    pts: list[tuple[Point, Window]], es: EstimateSettings, rng: random.Random
) -> dict[str, dict[str, Any]]:
    """Leave-one-game-out and a time split, on one list's points."""
    out: dict[str, dict[str, Any]] = {}
    boot = max(60, es.bootstrap // 4)
    games = sorted({p.group for p, _ in pts})
    s = Score()
    for g in games:
        train = [p for p, _ in pts if p.group != g]
        if len({p.group for p in train}) < max(3, es.min_games - 1):
            continue
        try:
            f = fit_power_law(train, boot=boot, rng=rng)
        except ValueError:
            continue
        for p, _ in pts:
            if p.group == g:
                s.add(*band(f.draws(p.rank, rng), es), p.y)
    out["logo"] = s.row()
    ordered = sorted(pts, key=lambda pw: pw[1].t1)
    s = Score()
    if len(ordered) >= 6:
        cut = ordered[int(len(ordered) * 0.7) - 1][1].t1
        train = [p for p, w in ordered if w.t1 <= cut]
        test = [p for p, w in ordered if w.t1 > cut]
        if len({p.group for p in train}) >= max(3, es.min_games - 1) and test:
            try:
                f = fit_power_law(train, boot=boot, rng=rng)
                for p in test:
                    s.add(*band(f.draws(p.rank, rng), es), p.y)
            except ValueError:
                pass
    out["time"] = s.row()
    return out


def charted_today(con: duckdb.DuckDBPyConnection, day: dt.date) -> list[dict[str, Any]]:
    cur = con.execute(
        "SELECT app_id, bundle_id, name, country, list_key, list_kind, rank FROM stg_mr_rank_input "
        "WHERE snapshot_date = ? ORDER BY app_id, country, list_key",
        [day],
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def game_bands(
    con: duckdb.DuckDBPyConnection,
    day: dt.date,
    curves: dict[str, Curve],
    split: dict[str, Any],
    es: EstimateSettings,
    rng: random.Random,
) -> list[dict[str, Any]]:
    rows = charted_today(con, day)
    free_countries: dict[str, set[str]] = {}
    for r in rows:
        if r["list_kind"] == FREE:
            free_countries.setdefault(r["app_id"], set()).add(r["country"])
    out: list[dict[str, Any]] = []
    for r in rows:
        c = curves.get(r["list_key"])
        if not c or c.status != "ok" or c.fit is None:
            continue
        draws = c.fit.draws(r["rank"], rng)
        lo, mid, hi = band(draws, es)
        n = c.n_games
        base = {
            "snapshot_date": day,
            "app_id": r["app_id"],
            "play_id": None,
            "country": r["country"],
            "list_key": r["list_key"],
            "rank": r["rank"],
            "n": n,
            "basis": "estimate",
        }
        method = (
            f"rank->Play installs/day power law ({r['list_key']}, b={c.fit.b:.2f}, "
            f"n={n} games, {es.band_pct}% band)"
        )
        out.append(
            {
                **base,
                "kind": "android_installs_ww",
                "low": lo,
                "mid": mid,
                "high": hi,
                "unit": "installs/day",
                "method": method,
            }
        )
        rr = ratio_range(split, sorted(free_countries.get(r["app_id"], {r["country"]})))
        if rr:
            ios = [d + math.log(log_uniform(rng, *rr)) for d in draws]
            ilo, imid, ihi = band(ios, es)
            out.append(
                {
                    **base,
                    "kind": "ios_downloads_ww",
                    "low": ilo,
                    "mid": imid,
                    "high": ihi,
                    "unit": "downloads/day",
                    "method": method + f" x iOS:Android {rr[0]:.2f}-{rr[1]:.2f} (ios_android.yaml)",
                }
            )
    return out
