"""`radar.creatives` configuration: tracked advertisers, caps, tagging budget, score weights.

radar:
  creatives:
    enabled: true            # the weekly refresh inside `pulse run`
    every_days: 7            # Google Ads Transparency scan (BigQuery) at most this often
    max_gb: 30               # cap per BigQuery query (at most 50)
    per_advertiser: 120      # creatives kept per Google advertiser id and refresh
    genres: [puzzle, sort, casual]   # "our genres" for the summary strip
    advertisers:             # replaces nothing: added to the seed list (seed: false drops it)
      - {name: Example Games, genre: sort, google_names: [Example Games Ltd],
         meta_page_ids: ["123"], meta_terms: [Example Sort]}
    discover: {enabled: true, top: 12, days: 28}   # top publishers of the radar charts
    tagging: {enabled: true, max_per_run: 20, budget_usd: 0.10, video_seconds: 60}
    meta: {enabled: true, countries: [DE, FR, NL, PL, IT, ES], version: v24.0}
    tiktok: {enabled: false}   # needs an approved Commercial Content API app
    weights: {longevity: 0.35, active: 0.15, variants: 0.2, spread: 0.15, volume: 0.15}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ...config import Config, ConfigError

HARD_MAX_GB = 50.0  # a typo must not scan the whole 141 GB table
GENRES = ("puzzle", "sort", "casual", "word", "board", "other")
DEFAULT_WEIGHTS = {
    "longevity": 0.35,
    "active": 0.15,
    "variants": 0.20,
    "spread": 0.15,
    "volume": 0.15,
}

# Big casual / puzzle publishers (public company names). `google_names` are matched against
# the Ads Transparency advertiser name with legal-form words removed: equal, or the start of
# it ("Dream Games" matches "Dream Games Teknoloji A.S."). Meta pages are left to the config
# (page ids are looked up in the Ad Library by hand); without them Meta is searched by name.
SEED: tuple[dict[str, Any], ...] = (
    {"name": "Dream Games", "genre": "puzzle", "meta_terms": ["Royal Match", "Royal Kingdom"]},
    {"name": "King", "genre": "puzzle", "google_names": ["King.com"],
     "meta_terms": ["Candy Crush"]},
    {"name": "Playrix", "genre": "puzzle",
     "meta_terms": ["Homescapes", "Gardenscapes", "Fishdom", "Township"]},
    {"name": "Peak", "genre": "puzzle", "google_names": ["Peak Games", "Peak Oyun"],
     "meta_terms": ["Toon Blast", "Toy Blast", "Match Factory"]},
    {"name": "Easybrain", "genre": "puzzle", "meta_terms": ["Blockudoku"]},
    {"name": "Tripledot Studios", "genre": "puzzle", "meta_terms": ["Woodoku"]},
    {"name": "Magic Tavern", "genre": "puzzle", "meta_terms": ["Match Masters"]},
    {"name": "Hungry Studio", "genre": "puzzle", "meta_terms": ["Block Blast"]},
    {"name": "Grand Games", "genre": "puzzle"},
    {"name": "Good Job Games", "genre": "puzzle"},
    {"name": "Matchingham Games", "genre": "sort"},
    {"name": "Rollic Games", "genre": "sort"},
    {"name": "Moon Active", "genre": "casual", "meta_terms": ["Coin Master"]},
    {"name": "Scopely", "genre": "casual", "meta_terms": ["Monopoly Go"]},
    {"name": "Playtika", "genre": "casual"},
    {"name": "Voodoo", "genre": "casual"},
    {"name": "SayGames", "genre": "casual"},
    {"name": "Supersonic Studios", "genre": "casual"},
    {"name": "CrazyLabs", "genre": "casual", "google_names": ["CrazyLabs", "Crazy Labs"]},
    {"name": "Kwalee", "genre": "casual"},
)  # fmt: skip

_EU = ("DE", "FR", "NL", "PL", "IT", "ES")
_ID = re.compile(r"^AR\d{10,30}$")
_PAGE = re.compile(r"^\d{5,25}$")


@dataclass
class Advertiser:
    key: str
    name: str
    genre: str
    origin: str  # config | seed | charts
    google_names: tuple[str, ...] = ()
    google_ids: tuple[str, ...] = ()
    meta_page_ids: tuple[str, ...] = ()
    meta_terms: tuple[str, ...] = ()


@dataclass
class CreativeSettings:
    enabled: bool = False
    every_days: int = 7
    cap_bytes: int = 30_000_000_000
    resolve_cap_bytes: int = 15_000_000_000
    resolve_every_days: int = 30
    per_advertiser: int = 120
    lookback_days: int = 365
    active_days: int = 14
    genres: tuple[str, ...] = ("puzzle", "sort", "casual")
    advertisers: list[Advertiser] = field(default_factory=list)
    discover: bool = True
    discover_top: int = 12
    discover_days: int = 28
    tagging: bool = True
    tag_max_per_run: int = 20
    tag_budget_usd: float = 0.10
    video_seconds: int = 60
    meta: bool = True
    meta_countries: tuple[str, ...] = _EU
    meta_version: str = "v24.0"
    meta_max_pages: int = 2
    meta_every_days: int = 1
    tiktok: bool = False
    tiktok_countries: tuple[str, ...] = ("DE", "FR")
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


def slug(name: str) -> str:
    from ..adintel.google_atc import fold

    return "-".join(fold(name).split())[:60] or "x"


def _advertiser(raw: Any, origin: str) -> Advertiser:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        raise ConfigError("radar.creatives.advertisers: every entry needs a name")
    name = str(raw["name"]).strip()[:80]
    genre = str(raw.get("genre") or "other").lower()
    if genre not in GENRES:
        raise ConfigError(f"radar.creatives.advertisers: genre must be one of {', '.join(GENRES)}")
    ids = tuple(str(x) for x in raw.get("google_ids") or [])
    if any(not _ID.match(i) for i in ids):
        raise ConfigError("radar.creatives.advertisers: google_ids look like AR0123...")
    pages = tuple(str(x) for x in raw.get("meta_page_ids") or [])
    if any(not _PAGE.match(p) for p in pages):
        raise ConfigError("radar.creatives.advertisers: meta_page_ids are numeric page ids")
    return Advertiser(
        key=slug(name),
        name=name,
        genre=genre,
        origin=origin,
        google_names=tuple(str(x)[:80] for x in raw.get("google_names") or [name]),
        google_ids=ids,
        meta_page_ids=pages,
        meta_terms=tuple(str(x)[:100] for x in raw.get("meta_terms") or []),
    )


def from_config(cfg: Config) -> CreativeSettings:
    raw = dict((cfg.raw.get("radar") or {}).get("creatives") or {})
    gb = float(raw.get("max_gb", 30))
    rgb = float(raw.get("resolve_max_gb", 15))
    for k, v in (("max_gb", gb), ("resolve_max_gb", rgb)):
        if not 0 < v <= HARD_MAX_GB:
            raise ConfigError(f"radar.creatives.{k} must be in (0, {HARD_MAX_GB:g}]")
    tag = dict(raw.get("tagging") or {})
    meta = dict(raw.get("meta") or {})
    tik = dict(raw.get("tiktok") or {})
    disc = dict(raw.get("discover") or {})
    weights = dict(DEFAULT_WEIGHTS)
    for k, v in (raw.get("weights") or {}).items():
        if k not in DEFAULT_WEIGHTS or not 0 <= float(v) <= 1:
            raise ConfigError(f"radar.creatives.weights: unknown key or bad value {k!r}")
        weights[k] = float(v)
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        raise ConfigError("radar.creatives.weights must add up to 1")
    advs: dict[str, Advertiser] = {}
    if raw.get("seed", True):
        for a in SEED:
            ad = _advertiser(a, "seed")
            advs[ad.key] = ad
    for a in raw.get("advertisers") or []:
        ad = _advertiser(a, "config")
        advs[ad.key] = ad  # the config wins over a seed of the same name
    budget = float(tag.get("budget_usd", 0.10))
    if not 0 <= budget <= 5:
        raise ConfigError("radar.creatives.tagging.budget_usd must be in [0, 5]")
    genres = tuple(str(g) for g in raw.get("genres") or ("puzzle", "sort", "casual"))
    if any(g not in GENRES for g in genres):
        raise ConfigError(f"radar.creatives.genres: one of {', '.join(GENRES)}")
    countries = tuple(str(c).upper() for c in meta.get("countries") or _EU)
    if any(not re.fullmatch(r"[A-Z]{2}", c) for c in countries):
        raise ConfigError("radar.creatives.meta.countries are two-letter country codes")
    version = str(meta.get("version", "v24.0"))
    if not re.fullmatch(r"v\d{2}\.\d", version):
        raise ConfigError("radar.creatives.meta.version looks like v24.0")
    return CreativeSettings(
        enabled=bool(raw.get("enabled", False)),
        every_days=int(raw.get("every_days", 7)),
        cap_bytes=int(gb * 1e9),
        resolve_cap_bytes=int(rgb * 1e9),
        resolve_every_days=int(raw.get("resolve_every_days", 30)),
        per_advertiser=max(1, min(int(raw.get("per_advertiser", 120)), 1000)),
        lookback_days=max(30, min(int(raw.get("lookback_days", 365)), 730)),
        active_days=max(1, int(raw.get("active_days", 14))),
        genres=genres,
        advertisers=sorted(advs.values(), key=lambda a: a.key),
        discover=bool(disc.get("enabled", True)),
        discover_top=max(0, min(int(disc.get("top", 12)), 50)),
        discover_days=max(1, int(disc.get("days", 28))),
        tagging=bool(tag.get("enabled", True)),
        tag_max_per_run=max(0, min(int(tag.get("max_per_run", 20)), 500)),
        tag_budget_usd=budget,
        video_seconds=max(5, min(int(tag.get("video_seconds", 60)), 180)),
        meta=bool(meta.get("enabled", True)),
        meta_countries=countries,
        meta_version=version,
        meta_max_pages=max(1, min(int(meta.get("max_pages", 2)), 10)),
        meta_every_days=max(1, int(meta.get("every_days", 1))),
        tiktok=bool(tik.get("enabled", False)),
        tiktok_countries=tuple(str(c).upper() for c in tik.get("countries") or ("DE", "FR")),
        weights=weights,
    )
