"""Small, dependency-free statistics for the insight jobs.

Each function names its reference; tests check them against scipy /
statsmodels (already dev dependencies) or a published example.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..stats.dist import chi2_sf, norm_ppf, norm_sf_two_sided

Z95 = 1.959963984540054


def wilson(successes: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion (NIST e-Handbook 7.2.4.1)."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class TwoProp:
    p1: float
    p2: float
    diff: float  # p2 - p1
    ci: tuple[float, float]  # Wald interval of the difference (unpooled)
    z: float
    p_value: float
    relative: float | None  # p2 / p1 - 1


def two_proportions(x1: float, n1: float, x2: float, n2: float) -> TwoProp | None:
    """Two-proportion z-test with the pooled variance (NIST 7.3.3); 1 = baseline, 2 = test."""
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = x1 / n1, x2 / n2
    pool = (x1 + x2) / (n1 + n2)
    se0 = math.sqrt(max(pool * (1 - pool) * (1 / n1 + 1 / n2), 0.0))
    se = math.sqrt(max(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2, 0.0))
    diff = p2 - p1
    if se0 == 0:
        z, p = 0.0, 1.0
    else:
        z = diff / se0
        p = norm_sf_two_sided(z)
    rel = (p2 / p1 - 1) if p1 > 0 else None
    return TwoProp(p1, p2, diff, (diff - Z95 * se, diff + Z95 * se), z, p, rel)


def benjamini_hochberg(p_values: Sequence[float | None]) -> list[float | None]:
    """BH-adjusted q-values (Benjamini & Hochberg 1995), None passes through."""
    idx = [i for i, p in enumerate(p_values) if p is not None]
    m = len(idx)
    out: list[float | None] = [None] * len(p_values)
    if not m:
        return out
    order = sorted(idx, key=lambda i: p_values[i])  # type: ignore[arg-type,return-value]
    q_prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        q = min(q_prev, float(p_values[i]) * m / rank)  # type: ignore[arg-type]
        out[i] = q
        q_prev = q
    return out


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return math.nan
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def mad(xs: Sequence[float]) -> float:
    """Median absolute deviation scaled to a normal SD (x 1.4826)."""
    m = median(xs)
    return 1.4826 * median([abs(x - m) for x in xs])


def robust_z(x: float, centre: float, scale: float) -> float | None:
    return None if scale <= 0 else (x - centre) / scale


def median_ci(xs: Sequence[float], z: float = Z95) -> tuple[float, float] | None:
    """Distribution-free interval for the median from order statistics (normal approx. to the binomial)."""
    s = sorted(xs)
    n = len(s)
    if n < 6:
        return None
    half = z * math.sqrt(n) / 2
    lo = max(0, math.floor(n / 2 - half))
    hi = min(n - 1, math.ceil(n / 2 + half) - 1)
    return (s[lo], s[hi])


def mann_whitney(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Mann-Whitney U of a vs b with tie correction, two-sided normal p (scipy method='asymptotic', no continuity)."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return (math.nan, 1.0)
    pooled = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks = [0.0] * len(pooled)
    ties = 0.0
    i = 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        t = j - i + 1
        ties += t**3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, pooled, strict=True) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    mu = n1 * n2 / 2
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))
    if var <= 0:
        return (u1, 1.0)
    z = (u1 - mu) / math.sqrt(var)
    return (u1, norm_sf_two_sided(z))


def js_divergence(p: float, q: float) -> float:
    """Adtributor's per-element surprise: Jensen-Shannon divergence of two shares (bits)."""

    def term(a: float, m: float) -> float:
        return 0.0 if a <= 0 or m <= 0 else a * math.log2(a / m)

    m = (p + q) / 2
    return 0.5 * (term(p, m) + term(q, m))


@dataclass(frozen=True)
class KMPoint:
    step: int
    at_risk: float
    events: float
    censored: float
    survival: float
    ci: tuple[float, float]


def kaplan_meier(rows: Sequence[tuple[int, float, float, float]]) -> list[KMPoint]:
    """Kaplan-Meier from aggregated counts (step, at_risk, events, censored), Greenwood CI.

    The interval uses the log(-log) transform (lifelines' default), which
    stays inside [0, 1].
    """
    s = 1.0
    green = 0.0
    out: list[KMPoint] = []
    for step, n, d, c in sorted(rows):
        if n <= 0:
            continue
        s *= 1 - d / n
        if n > d:
            green += d / (n * (n - d))
        if 0 < s < 1 and green > 0:
            se = math.sqrt(green) / abs(math.log(s))
            lo = s ** math.exp(Z95 * se)
            hi = s ** math.exp(-Z95 * se)
        else:
            lo = hi = s
        out.append(KMPoint(step, n, d, c, s, (lo, hi)))
    return out


def logrank(
    a: Sequence[tuple[int, float, float]], b: Sequence[tuple[int, float, float]]
) -> tuple[float, float]:
    """Log-rank chi-square and p for two aggregated curves [(step, at_risk, events)]."""
    da = {s: (n, d) for s, n, d in a}
    db = {s: (n, d) for s, n, d in b}
    o_minus_e = 0.0
    var = 0.0
    for step in sorted(set(da) | set(db)):
        n1, d1 = da.get(step, (0.0, 0.0))
        n2, d2 = db.get(step, (0.0, 0.0))
        n, d = n1 + n2, d1 + d2
        if n <= 1 or d == 0 or n1 == 0 or n2 == 0:
            continue
        e1 = d * n1 / n
        o_minus_e += d1 - e1
        var += d * (n1 / n) * (n2 / n) * (n - d) / (n - 1)
    if var <= 0:
        return (0.0, 1.0)
    chi2 = o_minus_e**2 / var
    return (chi2, chi2_sf(chi2, 1))


def welch_mean_diff(
    a: Sequence[float], b: Sequence[float]
) -> tuple[float, tuple[float, float], float] | None:
    """Difference of means b - a with a Welch interval and p (normal approximation for df > 30)."""
    from ..stats.dist import t_ppf, t_sf_two_sided

    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None
    m1, m2 = sum(a) / n1, sum(b) / n2
    v1 = sum((x - m1) ** 2 for x in a) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in b) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    diff = m2 - m1
    if se == 0:
        return (diff, (diff, diff), 1.0 if diff == 0 else 0.0)
    df = (v1 / n1 + v2 / n2) ** 2 / (
        (v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1) or 1e-300
    )
    t = diff / se
    q = t_ppf(0.975, df)
    return (diff, (diff - q * se, diff + q * se), t_sf_two_sided(t, df))


__all__ = [
    "Z95",
    "TwoProp",
    "KMPoint",
    "benjamini_hochberg",
    "js_divergence",
    "kaplan_meier",
    "logrank",
    "mad",
    "mann_whitney",
    "median",
    "median_ci",
    "norm_ppf",
    "robust_z",
    "two_proportions",
    "welch_mean_diff",
    "wilson",
]
