"""The `radar:` block of the config (docs/RADAR.md has the full reference).

radar:
  enabled: true                 # run inside every `pulse run` (and so the daily schedule)
  countries: [us, jp, kr, de, gb, fr, ca, au, br, tr]   # Apple storefront codes (default)
  charts:                       # Apple legacy RSS lists; `countries` narrows one list
    - {chart: topfreeapplications, genre: 6014}
    - {chart: topfreeapplications, genre: 7012}
    - {chart: topfreeapplications, genre: 7003}
    - {chart: topfreeapplications, genre: 7015}
    - {chart: topgrossingapplications, genre: 6014}
  limit: 100
  publishers: [{id: "1743349083", name: ABI Global}]   # artistIds, checked weekly
  apple: {spacing_s: 3, daily_budget: 200, lookup_batch: 100}
  play: {enabled: true, country: us, lang: en, spacing_s: 3, daily_budget: 200,
         map_file: play_map.yaml, watch: [com.example.package]}
  llm: {max_apps: 25, screenshots: 0, narrate: true}   # provider: top-level `llm:` block
  params: {breakout_min: 0.35}  # any scoring parameter (fct_mr_breakout_daily.sql)
  out_dir: local/radar          # digest, facts and market_trends.yaml (kept local)
  creative_forge_library: ~/Library/Application Support/creative-forge/library
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config, ConfigError

# Ten large game markets (App Store storefront codes). With the five default lists that is
# 50 RSS + ~35 Lookup requests a day to itunes.apple.com (docs/RADAR.md, "Request volume").
DEFAULT_COUNTRIES = ["us", "jp", "kr", "de", "gb", "fr", "ca", "au", "br", "tr"]
_STOREFRONT = re.compile(r"^[a-z]{2}$")
DEFAULT_CHARTS = [
    {"chart": "topfreeapplications", "genre": "6014"},
    {"chart": "topfreeapplications", "genre": "7012"},
    {"chart": "topfreeapplications", "genre": "7003"},
    {"chart": "topfreeapplications", "genre": "7015"},
    {"chart": "topgrossingapplications", "genre": "6014"},
]
DEFAULT_PARAMS: dict[str, float] = {
    "w_vel": 0.40,
    "w_climb": 0.25,
    "w_rank": 0.15,
    "w_spread": 0.10,
    "w_new": 0.10,
    "rv_saturation": 200,
    "iv_saturation": 50000,
    "climb_saturation": 3,
    "spread_saturation": 4,
    "climb_days": 7,
    "spread_days": 7,
    "new_days": 7,
    "new_lookback_days": 30,
    "age_max": 60,
    "late_age_max": 120,
    "breakout_min": 0.35,
    "watch_min": 0.20,
    "gate_climb": 1,
    "gate_rv": 10,
    "gate_iv": 1000,
    "trend_delta_min": 0.02,
    "trend_z_min": 2,
    "trend_min_apps": 3,
    "trend_prior_weeks": 4,
}


@dataclass
class ChartSpec:
    chart: str
    genre: str
    limit: int
    countries: list[str] | None = None

    @property
    def key(self) -> str:
        return f"{self.chart}:{self.genre}"


@dataclass
class RadarSettings:
    enabled: bool = False
    countries: list[str] = field(default_factory=lambda: list(DEFAULT_COUNTRIES))
    charts: list[ChartSpec] = field(default_factory=list)
    publishers: list[dict[str, str]] = field(default_factory=list)
    publisher_every_days: int = 7
    apple_spacing_s: float = 3.0
    apple_budget: int = 200
    lookup_batch: int = 100
    play_enabled: bool = True
    play_country: str = "us"
    play_lang: str = "en"
    play_spacing_s: float = 3.0
    play_budget: int = 200
    play_map_file: Path | None = None
    play_watch: list[str] = field(default_factory=list)
    play_retry_days: int = 14
    llm_max_apps: int = 25
    llm_screenshots: int = 0
    narrate: bool = False
    trend_list: str = "topfreeapplications:6014"
    params: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PARAMS))
    out_dir: Path = Path("local/radar")
    cache_keep_days: int = 3
    creative_forge_library: Path | None = None
    # Recorded responses instead of the network (tests and offline demos only).
    replay: Path | None = None
    # Snapshot date override (replays of a recorded day); default: config as_of, else today.
    as_of: dt.date | None = None

    def charts_for(self, country: str) -> list[ChartSpec]:
        return [c for c in self.charts if not c.countries or country in c.countries]

    @property
    def primary_country(self) -> str:
        return self.countries[0] if self.countries else "us"


def _path(cfg: Config, v: Any) -> Path | None:
    return cfg.resolve(str(v)) if v not in (None, "") else None


def from_config(cfg: Config) -> RadarSettings:
    raw = dict(cfg.raw.get("radar") or {})
    s = RadarSettings(enabled=bool(raw.get("enabled", False)))
    s.countries = list(
        dict.fromkeys(str(c).lower() for c in raw.get("countries") or DEFAULT_COUNTRIES)
    )
    bad = [c for c in s.countries if not _STOREFRONT.match(c)]
    if bad:
        raise ConfigError(f"radar.countries: not two-letter storefront codes: {bad}")
    limit = int(raw.get("limit", 100))
    if not 1 <= limit <= 100:
        raise ConfigError("radar.limit must be between 1 and 100 (the feed's maximum)")
    s.charts = [
        ChartSpec(
            chart=str(c["chart"]),
            genre=str(c.get("genre", "6014")),
            limit=int(c.get("limit", limit)),
            countries=[str(x).lower() for x in c["countries"]] if c.get("countries") else None,
        )
        for c in (raw.get("charts") or DEFAULT_CHARTS)
    ]
    s.publishers = [
        {"id": str(p["id"]), "name": str(p.get("name", p["id"]))}
        for p in raw.get("publishers") or []
    ]
    s.publisher_every_days = int(raw.get("publisher_every_days", 7))
    apple = raw.get("apple") or {}
    s.apple_spacing_s = float(apple.get("spacing_s", 3.0))
    s.apple_budget = int(apple.get("daily_budget", 200))
    s.lookup_batch = int(apple.get("lookup_batch", 100))
    play = raw.get("play") or {}
    s.play_enabled = bool(play.get("enabled", True))
    s.play_country = str(play.get("country", "us")).lower()
    s.play_lang = str(play.get("lang", "en"))
    s.play_spacing_s = max(3.0, float(play.get("spacing_s", 3.0)))
    s.play_budget = min(200, int(play.get("daily_budget", 200)))
    s.play_map_file = _path(cfg, play.get("map_file"))
    s.play_watch = [str(x) for x in play.get("watch") or []]
    s.play_retry_days = int(play.get("retry_days", 14))
    llm = raw.get("llm") or {}
    s.llm_max_apps = int(llm.get("max_apps", 25))
    s.llm_screenshots = max(0, min(6, int(llm.get("screenshots", 0))))
    s.narrate = bool(llm.get("narrate", False))
    s.trend_list = str(raw.get("trend_list", "topfreeapplications:6014"))
    need = sum(len(s.charts_for(cc)) for cc in s.countries) + len(s.countries)
    if need > s.apple_budget:
        raise ConfigError(
            f"radar: {len(s.countries)} countries need at least {need} Apple requests a day "
            f"(lists + one Lookup each) but radar.apple.daily_budget is {s.apple_budget}"
        )
    s.params = {**DEFAULT_PARAMS, **{k: float(v) for k, v in (raw.get("params") or {}).items()}}
    unknown = set(s.params) - set(DEFAULT_PARAMS)
    if unknown:
        raise ConfigError(f"radar.params: unknown keys {sorted(unknown)}")
    s.out_dir = _path(cfg, raw.get("out_dir")) or cfg.warehouse.parent / "radar"
    s.cache_keep_days = int(raw.get("cache_keep_days", 3))
    s.creative_forge_library = _path(cfg, raw.get("creative_forge_library"))
    s.replay = _path(cfg, raw.get("replay"))
    if raw.get("as_of"):
        s.as_of = dt.date.fromisoformat(str(raw["as_of"]))
    return s
