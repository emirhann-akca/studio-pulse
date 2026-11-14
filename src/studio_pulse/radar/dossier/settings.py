"""The `radar.dossier:` block of the config (docs/RADAR.md, "Game dossier").

radar:
  dossier:
    studio:                       # who "we" are in "should we make it?" (defaults below:
      team_size: 2                #  a 2-person Unity casual/puzzle studio; ASSUMPTIONS to edit)
      engine: unity
      genres: [puzzle, casual]    # families we ship (puzzle, casual, simulation, arcade ...)
      strengths: [color-sort, goods-sort, hexa-sort, block-puzzle, merge, physics-draw, runner]
      art: [2d, stylized-3d]      # art styles we can produce
      monetization: [ads, hybrid] # models we know how to run
      avoid: [pvp-race, battle-pass]   # twists/features we will not build
      months: 3                   # longest first version we accept
      budget_usd: 3000            # first test plus a soft launch
      target_markets: [us]        # where the game would earn
      test_market: br             # where the cheap creative test runs
      test_platform: android
      launch_platform: android    # the platform whose CPI the rubric checks
      max_cpi_usd: auto           # the CPI our economics can carry; auto = the benchmark CPI
                                  #   of hyper-casual in the first target market (US Android
                                  #   1.55 USD, AppsFlyer Q3 2026): an ads-first hybrid-casual
                                  #   studio's lifetime value is in that range, not puzzle's
      ipm_gate: 8                 # installs per 1,000 impressions a creative must reach
    rubric: {weights: {momentum: 0.2, ...}, yap_min: 0.62, bekle_min: 0.45, cpi_cap_ratio: 2}
    test: {variants: 4, installs_per_variant: 40, days: 4, min_per_variant_usd: 25}
    screenshots: 6
    reviews: true
    peer_reviews: 3               # genre peers whose Play reviews are mined for comparison
    countries: [us, gb]           # Lookup storefronts (default: the radar's countries)
    ads:
      google_atc: true            # read the local weekly extract (radar.adintel, see google_atc.py)
      meta: {countries: [DE, FR], version: v24.0}   # token: PULSE_META_AD_LIBRARY_TOKEN
    effort: {base: {color-sort: 2.0}}   # overrides benchmarks.yaml effort
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ...config import Config, ConfigError
from ..settings import RadarSettings
from ..settings import from_config as radar_from_config

# A 2-person Unity studio making casual and puzzle games: sort puzzles, light merge, hyper- and
# hybrid-casual. Every value is an assumption the owner edits under radar.dossier.studio.
DEFAULT_STUDIO: dict[str, Any] = {
    "team_size": 2,
    "engine": "unity",
    "genres": ["puzzle", "casual"],
    "strengths": [
        "color-sort",
        "goods-sort",
        "hexa-sort",
        "block-puzzle",
        "merge",  # merge-lite: a merge step inside a puzzle, not a merge-2 meta game
        "physics-draw",
        "runner",
    ],
    "art": ["2d", "stylized-3d"],
    "monetization": ["ads", "hybrid"],
    "avoid": ["pvp-race", "battle-pass"],
    "months": 3,
    "budget_usd": 3000,
    "target_markets": ["us"],
    "test_market": "br",
    "test_platform": "android",
    "launch_platform": "android",
    "max_cpi_usd": "auto",
    "ipm_gate": 8,
}
DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 0.20,
    "saturation": 0.15,
    "monetization_fit": 0.15,
    "capability_fit": 0.25,
    "production": 0.15,
    "cpi_risk": 0.10,
}
DEFAULT_TEST: dict[str, Any] = {
    "variants": 4,
    "installs_per_variant": 40,
    "days": 4,
    "min_per_variant_usd": 25,
    "safety": 1.2,
}


@functools.lru_cache(maxsize=2)
def load_benchmarks() -> dict[str, Any]:
    text = (
        resources.files("studio_pulse.radar.dossier")
        .joinpath("benchmarks.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text) or {}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@dataclass
class DossierSettings:
    radar: RadarSettings
    studio: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_STUDIO))
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    yap_min: float = 0.62
    bekle_min: float = 0.45
    cpi_cap_ratio: float = 2.0
    test: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_TEST))
    screenshots: int = 6
    reviews: bool = True
    peer_reviews: int = 3
    countries: list[str] = field(default_factory=list)
    google_atc: bool = True
    meta_countries: list[str] = field(default_factory=lambda: ["DE", "FR"])
    meta_version: str = "v24.0"
    benchmarks: dict[str, Any] = field(default_factory=load_benchmarks)
    out_dir: Path = Path("local/radar/dossier")

    @property
    def primary_country(self) -> str:
        return self.countries[0] if self.countries else "us"


def from_config(cfg: Config, radar: RadarSettings | None = None) -> DossierSettings:
    rs = radar or radar_from_config(cfg)
    raw = dict((cfg.raw.get("radar") or {}).get("dossier") or {})
    s = DossierSettings(radar=rs)
    s.studio = _merge(DEFAULT_STUDIO, raw.get("studio") or {})
    rub = raw.get("rubric") or {}
    s.weights = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (rub.get("weights") or {}).items()}}
    unknown = set(s.weights) - set(DEFAULT_WEIGHTS)
    if unknown:
        raise ConfigError(f"radar.dossier.rubric.weights: unknown keys {sorted(unknown)}")
    if sum(s.weights.values()) <= 0:
        raise ConfigError("radar.dossier.rubric.weights must not all be zero")
    s.yap_min = float(rub.get("yap_min", 0.62))
    s.bekle_min = float(rub.get("bekle_min", 0.45))
    s.cpi_cap_ratio = float(rub.get("cpi_cap_ratio", 2.0))
    if not 0 <= s.bekle_min <= s.yap_min <= 1:
        raise ConfigError("radar.dossier.rubric: need 0 <= bekle_min <= yap_min <= 1")
    s.test = {**DEFAULT_TEST, **(raw.get("test") or {})}
    s.screenshots = max(0, min(10, int(raw.get("screenshots", 6))))
    s.reviews = bool(raw.get("reviews", True))
    s.peer_reviews = max(0, min(5, int(raw.get("peer_reviews", 3))))
    s.countries = [str(c).lower() for c in raw.get("countries") or rs.countries]
    ads = raw.get("ads") or {}
    s.google_atc = bool(ads.get("google_atc", True))
    meta = ads.get("meta") or {}
    s.meta_countries = [str(c).upper() for c in meta.get("countries") or ["DE", "FR"]]
    s.meta_version = str(meta.get("version", "v24.0"))
    bm = load_benchmarks()
    if raw.get("effort"):
        bm = _merge(bm, {"effort": raw["effort"]})
    if raw.get("cpi"):
        bm = _merge(bm, {"cpi": raw["cpi"]})
    s.benchmarks = bm
    s.studio.update(resolve_max_cpi(s.studio, bm))
    s.out_dir = rs.out_dir / "dossier"
    return s


def resolve_max_cpi(studio: dict[str, Any], bm: dict[str, Any]) -> dict[str, Any]:
    """`max_cpi_usd: auto` -> the hyper-casual benchmark CPI of the first target market on the
    launch platform (what an ads-first hybrid-casual game can pay); a number stays as given."""
    v = studio.get("max_cpi_usd", "auto")
    if v not in (None, "auto"):
        return {"max_cpi_usd": float(v), "max_cpi_source": "config"}
    cc = str((studio.get("target_markets") or ["us"])[0]).lower()
    plat = str(studio.get("launch_platform", "android"))
    row = ((bm.get("cpi") or {}).get("values") or {}).get("hypercasual", {}).get(cc) or [None, None]
    cpi = row[0 if plat == "ios" else 1]
    if cpi is None:
        return {"max_cpi_usd": 1.5, "max_cpi_source": "fallback (no hyper-casual benchmark)"}
    src = (bm.get("cpi") or {}).get("source", "benchmark")
    return {
        "max_cpi_usd": float(cpi),
        "max_cpi_source": f"auto: hyper-casual CPI {cc.upper()} {plat} ({src})",
    }
