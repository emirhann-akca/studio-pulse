"""The `radar.estimates:` block (docs/RADAR.md "Market estimates").

radar:
  estimates:
    enabled: true              # compute download/revenue bands after every radar run
    min_games: 10              # a list's download curve needs this many games with windows
    min_window_days: 7         # Play counter windows shorter than this are not used
    max_window_days: 35
    max_ratings_per_install: 0.25  # more new Play ratings than this per new install: stale counter
    bootstrap: 400             # cluster-bootstrap replicates per curve
    draws: 2000                # Monte Carlo draws for the revenue band
    band: [0.1, 0.9]           # quantiles shown (an 80% band)
    panel_size: 80             # charted games whose Play page is read daily for ground truth
    play_reserve: 10           # Play requests left unused for the next run of the day
    seed: 7
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...config import Config, ConfigError


@dataclass
class EstimateSettings:
    enabled: bool = True
    min_games: int = 10
    min_window_days: float = 7.0
    max_window_days: float = 35.0
    max_ratings_per_install: float = 0.25
    min_listed_share: float = 0.5
    bootstrap: int = 400
    draws: int = 2000
    band_lo: float = 0.1
    band_hi: float = 0.9
    panel_size: int = 80
    play_reserve: int = 10
    seed: int = 7

    @property
    def band_pct(self) -> int:
        return round((self.band_hi - self.band_lo) * 100)


def from_raw(raw: dict[str, Any] | None) -> EstimateSettings:
    raw = dict(raw or {})
    e = EstimateSettings(enabled=bool(raw.get("enabled", True)))
    for key in (
        "min_games",
        "bootstrap",
        "draws",
        "panel_size",
        "play_reserve",
        "seed",
    ):
        if key in raw:
            setattr(e, key, int(raw[key]))
    for key in (
        "min_window_days",
        "max_window_days",
        "max_ratings_per_install",
        "min_listed_share",
    ):
        if key in raw:
            setattr(e, key, float(raw[key]))
    if "band" in raw:
        lo, hi = (float(x) for x in raw["band"])
        if not 0 < lo < hi < 1:
            raise ConfigError("radar.estimates.band must be two quantiles 0 < low < high < 1")
        e.band_lo, e.band_hi = lo, hi
    if e.min_games < 3:
        raise ConfigError("radar.estimates.min_games must be at least 3")
    if e.min_window_days < 1 or e.max_window_days <= e.min_window_days:
        raise ConfigError("radar.estimates: need 1 <= min_window_days < max_window_days")
    if e.bootstrap < 50 or e.draws < 200:
        raise ConfigError("radar.estimates: bootstrap >= 50 and draws >= 200")
    if e.panel_size < 0 or e.play_reserve < 0:
        raise ConfigError("radar.estimates: panel_size and play_reserve must be >= 0")
    return e


def from_config(cfg: Config) -> EstimateSettings:
    return from_raw((cfg.raw.get("radar") or {}).get("estimates"))
