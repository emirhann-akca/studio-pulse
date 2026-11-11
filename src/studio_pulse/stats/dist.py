"""Distribution functions with the standard library only.

Student t and chi-square need the regularised incomplete beta and gamma
functions; they are computed with the continued fractions and series of
Numerical Recipes (Press et al., 3rd ed., sections 6.2 and 6.4) and checked
against scipy.stats in tests/test_stats_dist.py. The normal distribution
comes from statistics.NormalDist / math.erfc.
"""

from __future__ import annotations

import math
from statistics import NormalDist

_TINY = 1e-300
_EPS = 1e-15
_N = NormalDist()


def norm_cdf(z: float) -> float:
    return _N.cdf(z)


def norm_ppf(p: float) -> float:
    return _N.inv_cdf(p)


def norm_sf_two_sided(z: float) -> float:
    """P(|Z| >= |z|), accurate far into the tail (erfc instead of 1 - cdf)."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > _TINY else _TINY)
    h = d
    for m in range(1, 20000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            return h
    raise ArithmeticError("incomplete beta did not converge")


def _lgamma_half_ratio(a: float) -> float:
    """ln Gamma(a + 1/2) - ln Gamma(a), without the cancellation of two large lgamma values.

    Stirling's series for both terms, combined so that only small numbers are
    subtracted (accurate to ~1e-15 for a >= 20).
    """
    h = a + 0.5
    return (
        a * math.log1p(0.5 / a)
        + 0.5 * math.log(a)
        - 0.5
        + (1 / (12 * h) - 1 / (12 * a))
        - (1 / (360 * h**3) - 1 / (360 * a**3))
        + (1 / (1260 * h**5) - 1 / (1260 * a**5))
    )


def betainc(a: float, b: float, x: float, xc: float | None = None) -> float:
    """Regularised incomplete beta function I_x(a, b); xc = 1 - x when known more exactly."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    xc = 1.0 - x if xc is None else xc
    if b == 0.5 and a >= 20:
        lbeta = _lgamma_half_ratio(a) - math.lgamma(0.5)
    elif a == 0.5 and b >= 20:
        lbeta = _lgamma_half_ratio(b) - math.lgamma(0.5)
    else:
        lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    # log x and log(1 - x) from whichever of x, 1 - x is small: a * log(x) with a ~ 1e6
    # and x near 1 would otherwise lose ~10 digits.
    lx = math.log1p(-xc) if xc < 0.5 else math.log(x)
    lxc = math.log1p(-x) if x < 0.5 else math.log(xc)
    front = math.exp(lbeta + a * lx + b * lxc)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, xc) / b


def t_cdf(t: float, df: float) -> float:
    if math.isinf(df):
        return norm_cdf(t)
    tail = 0.5 * betainc(df / 2.0, 0.5, df / (df + t * t), t * t / (df + t * t))
    return 1.0 - tail if t > 0 else tail


def t_sf_two_sided(t: float, df: float) -> float:
    """P(|T| >= |t|) for Student t with df degrees of freedom."""
    if math.isinf(df):
        return norm_sf_two_sided(t)
    return betainc(df / 2.0, 0.5, df / (df + t * t), t * t / (df + t * t))


def t_ppf(p: float, df: float) -> float:
    """Quantile of Student t (bisection on t_cdf; |error| < 1e-12)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -t_ppf(1.0 - p, df)
    lo, hi = 0.0, max(1.0, norm_ppf(p) * 2)
    while t_cdf(hi, df) < p:
        lo, hi = hi, hi * 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-13 * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


def _gser(a: float, x: float) -> float:
    ap, total = a, 1.0 / a
    delta = total
    for _ in range(100000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * _EPS:
            return total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    raise ArithmeticError("incomplete gamma series did not converge")


def _gcf(a: float, x: float) -> float:
    b = x + 1.0 - a
    c, d = 1.0 / _TINY, 1.0 / b
    h = d
    for i in range(1, 100000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = b + an / c
        c = c if abs(c) > _TINY else _TINY
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    raise ArithmeticError("incomplete gamma fraction did not converge")


def gammaincc(a: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(a, x)."""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        return 1.0 - _gser(a, x)
    return _gcf(a, x)


def chi2_sf(x: float, df: float) -> float:
    """P(X >= x) for a chi-square variable with df degrees of freedom."""
    return gammaincc(df / 2.0, x / 2.0)
