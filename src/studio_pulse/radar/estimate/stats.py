"""Small statistics for the market estimates, without numpy (it is not a runtime dependency).

- `ols`: least squares y = a + s*x.
- `fit_power_law`: log(y) = a - b*log(rank) with a cluster bootstrap (resampling games, not
  rows: one game contributes several windows that are not independent). Each bootstrap
  replicate keeps its own residuals so a prediction band carries both the parameter
  uncertainty and the game-to-game scatter (a prediction interval, not a confidence interval
  of the mean curve).
- `quantile`: type-7 quantile (numpy's default).
- `log_uniform`: a draw uniform in log space between two positive bounds.
Every random draw comes from a seeded `random.Random`, so a run is reproducible.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field


def quantile(values: Sequence[float], q: float) -> float:
    xs = sorted(values)
    if not xs:
        raise ValueError("quantile of an empty sequence")
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def log_uniform(rng: random.Random, lo: float, hi: float) -> float:
    if lo <= 0 or hi <= 0:
        raise ValueError("log_uniform needs positive bounds")
    if hi < lo:
        lo, hi = hi, lo
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


def ols(x: Sequence[float], y: Sequence[float]) -> tuple[float, float]:
    """(intercept, slope) of the least-squares line; ValueError if x has no spread."""
    n = len(x)
    if n < 2 or n != len(y):
        raise ValueError("ols needs at least two points")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx <= 1e-12:
        raise ValueError("ols: x has no spread")
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True))
    s = sxy / sxx
    return my - s * mx, s


@dataclass
class Point:
    group: str  # the game (bootstrap cluster)
    rank: float  # mean rank over the window (geometric)
    y: float  # observed rate (per day)


@dataclass
class PowerLawFit:
    a: float  # log(y) at rank 1
    b: float  # y ~ rank^-b
    sigma: float  # residual sd (natural log), small-sample corrected
    n_groups: int
    n_points: int
    rank_min: float
    rank_max: float
    boots: list[tuple[float, float, list[float]]] = field(default_factory=list)

    @property
    def b_band(self) -> tuple[float, float]:
        bs = [b for _, b, _ in self.boots] or [self.b]
        return quantile(bs, 0.1), quantile(bs, 0.9)

    def point(self, rank: float) -> float:
        return math.exp(self.a - self.b * math.log(rank))

    def draws(self, rank: float, rng: random.Random, per_boot: int = 4) -> list[float]:
        """Predictive draws of log(y) at `rank`: bootstrap curve + one of its residuals."""
        lr = math.log(rank)
        out = []
        boots = self.boots or [(self.a, self.b, [0.0])]
        for a, b, res in boots:
            for _ in range(per_boot):
                out.append(a - b * lr + (rng.choice(res) if res else 0.0))
        return out


def _residuals(points: list[Point], a: float, b: float) -> list[float]:
    return [math.log(p.y) - (a - b * math.log(p.rank)) for p in points]


def fit_power_law(
    points: list[Point], *, boot: int = 400, rng: random.Random | None = None
) -> PowerLawFit:
    """log(y) = a - b log(rank) by least squares, with a cluster bootstrap over groups."""
    pts = [p for p in points if p.y > 0 and p.rank >= 1]
    x = [math.log(p.rank) for p in pts]
    z = [math.log(p.y) for p in pts]
    a, s = ols(x, z)
    b = -s
    res = _residuals(pts, a, b)
    n = len(pts)
    infl = math.sqrt(n / (n - 2)) if n > 2 else 1.0
    sigma = math.sqrt(sum(r * r for r in res) / max(1, n - 2)) if n > 2 else 0.0
    groups: dict[str, list[Point]] = {}
    for p in pts:
        groups.setdefault(p.group, []).append(p)
    keys = sorted(groups)
    fit = PowerLawFit(
        a=a,
        b=b,
        sigma=sigma,
        n_groups=len(keys),
        n_points=n,
        rank_min=min(p.rank for p in pts),
        rank_max=max(p.rank for p in pts),
    )
    rng = rng or random.Random(0)
    for _ in range(boot):
        sample = [q for _k in keys for q in groups[rng.choice(keys)]]
        try:
            ab, sb = ols([math.log(p.rank) for p in sample], [math.log(p.y) for p in sample])
        except ValueError:
            continue
        rb = [r * infl for r in _residuals(sample, ab, -sb)]
        fit.boots.append((ab, -sb, rb))
    return fit
