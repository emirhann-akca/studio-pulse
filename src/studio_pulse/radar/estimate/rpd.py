"""Revenue from downloads: the radar's default revenue estimate (revenue_per_download.yaml).

No compliant source publishes a top-grossing rank (apps.apple.com has no grossing chart; the
legacy iTunes RSS is robots-disallowed), so revenue is never read off a grossing rank by
default. Instead, for a game with a download band:

    revenue/day = Android installs/day x RPD_android(g) + iOS downloads/day x RPD_ios(g)
                + (Android + iOS installs/day) x ad revenue per install   (if it shows ads)

g is the game's genre from its Apple Lookup genres, or its subgenre when the radar's profile
names match-3, merge or tile-match. RPD values are 2025 worldwide genre averages (AppMagic, net
of store fees, grossed up by 1/0.85-1/0.70), split by store like the whole market (Sensor
Tower: App Store 4.15x, Google Play 0.44x of the two-store average). A game is drawn anywhere
from a third to three times its genre's average (an assumption; five-fold for an unknown
genre). Ad revenue per install is AppsFlyer's D90 ad ARPU to twice that, and is added only
when the Play page says the game has ads (half the time, so to speak, when it is unknown: the
range then starts at 0). The result is gross consumer spend plus ad revenue, worldwide.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any

import duckdb
import yaml

from .stats import log_uniform


def load() -> dict[str, Any]:
    text = (
        resources.files("studio_pulse.radar.estimate")
        .joinpath("revenue_per_download.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


@dataclass
class GameProfile:
    genre: str  # radar genre key, or "all"
    basis: str  # "mechanic:match-3", "apple:Puzzle" or "unknown"
    ads: bool | None  # Play page containsAds; None = unknown


def genre_of(doc: dict[str, Any], apple_genres: list[str], mechanic: str | None) -> GameProfile:
    if mechanic and mechanic in (doc.get("mechanic_rpd_net") or {}).get("values", {}):
        return GameProfile(mechanic, f"mechanic:{mechanic}", None)
    for g in apple_genres:
        key = (doc.get("apple_genre") or {}).get(g)
        if key:
            return GameProfile(key, f"apple:{g}", None)
    return GameProfile("all", "unknown", None)


def profiles(con: duckdb.DuckDBPyConnection, doc: dict[str, Any]) -> dict[str, GameProfile]:
    """Genre profile per Apple id from the latest Lookup genres, the radar's mechanic tag and
    the Play page's ads flag (same bundle id or the verified mapping)."""
    rows = con.execute(
        """
        WITH g AS (
          SELECT app_id, genres, bundle_id FROM raw_mr_app WHERE store = 'apple'
          QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC) = 1
        ), p AS (
          SELECT app_id, contains_ads FROM raw_mr_app WHERE store = 'play'
          QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC) = 1
        ), x AS (
          SELECT apple_id, any_value(play_id) AS play_id FROM raw_mr_xref
          WHERE status = 'mapped' GROUP BY 1
        )
        SELECT g.app_id, g.genres, p.contains_ads
        FROM g LEFT JOIN x ON x.apple_id = g.app_id
        LEFT JOIN p ON p.app_id = COALESCE(x.play_id, g.bundle_id)
        """
    ).fetchall()
    mech: dict[str, str] = {}
    try:
        for app_key, value in con.execute(
            "SELECT app_key, value FROM fct_mr_app_tags WHERE dimension = 'mechanic_primary'"
        ).fetchall():
            mech[str(app_key).split(":", 1)[-1]] = value
    except duckdb.Error:
        pass
    out: dict[str, GameProfile] = {}
    for app_id, genres, ads in rows:
        try:
            gl = [str(x) for x in json.loads(genres or "[]")]
        except ValueError:
            gl = []
        prof = genre_of(doc, [x for x in gl if x != "Games"], mech.get(app_id))
        prof.ads = None if ads is None else bool(ads)
        out[app_id] = prof
    return out


def revenue_sampler(
    doc: dict[str, Any], prof: GameProfile
) -> Callable[[random.Random, float, float], float]:
    """f(rng, android_per_day, ios_per_day) -> one draw of revenue per day (USD)."""
    mech = (doc.get("mechanic_rpd_net") or {}).get("values", {})
    genre = (doc.get("genre_rpd_net") or {}).get("values", {})
    net = mech.get(prof.genre, genre.get(prof.genre))
    gross_lo, gross_hi = (float(x) for x in doc["net_to_gross"])
    spread = doc["game_spread"] if net is not None else doc["game_spread_unknown_genre"]
    pf = doc["platform_factor"]
    group = (doc.get("genre_ad_group") or {}).get(prof.genre) or (
        doc.get("genre_ad_group") or {}
    ).get("all", "casual")
    ad = float(doc["ad_arpu_d90"]["values"][group])
    life_lo, life_hi = (float(x) for x in doc["ad_arpu_d90"]["lifetime_factor"])

    def draw(rng: random.Random, android: float, ios: float) -> float:
        base = (
            net * log_uniform(rng, gross_lo, gross_hi)
            if net is not None
            else float(doc["all_games_rpd_gross"])
        )
        rpd = base * log_uniform(rng, float(spread[0]), float(spread[1]))
        iap = android * rpd * float(pf["android"]) + ios * rpd * float(pf["ios"])
        if prof.ads is False:
            return iap
        ads = (android + ios) * ad * log_uniform(rng, life_lo, life_hi)
        if prof.ads is None:
            ads *= rng.random()  # unknown: anywhere from no ads to full ads
        return iap + ads

    return draw


def method_text(prof: GameProfile) -> str:
    ads = {True: "ads", False: "no ads", None: "ads unknown"}[prof.ads]
    return (
        f"downloads x revenue per download ({prof.genre}, {prof.basis}; AppMagic/Sensor Tower "
        f"2025) + ad revenue per install (AppsFlyer D90, {ads})"
    )


def safe_log(x: float) -> float:
    return math.log(max(x, 1e-9))
