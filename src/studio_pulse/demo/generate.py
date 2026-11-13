"""Synthetic demo data for three fictional apps.

Everything here is invented. The generator writes source files in the same
formats the real connectors read, so `pulse demo` exercises the full pipeline:

  ga4/<app>/events.parquet            GA4 BigQuery export layout (nested params)
  crashlytics/<app>/crashes.parquet   Crashlytics export layout
  revenuecat/<app>/<date>/transactions_<epoch>.parquet
                                      incremental RevenueCat deliveries, with
                                      refunds/unsubscribes re-delivering rows
  admob/<app>.json                    AdMob networkReport:generate response
  play/<app>/installs_*/ratings_*     Play Console statistics CSVs (UTF-16)
  appstore/<app>/S_D_*.txt            App Store Connect Summary Sales reports
  unity/<app>/pulse-<date>.jsonl      Studio.Pulse JsonLinesSink output

One app has a scripted incident in its last days (a release that raises crash
rate, slows low-tier devices and hurts D1 retention) so the alert rules have
something to find.

Retention is calibrated to public benchmarks rather than to look good. Day-N
activity follows p(n) = d1 * engagement * n^-decay with a per-user lognormal
engagement, which lands the three apps at (D1 / D7 / D30):
  mossy_match  a good puzzle game   ~38% / ~10% / ~3%   (top 10-25% of games)
  pacer_pal    a fitness app        ~24% / ~8% / ~3%    (flatter: habit apps)
  quartz_merge a weaker casual game ~27% / ~5% / ~1.3%  (around the top 25%)
GameAnalytics' 2026 benchmarks (16,262 mobile games, all genres) put the
median D7 just under 4% and D30 at 0.69-0.79%; see benchmarks.py.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .. import events as E
from .export_files import flat_table, write_crashlytics_parquet, write_ga4_parquet
from .reviews import generate as generate_reviews
from .ua import UASim

DEMO_END = dt.date(2026, 10, 31)
DEMO_DAYS = 75

PARAM_TYPES = {
    "ga_session_id": "int",
    "engagement_time_msec": "int",
    "level": "int",
    "fatal": "int",
    "value": "double",
    "currency": "string",
    "ad_format": "string",
    E.PARAM_SCHEMA_VERSION: "int",
    E.PARAM_TIER: "string",
    E.PARAM_SCENE: "string",
    E.PARAM_WINDOW_S: "double",
    E.PARAM_FRAMES: "int",
    E.PARAM_FT_SOURCE: "string",
    E.PARAM_FT_P50: "double",
    E.PARAM_FT_P95: "double",
    E.PARAM_FT_MAX: "double",
    E.PARAM_SLOW_FRAMES: "int",
    E.PARAM_CPU_MAIN_P95: "double",
    E.PARAM_GPU_P95: "double",
    E.PARAM_MEM_SOURCE: "string",
    E.PARAM_MEM_ALLOC: "int",
    E.PARAM_MEM_RESERVED: "int",
    E.PARAM_GC_RESERVED: "int",
    E.PARAM_SYS_USED: "int",
    E.PARAM_SYS_MEM: "int",
    E.PARAM_KIND: "string",
    E.PARAM_DURATION: "double",
    "experiment_id": "string",
    "variant": "string",
}

# A/B test in the demo: Mossy Match shows variant B a cheaper booster offer
# from day 50 to day 63 to players with 14+ days of tenure; B players use about
# 8% more boosters. Exposure is an
# event with the experiment id and variant; the metric is booster_used events
# in the 7 days after first exposure, the CUPED covariate the 14 days before.
DEMO_EXPERIMENTS = {
    "mossy_match": {
        "id": "booster_offer_v2",
        "name": "Booster offer v2",
        "start": 50,
        "end": 63,
        "lift": 1.08,
    }
}

COUNTRIES = [
    ("United States", "US", "USD", 0.24, 1.6),
    ("Brazil", "BR", "BRL", 0.12, 0.45),
    ("India", "IN", "INR", 0.10, 0.25),
    ("Germany", "DE", "EUR", 0.08, 1.3),
    ("Turkey", "TR", "TRY", 0.08, 0.35),
    ("United Kingdom", "GB", "GBP", 0.07, 1.3),
    ("France", "FR", "EUR", 0.06, 1.1),
    ("Mexico", "MX", "MXN", 0.07, 0.5),
    ("Japan", "JP", "JPY", 0.05, 1.4),
    ("Indonesia", "ID", "IDR", 0.13, 0.3),
]
# Fictional, fixed demo exchange rates (USD per unit of currency).
DEMO_FX = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "BRL": 0.18,
    "INR": 0.012,
    "TRY": 0.029,
    "MXN": 0.055,
    "JPY": 0.0066,
    "IDR": 0.000063,
}
TAX = {
    "DE": 0.16,
    "FR": 0.167,
    "GB": 0.167,
    "TR": 0.167,
    "IN": 0.153,
    "JP": 0.091,
    "BR": 0.09,
    "MX": 0.138,
    "ID": 0.1,
    "US": 0.0,
}

MODELS = {
    ("ANDROID", "low"): ["SM-A055F", "Redmi 12C", "moto e13"],
    ("ANDROID", "mid"): ["SM-A546B", "Redmi Note 13", "Pixel 6a"],
    ("ANDROID", "high"): ["SM-S921B", "Pixel 8 Pro", "OnePlus 12"],
    ("IOS", "low"): ["iPhone 8", "iPhone SE"],
    ("IOS", "mid"): ["iPhone 12", "iPhone 13"],
    ("IOS", "high"): ["iPhone 15 Pro", "iPhone 16"],
}
SYS_MEM = {"low": 3072, "mid": 6144, "high": 8192}


@dataclass
class Incident:
    days_before_end: int
    version: str
    crash_mult: float
    low_ft_add: float
    mid_ft_add: float
    d1_mult: float


@dataclass
class DemoApp:
    id: str
    name: str
    genre: str
    platforms: list[str]
    package: str
    apple_id: str | None
    installs: float
    d1: float
    decay: float
    sessions: float
    iap: str | None  # consumable | subscription | ga4_consumable | None
    payer_rate: float
    ads_per_session: float
    ecpm: float
    perf: str | None  # ga4 | jsonl | None
    levels: bool
    crash_rate: float
    versions: list[tuple[int, str]]  # (day index released, version)
    incident: Incident | None = None
    campaign: tuple[int, int, float] | None = None
    admob_app_id: str = ""
    seed: int = 1


APPS = [
    DemoApp(
        id="mossy_match",
        name="Mossy Match",
        genre="puzzle",
        platforms=["ANDROID", "IOS"],
        package="demo.mossymatch",
        apple_id="9000000001",
        installs=260,
        d1=0.343,
        decay=0.68,
        sessions=1.6,
        iap="consumable",
        payer_rate=0.035,
        ads_per_session=1.4,
        ecpm=9.0,
        perf="ga4",
        levels=True,
        crash_rate=0.0018,
        versions=[(0, "2.3.0"), (38, "2.4.0"), (61, "2.4.1")],
        campaign=(28, 35, 1.7),
        admob_app_id="ca-app-pub-0000000000000000~1000000001",
        seed=11,
    ),
    DemoApp(
        id="pacer_pal",
        name="Pacer Pal",
        genre="fitness",
        platforms=["ANDROID", "IOS"],
        package="demo.pacerpal",
        apple_id="9000000002",
        installs=120,
        d1=0.217,
        decay=0.58,
        sessions=1.2,
        iap="subscription",
        payer_rate=0.075,
        ads_per_session=0.0,
        ecpm=0.0,
        perf=None,
        levels=False,
        crash_rate=0.0012,
        versions=[(0, "5.1.0"), (45, "5.2.0")],
        seed=22,
    ),
    DemoApp(
        id="quartz_merge",
        name="Quartz Merge",
        genre="casual",
        platforms=["ANDROID"],
        package="demo.quartzmerge",
        apple_id=None,
        installs=280,
        d1=0.244,
        decay=0.79,
        sessions=1.9,
        iap="ga4_consumable",
        payer_rate=0.015,
        ads_per_session=2.2,
        ecpm=6.0,
        perf="jsonl",
        levels=True,
        crash_rate=0.0022,
        versions=[(0, "1.7.2")],
        incident=Incident(
            days_before_end=4,
            version="1.8.0",
            crash_mult=6.5,
            low_ft_add=6.5,
            mid_ft_add=1.2,
            d1_mult=0.55,
        ),
        admob_app_id="ca-app-pub-0000000000000000~1000000003",
        seed=33,
    ),
]


@dataclass
class User:
    uid: str
    install: int
    platform: str
    country: tuple
    tier: str
    model: str
    engage: float
    payer: bool
    level: int = 1
    sub: dict[str, Any] | None = None
    traffic: tuple[str, str, str] = ("(direct)", "(none)", "(direct)")
    install_version: str | None = None  # set during an incident window only


class EventBuffer:
    """Column buffers grouped by parameter signature (fast appends)."""

    def __init__(self) -> None:
        self.groups: dict[tuple[str, ...], dict[str, list[Any]]] = {}

    def add(
        self,
        base: dict[str, Any],
        name: str,
        ts: int,
        value_usd: float | None,
        params: dict[str, Any],
    ) -> None:
        key = tuple(params)
        g = self.groups.get(key)
        if g is None:
            g = self.groups[key] = defaultdict(list)
        g["event_date"].append(base["event_date"])
        g["event_timestamp"].append(ts)
        g["event_name"].append(name)
        g["user_pseudo_id"].append(base["user_pseudo_id"])
        g["user_first_touch_timestamp"].append(base["user_first_touch_timestamp"])
        g["platform"].append(base["platform"])
        g["app_id"].append(base["app_id"])
        g["app_version"].append(base["app_version"])
        g["device_category"].append("mobile")
        g["mobile_model_name"].append(base["mobile_model_name"])
        g["country"].append(base["country"])
        g["event_value_in_usd"].append(value_usd)
        g["ts_source"].append(base["ts_source"])
        g["ts_medium"].append(base["ts_medium"])
        g["ts_name"].append(base["ts_name"])
        for k, v in params.items():
            g["p_" + k].append(v)

    def table(self) -> pa.Table:
        tables = []
        for g in self.groups.values():
            types = {k: PARAM_TYPES[k] for k in (c[2:] for c in g if c.startswith("p_"))}
            tables.append(flat_table(g, types))
        full = pa.concat_tables(tables, promote_options="default")
        # put every param column in a stable order
        cols = [c for c in full.column_names if not c.startswith("p_")] + sorted(
            c for c in full.column_names if c.startswith("p_")
        )
        return full.select(cols)


def _micros(day: dt.date, seconds: float) -> int:
    return int(
        (dt.datetime.combine(day, dt.time()) - dt.datetime(1970, 1, 1)).total_seconds() * 1e6
        + seconds * 1e6
    )


def _choice_w(rng: random.Random, items: list, weights: list[float]):
    return rng.choices(items, weights=weights, k=1)[0]


def _version_for(app: DemoApp, day_idx: int, days: int, rng: random.Random) -> str:
    rel = [(d, v) for d, v in app.versions]
    if app.incident:
        rel.append((days - app.incident.days_before_end, app.incident.version))
    rel.sort()
    current, previous, rel_day = rel[0][1], rel[0][1], 0
    for d, v in rel:
        if d <= day_idx:
            previous, current, rel_day = current, v, d
    if current == previous:
        return current
    adoption = min(0.97, 0.55 + 0.1 * (day_idx - rel_day))
    return current if rng.random() < adoption else previous


class Generator:
    def __init__(self, app: DemoApp, start: dt.date, days: int, scale: float, out: Path):
        self.app, self.start, self.days, self.scale, self.out = app, start, days, scale, out
        self.rng = random.Random(app.seed)
        self.ev = EventBuffer()
        self.crash: dict[str, list[Any]] = defaultdict(list)
        self.rc_tx: list[dict[str, Any]] = []  # every version, with delivery date
        self.unity_lines: dict[dt.date, list[str]] = defaultdict(list)
        self.new_by_day: dict[tuple[int, str], int] = defaultdict(int)
        self.ad_by_day: dict[int, list[float]] = defaultdict(list)
        self.iap_store: dict[tuple[int, str, str], list[float]] = defaultdict(
            list
        )  # (day, platform, currency)
        self.dau_by_day: dict[tuple[int, str], int] = defaultdict(int)
        # Bought users and the ad networks' reports (own random stream).
        self.ua = UASim(app.id, app.seed, start)
        # Experiment events (own random stream, so the other numbers do not move).
        self.rng_x = random.Random(app.seed * 31 + 5)
        self.booster_rate: dict[str, float] = {}
        self.exposed: dict[str, int] = {}

    # -- population ---------------------------------------------------------------
    def _new_user(self, day_idx: int) -> User:
        a, r = self.app, self.rng
        platform = (
            a.platforms[0]
            if len(a.platforms) == 1
            else _choice_w(r, ["ANDROID", "IOS"], [0.64, 0.36])
        )
        country = _choice_w(r, COUNTRIES, [c[3] for c in COUNTRIES])
        tier = _choice_w(
            r,
            ["low", "mid", "high"],
            [0.32, 0.45, 0.23] if platform == "ANDROID" else [0.12, 0.48, 0.40],
        )
        engage = r.lognormvariate(0, 0.45)
        install_version = None
        if a.incident and day_idx >= self.days - a.incident.days_before_end:
            # The bad release causes the D1 drop: only users who install it churn more
            # (a planted, single-dimension cause for the brain's root-cause tests).
            install_version = _version_for(a, day_idx, self.days, r)
            if install_version == a.incident.version:
                engage *= a.incident.d1_mult
        payer = r.random() < a.payer_rate * (1.6 if country[4] > 1 else 0.7)
        return User(
            uid=f"{r.getrandbits(64):016X}{r.getrandbits(64):016X}",
            install=day_idx,
            platform=platform,
            country=country,
            tier=tier,
            model=r.choice(MODELS[(platform, tier)]),
            engage=engage,
            payer=payer,
            install_version=install_version,
        )

    def _p_active(self, u: User, n: int) -> float:
        return min(0.97, self.app.d1 * u.engage * n ** (-self.app.decay))

    # -- simulation ---------------------------------------------------------------
    def run(self) -> None:
        a, r = self.app, self.rng
        users: list[User] = []
        for di in range(self.days):
            day = self.start + dt.timedelta(days=di)
            mult = 1.0 + (0.18 if day.weekday() >= 5 else 0.0)
            mult *= 1.0 + 0.003 * di
            if a.campaign and a.campaign[0] <= di <= a.campaign[1]:
                mult *= a.campaign[2]
            n_new = max(0, int(round(a.installs * self.scale * mult * r.gauss(1.0, 0.08))))
            for _ in range(n_new):
                u = self._new_user(di)
                u.traffic = self.ua.traffic(di, u.country[1], u.platform)
                users.append(u)
            for u in users:
                n = di - u.install
                if n == 0 or r.random() < self._p_active(u, n):
                    self._user_day(u, di, day, n)

    def _user_day(self, u: User, di: int, day: dt.date, n: int) -> None:
        a, r = self.app, self.rng
        version = (
            u.install_version if n == 0 and u.install_version else _version_for(a, di, self.days, r)
        )
        incident = bool(a.incident and version == a.incident.version)
        base = {
            "event_date": day.strftime("%Y%m%d"),
            "user_pseudo_id": u.uid,
            "user_first_touch_timestamp": _micros(self.start + dt.timedelta(days=u.install), 3600),
            "platform": u.platform,
            "app_id": a.package,
            "app_version": version,
            "mobile_model_name": u.model,
            "country": u.country[0],
            "ts_source": u.traffic[0],
            "ts_medium": u.traffic[1],
            "ts_name": u.traffic[2],
        }
        self.dau_by_day[(di, u.platform)] += 1
        t = r.uniform(7 * 3600, 21 * 3600)
        if n == 0:
            self.ev.add(base, "first_open", _micros(day, t - 1), None, {})
            self.new_by_day[(di, u.platform)] += 1
        n_sessions = (
            1
            + (1 if r.random() < a.sessions - 1 else 0)
            + (1 if r.random() < (a.sessions - 1) / 3 else 0)
        )
        crashed_today = r.random() < a.crash_rate * (a.incident.crash_mult if incident else 1.0) * (
            1.5 if u.tier == "low" else 1.0
        )
        for s in range(n_sessions):
            t = min(t + (r.uniform(600, 4 * 3600) if s else 0), 22.5 * 3600)
            sid = int(_micros(day, t) // 1_000_000)
            sp = {"ga_session_id": sid}
            self.ev.add(base, "session_start", _micros(day, t), None, sp)
            minutes = max(
                0.3, r.lognormvariate(math.log(5.5 if a.genre != "fitness" else 7.0), 0.6)
            )
            self.ev.add(
                base,
                "user_engagement",
                _micros(day, t + minutes * 60),
                None,
                {**sp, "engagement_time_msec": int(minutes * 60000)},
            )
            if a.levels:
                self._levels(u, base, day, t, sp)
            ads = self._poisson(a.ads_per_session) if a.ads_per_session else 0
            for k in range(ads):
                val = a.ecpm / 1000 * u.country[4] * r.uniform(0.5, 1.6)
                fmt = "rewarded" if r.random() < 0.35 else "interstitial"
                self.ev.add(
                    base,
                    "ad_impression",
                    _micros(day, t + 30 + k * 40),
                    round(val, 6),
                    {**sp, "value": round(val, 6), "currency": "USD", "ad_format": fmt},
                )
                self.ad_by_day[di].append(val)
            if a.perf and r.random() < 0.6:
                self._perf(u, base, day, t, sp, version, incident, minutes, s == 0)
            if crashed_today and s == n_sessions - 1:
                self.ev.add(
                    base, "app_exception", _micros(day, t + minutes * 30), None, {**sp, "fatal": 1}
                )
                self.crash["event_timestamp"].append(
                    dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=t + minutes * 30)
                )
                self.crash["platform"].append(u.platform)
                self.crash["is_fatal"].append(True)
                self.crash["installation_uuid"].append(u.uid.lower())
                self.crash["issue_id"].append(
                    f"issue-{(r.randrange(3) if not incident else 7 + r.randrange(2)):02d}"
                )
                self.crash["app_version"].append(version)
            if r.random() < 0.004:  # non-fatal
                self.crash["event_timestamp"].append(
                    dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=t + 5)
                )
                self.crash["platform"].append(u.platform)
                self.crash["is_fatal"].append(False)
                self.crash["installation_uuid"].append(u.uid.lower())
                self.crash["issue_id"].append("nonfatal-01")
                self.crash["app_version"].append(version)
        self._purchases(u, base, di, day, t)
        self._experiment(u, base, di, day, t)

    def _experiment(self, u: User, base: dict, di: int, day: dt.date, t: float) -> None:
        x = DEMO_EXPERIMENTS.get(self.app.id)
        if not x:
            return
        r = self.rng_x
        rate = self.booster_rate.get(u.uid)
        if rate is None:
            rate = self.booster_rate[u.uid] = 1.5 * r.lognormvariate(0, 0.9)
        h = int(hashlib.sha256(f"{x['id']}:{u.uid}".encode()).hexdigest()[:8], 16)
        variant = "B" if h % 2 else "A"
        first = self.exposed.get(u.uid)
        # Shown to players with at least 14 days of tenure (the pre-period is complete).
        if first is None and x["start"] <= di <= x["end"] and di - u.install >= 14:
            first = self.exposed[u.uid] = di
            self.ev.add(
                base,
                "experiment_exposure",
                _micros(day, t + 5),
                None,
                {"experiment_id": x["id"], "variant": variant},
            )
        lam = rate * (x["lift"] if first is not None and variant == "B" else 1.0)
        for k in range(self._poisson(lam, r)):
            self.ev.add(base, "booster_used", _micros(day, t + 90 + 30 * k), None, {})

    def experiments(self) -> list[dict[str, Any]]:
        x = DEMO_EXPERIMENTS.get(self.app.id)
        if not x:
            return []
        return [
            {
                "id": x["id"],
                "name": x["name"],
                "start": (self.start + dt.timedelta(days=x["start"])).isoformat(),
                "end": (self.start + dt.timedelta(days=x["end"])).isoformat(),
                "metric_event": "booster_used",
                "conversion_event": "in_app_purchase",
                "window_days": 7,
                "pre_days": 14,
                "control": "A",
                "split": {"A": 0.5, "B": 0.5},
            }
        ]

    def _poisson(self, lam: float, rng: random.Random | None = None) -> int:
        r, L, k, p = rng or self.rng, math.exp(-lam), 0, 1.0
        while True:
            p *= r.random()
            if p <= L:
                return k
            k += 1

    def _levels(self, u: User, base: dict, day: dt.date, t: float, sp: dict) -> None:
        r = self.rng
        for k in range(r.randint(1, 4)):
            lvl = u.level
            hard = lvl % 7 == 0
            win_p = (0.42 if hard else 0.86) - min(0.25, lvl * 0.004)
            ts = _micros(day, t + 20 + k * 90)
            self.ev.add(base, E.LEVEL_START, ts, None, {**sp, "level": lvl})
            if r.random() < win_p:
                self.ev.add(base, E.LEVEL_COMPLETE, ts + 60_000_000, None, {**sp, "level": lvl})
                u.level += 1
            else:
                self.ev.add(base, E.LEVEL_FAIL, ts + 60_000_000, None, {**sp, "level": lvl})
                if hard:
                    u.engage *= 0.9

    def _perf(
        self,
        u: User,
        base: dict,
        day: dt.date,
        t: float,
        sp: dict,
        version: str,
        incident: bool,
        minutes: float,
        first: bool,
    ) -> None:
        r, a = self.rng, self.app
        base_ft = {"low": 18.4 if a.id == "quartz_merge" else 17.6, "mid": 16.9, "high": 16.7}[
            u.tier
        ]
        p95_add = {"low": 13.0, "mid": 5.5, "high": 1.6}[u.tier]
        slow = {"low": 0.032, "mid": 0.007, "high": 0.002}[u.tier]
        # Share of the frame p95 spent on the CPU main thread / GPU: low-tier
        # phones are GPU-bound, mid-tier CPU-bound, high-tier mostly waiting on vsync.
        cpu_share, gpu_share = {"low": (0.55, 0.88), "mid": (0.74, 0.6), "high": (0.45, 0.4)}[
            u.tier
        ]
        if incident:
            base_ft += (
                a.incident.low_ft_add
                if u.tier == "low"
                else (a.incident.mid_ft_add if u.tier == "mid" else 0.0)
            )
            p95_add *= 1.7 if u.tier == "low" else 1.15
            slow *= 3.2 if u.tier == "low" else 1.3
            gpu_share = min(0.97, gpu_share * 1.12)  # the 1.8.0 regression is a heavier shader
        gles = (
            u.platform == "ANDROID" and u.tier == "low" and r.random() < 0.3
        )  # no GPU timing on GLES by default
        windows = []
        for w in range(2):
            p50 = max(8.3, r.gauss(base_ft, 0.9 if u.tier == "low" else 0.3))
            p95 = p50 + max(0.5, r.gauss(p95_add, p95_add * 0.25))
            frames = int(30_000 / p50 * r.uniform(0.97, 1.0))
            alloc = int(r.gauss({"low": 290, "mid": 365, "high": 430}[u.tier], 35))
            reserved = int(alloc * r.uniform(1.18, 1.35))
            params = {
                E.PARAM_SCHEMA_VERSION: E.SCHEMA_VERSION,
                E.PARAM_TIER: u.tier,
                E.PARAM_SCENE: "Level" if w else "Menu",
                E.PARAM_WINDOW_S: 30.0,
                E.PARAM_FRAMES: frames,
                # FrameTimingManager results lag a few frames, so a session's first
                # 30 s window uses deltaTime: one window in (session length / 30 s).
                E.PARAM_FT_SOURCE: E.SOURCE_DELTA_TIME
                if w == 0 and r.random() < 30 / max(60.0, minutes * 60)
                else E.SOURCE_FRAME_TIMING,
                E.PARAM_FT_P50: round(p50, 2),
                E.PARAM_FT_P95: round(p95, 2),
                E.PARAM_FT_MAX: round(p95 + r.uniform(5, 60), 2),
                E.PARAM_SLOW_FRAMES: int(frames * slow * r.uniform(0.5, 1.5)),
                E.PARAM_CPU_MAIN_P95: round(p95 * cpu_share * r.uniform(0.92, 1.05), 2),
                E.PARAM_MEM_SOURCE: E.SOURCE_RECORDER,
                E.PARAM_MEM_ALLOC: alloc,
                E.PARAM_MEM_RESERVED: reserved,
                E.PARAM_GC_RESERVED: int(r.gauss({"low": 48, "mid": 64, "high": 80}[u.tier], 8)),
                E.PARAM_SYS_USED: int(
                    reserved + r.gauss({"low": 260, "mid": 330, "high": 400}[u.tier], 40)
                ),
                E.PARAM_SYS_MEM: SYS_MEM[u.tier],
            }
            if not gles:
                params[E.PARAM_GPU_P95] = round(p95 * gpu_share * r.uniform(0.92, 1.05), 2)
            windows.append((t + 30 * (w + 1), E.PERF_EVENT, params))
        loads = []
        if first or r.random() < 0.3:
            cold = r.lognormvariate(
                math.log({"low": 4300, "mid": 2900, "high": 2100}[u.tier]), 0.22
            )
            loads.append(
                (
                    t + 1,
                    E.LOAD_EVENT,
                    {
                        E.PARAM_SCHEMA_VERSION: 1,
                        E.PARAM_TIER: u.tier,
                        E.PARAM_KIND: E.LOAD_KIND_COLD_START,
                        E.PARAM_SCENE: "Boot",
                        E.PARAM_DURATION: round(cold, 1),
                    },
                )
            )
        scene_ms = r.lognormvariate(math.log({"low": 1350, "mid": 880, "high": 610}[u.tier]), 0.25)
        loads.append(
            (
                t + 25,
                E.LOAD_EVENT,
                {
                    E.PARAM_SCHEMA_VERSION: 1,
                    E.PARAM_TIER: u.tier,
                    E.PARAM_KIND: E.LOAD_KIND_SCENE,
                    E.PARAM_SCENE: "Level",
                    E.PARAM_DURATION: round(scene_ms, 1),
                },
            )
        )
        for ts, name, params in windows + loads:
            if a.perf == "ga4":
                self.ev.add(base, name, _micros(day, ts), None, {**sp, **params})
            else:
                stamp = dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=ts)
                line = {
                    "v": E.SCHEMA_VERSION,
                    "event": name,
                    "ts": stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "app": a.package,
                    "app_version": base["app_version"],
                    "platform": u.platform,
                    "session": f"{sp['ga_session_id']:x}-{u.uid[:8].lower()}",
                    "params": params,
                }
                self.unity_lines[day].append(json.dumps(line, separators=(",", ":")))

    # -- monetisation ---------------------------------------------------------------
    def _rc_row(
        self,
        u: User,
        day: dt.date,
        t: float,
        usd: float,
        product: str,
        renewal: int,
        trial: bool,
        conv: bool,
        auto: bool,
        period_days: int | None,
        tx: str,
        orig: str,
    ) -> dict[str, Any]:
        cur = u.country[2]
        start = dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=t)
        local = round(usd / DEMO_FX[cur], 2 if cur not in ("JPY", "IDR") else 0)
        return {
            "rc_original_app_user_id": "$RCAnonymousID:" + u.uid[:24].lower(),
            "rc_last_seen_app_user_id_alias": None,
            "country": u.country[1],
            "product_identifier": product,
            "start_time": start,
            "end_time": start + dt.timedelta(days=period_days) if period_days else None,
            "store": "play_store" if u.platform == "ANDROID" else "app_store",
            "is_auto_renewable": auto,
            "is_trial_period": trial,
            "is_in_intro_offer_period": False,
            "is_sandbox": False,
            "price_in_usd": usd,
            "purchase_price_in_usd": usd,
            "tax_percentage": TAX.get(u.country[1], 0.1) if usd else 0.0,
            "commission_percentage": 0.15 if usd else 0.0,
            "takehome_percentage": None,
            "store_transaction_id": tx,
            "original_store_transaction_id": orig,
            "refunded_at": None,
            "unsubscribe_detected_at": None,
            "purchased_currency": cur,
            "price_in_purchased_currency": local,
            "purchase_price_in_purchased_currency": local,
            "renewal_number": renewal,
            "is_trial_conversion": conv,
            "ownership_type": "PURCHASED",
            "platform": u.platform.lower() if u.platform == "ANDROID" else "iOS",
            "first_seen_time": dt.datetime.combine(
                self.start + dt.timedelta(days=u.install), dt.time(1)
            ),
            "updated_at": start,
            "_deliver": day,
        }

    def _tx_id(self, u: User) -> str:
        r = self.rng
        if u.platform == "ANDROID":
            return f"GPA.{r.randrange(1000, 9999)}-{r.randrange(1000, 9999)}-{r.randrange(1000, 9999)}-{r.randrange(10000, 99999)}"
        return str(r.randrange(10**14, 10**15))

    def _maybe_update(self, row: dict[str, Any], day: dt.date) -> None:
        """Simulate later re-deliveries of the same transaction."""
        r = self.rng
        last = self.start + dt.timedelta(days=self.days - 1)
        if row["price_in_usd"] and r.random() < 0.045:  # refund
            when = day + dt.timedelta(days=r.randint(2, 12))
            if when <= last:
                upd = dict(row)
                upd.update(
                    refunded_at=dt.datetime.combine(when, dt.time(10)),
                    price_in_usd=0.0,
                    price_in_purchased_currency=0.0,
                    updated_at=dt.datetime.combine(when, dt.time(10)),
                    _deliver=when,
                )
                self.rc_tx.append(upd)
                return
        if row["is_auto_renewable"] and r.random() < 0.25:  # unsubscribe detected
            when = day + dt.timedelta(days=r.randint(1, 20))
            if when <= last:
                upd = dict(row)
                upd.update(
                    unsubscribe_detected_at=dt.datetime.combine(when, dt.time(8)),
                    updated_at=dt.datetime.combine(when, dt.time(8)),
                    _deliver=when,
                )
                self.rc_tx.append(upd)
        if r.random() < 0.03:  # plain re-send, identical content
            when = day + dt.timedelta(days=1)
            if when <= last:
                upd = dict(row)
                upd["_deliver"] = when
                self.rc_tx.append(upd)

    def _purchases(self, u: User, base: dict, di: int, day: dt.date, t: float) -> None:
        a, r = self.app, self.rng
        if (
            a.iap in ("consumable", "ga4_consumable")
            and u.payer
            and r.random() < (0.35 if di - u.install < 3 else 0.12)
        ):
            usd = _choice_w(r, [0.99, 1.99, 4.99, 9.99, 19.99], [0.25, 0.3, 0.28, 0.12, 0.05])
            product = {
                0.99: "coins_small",
                1.99: "coins_medium",
                4.99: "coins_large",
                9.99: "starter_bundle",
                19.99: "mega_bundle",
            }[usd]
            self.ev.add(
                base,
                "in_app_purchase",
                _micros(day, t + 200),
                usd,
                {"value": usd, "currency": "USD"},
            )
            self.iap_store[(di, u.platform, u.country[2])].append(usd)
            if a.iap == "consumable":
                tx = self._tx_id(u)
                row = self._rc_row(
                    u, day, t + 200, usd, product, 1, False, False, False, None, tx, tx
                )
                self.rc_tx.append(row)
                self._maybe_update(row, day)
        if a.iap == "subscription":
            if u.sub is None and di - u.install <= 1 and u.payer and r.random() < 0.7:
                annual = r.random() < 0.3
                tx = self._tx_id(u)
                u.sub = {"annual": annual}
                product = "pro_annual" if annual else "pro_monthly"
                row = self._rc_row(u, day, t + 120, 0.0, product, 1, True, False, True, 7, tx, tx)
                self.rc_tx.append(row)

    def run_subscriptions(self) -> None:
        """Renewals happen whether or not the user opens the app."""
        if self.app.iap != "subscription":
            return
        # Users are not kept after run(); re-derive from the trial rows.
        trials = [row for row in self.rc_tx if row["is_trial_period"]]
        r = self.rng
        last = self.days - 1
        for tr in trials:
            converts = r.random() < 0.52
            if not converts:
                continue
            annual = tr["product_identifier"] == "pro_annual"
            usd = 59.99 if annual else 9.99
            di = (tr["start_time"].date() - self.start).days + 7
            renewal = 2
            while di <= last:
                day = self.start + dt.timedelta(days=di)
                row = dict(tr)
                tx = tr["original_store_transaction_id"] + (
                    f"..{renewal - 2}" if tr["store"] == "play_store" else str(renewal)
                )
                start = dt.datetime.combine(day, dt.time()) + dt.timedelta(
                    seconds=tr["start_time"].hour * 3600
                )
                cur = row["purchased_currency"]
                local = round(usd / DEMO_FX[cur], 2 if cur not in ("JPY", "IDR") else 0)
                row.update(
                    store_transaction_id=tx,
                    renewal_number=renewal,
                    is_trial_period=False,
                    is_trial_conversion=renewal == 2,
                    price_in_usd=usd,
                    purchase_price_in_usd=usd,
                    price_in_purchased_currency=local,
                    purchase_price_in_purchased_currency=local,
                    tax_percentage=TAX.get(row["country"], 0.1),
                    commission_percentage=0.15,
                    start_time=start,
                    end_time=start + dt.timedelta(days=365 if annual else 30),
                    updated_at=start,
                    _deliver=day,
                )
                self.rc_tx.append(row)
                self._maybe_update(row, day)
                self.iap_store[
                    (di, "ANDROID" if tr["store"] == "play_store" else "IOS", cur)
                ].append(usd)
                if annual or r.random() > 0.83:
                    break
                di += 30
                renewal += 1

    # -- writers ----------------------------------------------------------------
    def write(self) -> dict[str, Any]:
        a, out = self.app, self.out
        sources: dict[str, Any] = {}
        write_ga4_parquet(self.ev.table(), out / "ga4" / a.id / "events.parquet")
        sources["ga4_files"] = {"path": f"ga4/{a.id}/events.parquet", "app_info_ids": [a.package]}
        if self.crash:
            write_crashlytics_parquet(self.crash, out / "crashlytics" / a.id / "crashes.parquet")
            sources["crashlytics_files"] = {"path": f"crashlytics/{a.id}/crashes.parquet"}
        if self.rc_tx:
            self._write_revenuecat(out / "revenuecat" / a.id)
            sources["revenuecat"] = {"path": f"revenuecat/{a.id}"}
        if self.ad_by_day:
            self._write_admob(out / "admob" / f"{a.id}.json")
            sources["admob"] = {"response_file": f"admob/{a.id}.json", "app_ids": [a.admob_app_id]}
        if "ANDROID" in a.platforms:
            self._write_play(out / "play" / a.id)
            sources["play_console"] = {"path": f"play/{a.id}", "package": a.package}
        if "IOS" in a.platforms:
            self._write_appstore(out / "appstore" / a.id)
            sources["app_store"] = {
                "path": f"appstore/{a.id}",
                "apple_id": a.apple_id,
                "sku": a.package,
            }
        if self.unity_lines:
            d = out / "unity" / a.id
            d.mkdir(parents=True, exist_ok=True)
            for day, lines in sorted(self.unity_lines.items()):
                (d / f"pulse-{day:%Y%m%d}.jsonl").write_text(
                    "\n".join(lines) + "\n", encoding="utf-8"
                )
            sources["unity_jsonl"] = {"path": f"unity/{a.id}", "app": a.package}
        sources.update(self.ua.write(out))
        sources.update(generate_reviews(self, out))
        return sources

    def _write_revenuecat(self, root: Path) -> None:
        by_day: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rc_tx:
            by_day[row["_deliver"]].append({k: v for k, v in row.items() if k != "_deliver"})
        for day, rows in sorted(by_day.items()):
            d = root / day.isoformat()
            d.mkdir(parents=True, exist_ok=True)
            epoch = int(
                (
                    dt.datetime.combine(day + dt.timedelta(days=1), dt.time(3))
                    - dt.datetime(1970, 1, 1)
                ).total_seconds()
            )
            # A few rows arrive without a USD price, to exercise the FX fallback.
            for row in rows:
                if row["price_in_usd"] and self.rng.random() < 0.02:
                    row["price_in_usd"] = None
            pq.write_table(pa.Table.from_pylist(rows), d / f"transactions_{epoch}.parquet")

    def _write_admob(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        r = self.rng
        elements: list[dict[str, Any]] = [
            {"header": {"dateRange": {}, "localizationSettings": {"currencyCode": "USD"}}}
        ]
        for di in sorted(self.ad_by_day):
            vals = self.ad_by_day[di]
            day = self.start + dt.timedelta(days=di)
            imps = int(len(vals) * r.uniform(1.0, 1.04))
            earn = sum(vals) * r.uniform(0.95, 1.0)
            elements.append(
                {
                    "row": {
                        "dimensionValues": {
                            "DATE": {"value": day.strftime("%Y%m%d")},
                            "APP": {"value": self.app.admob_app_id, "displayLabel": self.app.name},
                        },
                        "metricValues": {
                            "ESTIMATED_EARNINGS": {"microsValue": str(int(earn * 1e6))},
                            "IMPRESSIONS": {"integerValue": str(imps)},
                            "CLICKS": {"integerValue": str(int(imps * r.uniform(0.008, 0.016)))},
                            "AD_REQUESTS": {"integerValue": str(int(imps / r.uniform(0.86, 0.93)))},
                            "MATCHED_REQUESTS": {
                                "integerValue": str(int(imps / r.uniform(0.95, 0.99)))
                            },
                        },
                    }
                }
            )
        elements.append({"footer": {"matchingRowCount": str(len(elements) - 1)}})
        path.write_text(json.dumps(elements, indent=1), encoding="utf-8")

    def _write_play(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        r = self.rng
        months: dict[str, list[list[str]]] = defaultdict(list)
        ratings: dict[str, list[list[str]]] = defaultdict(list)
        total = 4.31
        for di in range(self.days):
            day = self.start + dt.timedelta(days=di)
            new = self.new_by_day.get((di, "ANDROID"), 0)
            inst = int(new * r.uniform(1.03, 1.09))
            un = int(inst * r.uniform(0.28, 0.4) + self.dau_by_day.get((di, "ANDROID"), 0) * 0.004)
            key = day.strftime("%Y%m")
            months[key].append(
                [
                    day.isoformat(),
                    self.app.package,
                    str(inst),
                    str(un),
                    str(inst),
                    str(un),
                    str(inst),
                    str(int(self.dau_by_day.get((di, "ANDROID"), 0) * 0.02)),
                    str(un),
                ]
            )
            daily = r.gauss(4.4, 0.3) if new else None
            if daily:
                total = total * 0.985 + min(5.0, daily) * 0.015
            ratings[key].append(
                [
                    day.isoformat(),
                    self.app.package,
                    f"{min(5.0, daily):.2f}" if daily else "NA",
                    f"{total:.2f}",
                ]
            )
        head_i = [
            "Date",
            "Package Name",
            "Daily Device Installs",
            "Daily Device Uninstalls",
            "Daily User Installs",
            "Daily User Uninstalls",
            "Install events",
            "Update events",
            "Uninstall events",
        ]
        head_r = ["Date", "Package Name", "Daily Average Rating", "Total Average Rating"]
        for key, rows in months.items():
            self._utf16_csv(root / f"installs_{self.app.package}_{key}_overview.csv", head_i, rows)
        for key, rows in ratings.items():
            self._utf16_csv(root / f"ratings_{self.app.package}_{key}_overview.csv", head_r, rows)

    @staticmethod
    def _utf16_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        path.write_bytes(buf.getvalue().encode("utf-16"))

    def _write_appstore(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        r = self.rng
        head = [
            "Provider",
            "Provider Country",
            "SKU",
            "Developer",
            "Title",
            "Version",
            "Product Type Identifier",
            "Units",
            "Developer Proceeds",
            "Begin Date",
            "End Date",
            "Customer Currency",
            "Country Code",
            "Currency of Proceeds",
            "Apple Identifier",
            "Customer Price",
            "Promo Code",
            "Parent Identifier",
            "Subscription",
            "Period",
            "Category",
            "CMB",
            "Device",
            "Supported Platforms",
            "Proceeds Reason",
            "Preserved Pricing",
            "Client",
            "Order Type",
        ]
        for di in range(self.days):
            day = self.start + dt.timedelta(days=di)
            ds = day.strftime("%m/%d/%Y")
            rows = []
            new = self.new_by_day.get((di, "IOS"), 0)
            units = int(new * r.uniform(1.0, 1.06))
            if units:
                rows.append(
                    [
                        "APPLE",
                        "US",
                        self.app.package,
                        "Demo Studio",
                        self.app.name,
                        "",
                        "1F",
                        str(units),
                        "0",
                        ds,
                        ds,
                        "USD",
                        "US",
                        "USD",
                        self.app.apple_id,
                        "0",
                        "",
                        "",
                        "",
                        "",
                        "Games",
                        "",
                        "iPhone",
                        "iOS",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
                re_units = int(units * r.uniform(0.08, 0.15))
                rows.append(
                    [
                        "APPLE",
                        "US",
                        self.app.package,
                        "Demo Studio",
                        self.app.name,
                        "",
                        "3F",
                        str(re_units),
                        "0",
                        ds,
                        ds,
                        "USD",
                        "US",
                        "USD",
                        self.app.apple_id,
                        "0",
                        "",
                        "",
                        "",
                        "",
                        "Games",
                        "",
                        "iPhone",
                        "iOS",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
            for (d2, plat, cur), vals in self.iap_store.items():
                if d2 != di or plat != "IOS":
                    continue
                ptype = "IAY" if self.app.iap == "subscription" else "IA1"
                for usd in sorted(set(vals)):
                    cnt = vals.count(usd)
                    local = usd / DEMO_FX[cur]
                    proceeds = local * 0.85 * (1 - 0.1)
                    rows.append(
                        [
                            "APPLE",
                            "US",
                            f"{self.app.package}.iap",
                            "Demo Studio",
                            "In-app item",
                            "",
                            ptype,
                            str(cnt),
                            f"{proceeds:.2f}",
                            ds,
                            ds,
                            cur,
                            "",
                            cur,
                            f"8{self.app.apple_id[1:]}",
                            f"{local:.2f}",
                            "",
                            self.app.package,
                            "",
                            "",
                            "Games",
                            "",
                            "iPhone",
                            "iOS",
                            "",
                            "",
                            "",
                            "",
                        ]
                    )
            text = (
                "\t".join(head)
                + "\n"
                + "\n".join("\t".join(x) for x in rows)
                + ("\n" if rows else "")
            )
            (root / f"S_D_80000000_{day:%Y%m%d}.txt").write_text(text, encoding="utf-8")


def generate(
    out_dir: Path,
    *,
    scale: float = 1.0,
    end: dt.date = DEMO_END,
    days: int = DEMO_DAYS,
    apps: list[DemoApp] | None = None,
    log: Any = print,
    site_dir: Path | None = None,
    sdk: bool = True,
) -> Path:
    """Write demo source files and a pulse.yaml into out_dir. Returns the config path.

    site_dir is where the dashboard goes (default <out_dir>/site); it is stored
    relative to out_dir when possible, so `pulse run` / `pulse serve` on the
    generated config use the same folder as `pulse demo --out`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    site = "site"
    if site_dir is not None:
        try:
            site = Path(os.path.relpath(Path(site_dir).resolve(), out_dir.resolve())).as_posix()
        except ValueError:  # another drive on Windows
            site = str(Path(site_dir).resolve())
    start = end - dt.timedelta(days=days - 1)
    cfg: dict[str, Any] = {
        "title": "Studio Pulse demo",
        "demo": True,
        "warehouse": "warehouse.duckdb",
        "site_dir": site,
        "lookback_days": days,
        "end_date": end.isoformat(),
        "as_of": (end + dt.timedelta(days=1)).isoformat(),
        "fx_rates": DEMO_FX,
        "apps": [],
    }
    for app in apps or APPS:
        g = Generator(app, start, days, scale, out_dir)
        g.run()
        g.run_subscriptions()
        sources = g.write()
        entry = {
            "id": app.id,
            "name": app.name,
            "genre": app.genre,
            "platforms": app.platforms,
            "sources": sources,
        }
        if g.experiments():
            entry["experiments"] = g.experiments()
        cfg["apps"].append(entry)
        n_events = sum(len(v["event_name"]) for v in g.ev.groups.values())
        log(
            f"  generated {app.name}: {n_events:,} GA4 events, {len(g.rc_tx)} RevenueCat rows, {sum(len(v) for v in g.unity_lines.values())} Unity lines"
        )
    if sdk and apps is None:  # SDK games through a real collector (simulate/demo.py)
        from ..simulate.demo import add_sdk_games

        add_sdk_games(cfg, out_dir, end=end, scale=scale, log=log)
    if apps is None:  # one synthetic market-radar day for the Studio's market view
        from .market import write as write_market

        write_market(out_dir, end + dt.timedelta(days=1))
    path = out_dir / "pulse.yaml"
    path.write_text(
        "# Generated by `pulse demo`. Synthetic data only.\n"
        + yaml.safe_dump(cfg, sort_keys=False),
        encoding="utf-8",
    )
    return path
