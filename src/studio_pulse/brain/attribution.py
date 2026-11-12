"""Root cause of a change in a ratio metric (retention): mix/rate, Adtributor, DIFF-lite.

Input: for a baseline period (0) and a current period (1), the totals
(users N, retained X) and, per dimension, its elements' users n and retained
x. Suppressed cells (below the pull's minimum) are folded into an
"(other)" element so every dimension still adds up to the total.

mix vs rate (Kitagawa 1955; the symmetric form, no residual):
    dR = sum (w1-w0)(r0+r1)/2  [mix]  +  sum (w0+w1)/2 (r1-r0)  [rate]
per element, for attribution, the exact split at the current mix
    c_j = w1_j (r1_j - r0_j)  +  (w1_j - w0_j)(r0_j - R0),   sum_j c_j = R1 - R0
(a new element, absent in the baseline, is held to the baseline rate R0).

Adtributor (Bhagwan et al., NSDI 2014): explanatory power EP_j = c_j / dR,
surprise = Jensen-Shannon divergence of the element's baseline and current
shares (of users and of retained users); per dimension, elements are taken
in order of surprise while EP_j >= TEEP until their EP reaches TEP; the
dimensions that reach it are ranked by surprise.

DIFF-lite (Abuzaid et al., PVLDB 2018, as a GROUPING SETS scan): within the
current period, each single or paired cell's retention against the rest of
the period (two-proportion z, BH-adjusted with everything else in the run),
with minimum support. A pair is kept only when it is clearly worse than each
of its single-dimension parents, so "1.8.0 x low tier" is reported only when
1.8.0 alone does not explain it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import stats

TEEP = 0.10
TEP = 0.67
MIN_SUPPORT = 0.05
MIN_GAP = 0.15  # cell retention at least 15% (relative) below the rest
OTHER = "(other)"


@dataclass
class Element:
    value: str  # "1.8.0" or "1.8.0 | low" for pairs
    n0: float
    x0: float
    n1: float
    x1: float

    def r0(self, fallback: float) -> float:
        return self.x0 / self.n0 if self.n0 > 0 else fallback

    def r1(self, fallback: float) -> float:
        return self.x1 / self.n1 if self.n1 > 0 else fallback


@dataclass
class Totals:
    n0: float
    x0: float
    n1: float
    x1: float

    @property
    def r0(self) -> float:
        return self.x0 / self.n0 if self.n0 else math.nan

    @property
    def r1(self) -> float:
        return self.x1 / self.n1 if self.n1 else math.nan

    @property
    def delta(self) -> float:
        return self.r1 - self.r0


@dataclass
class Row:
    value: str
    share0: float
    share1: float
    rate0: float | None
    rate1: float | None
    rate_effect: float
    mix_effect: float
    contribution: float
    ep: float
    surprise: float


@dataclass
class Decomposition:
    dimension: str
    rows: list[Row]
    mix: float  # symmetric Kitagawa totals
    rate: float
    total: float


@dataclass
class Candidate:
    dimension: str
    values: list[str]
    ep: float
    surprise: float


@dataclass
class Cell:
    dimension: str  # "app_version" or "app_version,device_tier"
    value: str
    n: float
    x: float
    rate: float
    rest_rate: float
    gap: float  # rate / rest_rate - 1
    support: float
    z: float
    p: float
    rate0: float | None = None  # the same cell in the baseline
    q: float | None = None
    parents: list[str] = field(default_factory=list)


def with_other(elements: list[Element], totals: Totals) -> list[Element]:
    """Add the suppressed remainder as one element so the dimension sums to the totals."""
    n0 = totals.n0 - sum(e.n0 for e in elements)
    x0 = totals.x0 - sum(e.x0 for e in elements)
    n1 = totals.n1 - sum(e.n1 for e in elements)
    x1 = totals.x1 - sum(e.x1 for e in elements)
    if n0 > 0.5 or n1 > 0.5:
        return [*elements, Element(OTHER, max(n0, 0), max(x0, 0), max(n1, 0), max(x1, 0))]
    return list(elements)


def decompose(dimension: str, elements: list[Element], totals: Totals) -> Decomposition:
    R0, R1 = totals.r0, totals.r1
    d = R1 - R0
    rows: list[Row] = []
    mix_sym = rate_sym = 0.0
    for e in elements:
        w0 = e.n0 / totals.n0 if totals.n0 else 0.0
        w1 = e.n1 / totals.n1 if totals.n1 else 0.0
        r0 = e.r0(R0)
        r1 = e.r1(r0)
        rate_e = w1 * (r1 - r0)
        mix_e = (w1 - w0) * (r0 - R0)
        c = rate_e + mix_e
        mix_sym += (w1 - w0) * (r0 + r1) / 2
        rate_sym += (w0 + w1) / 2 * (r1 - r0)
        s0x = e.x0 / totals.x0 if totals.x0 else 0.0
        s1x = e.x1 / totals.x1 if totals.x1 else 0.0
        surprise = stats.js_divergence(w0, w1) + stats.js_divergence(s0x, s1x)
        rows.append(
            Row(
                e.value,
                w0,
                w1,
                e.x0 / e.n0 if e.n0 else None,
                e.x1 / e.n1 if e.n1 else None,
                rate_e,
                mix_e,
                c,
                c / d if d else 0.0,
                surprise,
            )
        )
    rows.sort(key=lambda r: r.contribution if d < 0 else -r.contribution)
    return Decomposition(dimension, rows, mix_sym, rate_sym, d)


def adtributor(
    decomps: list[Decomposition], *, teep: float = TEEP, tep: float = TEP, k: int = 3
) -> list[Candidate]:
    out: list[Candidate] = []
    for dec in decomps:
        chosen: list[Row] = []
        total_ep = 0.0
        for row in sorted(dec.rows, key=lambda r: -r.surprise):
            if row.value == OTHER:
                continue
            if row.ep >= teep:
                chosen.append(row)
                total_ep += row.ep
                if total_ep >= tep:
                    break
        if chosen and total_ep >= tep:
            out.append(
                Candidate(
                    dec.dimension,
                    [r.value for r in chosen],
                    total_ep,
                    sum(r.surprise for r in chosen),
                )
            )
    out.sort(key=lambda c: (-c.surprise, -c.ep, c.dimension))
    return out[:k]


def diff_cells(
    cells: dict[str, list[Element]], totals: Totals, *, min_support: float = MIN_SUPPORT
) -> list[Cell]:
    """Every current-period cell with enough support, tested against the rest of the period."""
    out: list[Cell] = []
    N, X = totals.n1, totals.x1
    for dimension, elements in cells.items():
        for e in elements:
            if e.value == OTHER or e.n1 <= 0 or N - e.n1 <= 0:
                continue
            support = e.n1 / N
            if support < min_support:
                continue
            rest_n, rest_x = N - e.n1, X - e.x1
            t = stats.two_proportions(rest_x, rest_n, e.x1, e.n1)
            if t is None or t.p1 <= 0:
                continue
            out.append(
                Cell(
                    dimension=dimension,
                    value=e.value,
                    n=e.n1,
                    x=e.x1,
                    rate=t.p2,
                    rest_rate=t.p1,
                    gap=t.p2 / t.p1 - 1,
                    support=support,
                    z=t.z,
                    p=t.p_value,
                    rate0=(e.x0 / e.n0) if e.n0 > 0 else None,
                )
            )
    return out


def confirmed(cells: list[Cell], q_level: float, *, min_gap: float = MIN_GAP) -> list[Cell]:
    """Cells that retain clearly worse than the rest (q set by the caller's BH run)."""
    worse = [
        c for c in cells if c.q is not None and c.q <= q_level and c.gap <= -min_gap and c.z < 0
    ]
    singles = {(c.dimension, c.value): c for c in worse if "," not in c.dimension}
    keep: list[Cell] = []
    for c in worse:
        if "," not in c.dimension:
            keep.append(c)
            continue
        dims = c.dimension.split(",")
        vals = c.value.split(" | ")
        parents = [singles.get((d, v)) for d, v in zip(dims, vals, strict=False)]
        c.parents = [f"{d}={v}" for d, v in zip(dims, vals, strict=False)]
        # a pair must be clearly worse than every significant parent on its own
        if all(p is None or c.rate <= p.rate * (1 - min_gap) for p in parents):
            keep.append(c)
    keep.sort(key=lambda c: (c.gap * c.support, c.p))
    return keep


def verdict(
    candidates: list[Candidate], cells: list[Cell]
) -> tuple[str, Cell | None, Candidate | None]:
    """('pair' | 'single' | 'broad', cell, candidate) for the narration."""
    pairs = [c for c in cells if "," in c.dimension]
    singles = [c for c in cells if "," not in c.dimension]
    if pairs:  # confirmed() kept only pairs clearly worse than each single parent
        return "pair", pairs[0], None
    for cand in candidates:
        for c in singles:
            if c.dimension == cand.dimension and c.value in cand.values:
                return "single", c, cand
    if singles:
        return "single", singles[0], None
    return "broad", None, candidates[0] if candidates else None
