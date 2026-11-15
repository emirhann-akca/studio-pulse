"""Winner score ("kazanan puanı"): an ESTIMATE from public signals, never a performance number.

No ad repository publishes a competitor's installs, CPI or ROAS. What is public is how an
advertiser treats a creative: a creative that keeps running for months, is still running,
has many variants of the same idea, runs in many regions or networks and has a large
impression or reach count is one the advertiser keeps paying for. Each part is 0-1 and never
falls as its signal grows (tests/test_radar_creatives.py checks this):

    longevity = min(1, ln(1 + days_live) / ln(1 + 180))        days_live = last - first + 1
    active    = 1 if last shown <= active_days ago, then falls linearly to 0 at 90 days
    variants  = min(1, log2(n) / log2(16))     n = creatives of the advertiser with the same
                                                tagged hook and mechanic (no data untagged)
    spread    = max(min(1, (regions - 1) / 19), min(1, (networks - 1) / 3))
    volume    = min(1, log10(1 + evidence) / 7)   evidence = Google's lower bound of times
                shown, else Meta's EU reach, else TikTok's reach bucket floor

    score = 100 x (0.35 longevity + 0.15 active + 0.20 variants + 0.15 spread + 0.15 volume)

A part without data is 0 (no evidence, no points) and is listed as "no data". Weights are
`radar.creatives.weights`. Google's dataset dates are days, so a creative first and last
shown today has days_live 1.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from typing import Any

LONGEVITY_SAT = 180
ACTIVE_ZERO = 90
VARIANT_SAT = 16
REGION_SAT = 20
NETWORK_SAT = 4
VOLUME_LOG_SAT = 7  # 10 million


def clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def parts(
    *,
    first: dt.date | None,
    last: dt.date | None,
    as_of: dt.date,
    active_days: int,
    variants: int | None,
    regions: int | None,
    networks: int | None,
    times_shown: int | None,
    reach: int | None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if first and last and last >= first:
        days = (last - first).days + 1
        out["longevity"] = {"value": clip(math.log1p(days) / math.log1p(LONGEVITY_SAT)),
                            "signal": days}  # fmt: skip
    else:
        out["longevity"] = {"value": 0.0, "signal": None}
    if last:
        ago = max(0, (as_of - last).days)
        v = (
            1.0
            if ago <= active_days
            else clip(1 - (ago - active_days) / (ACTIVE_ZERO - active_days))
        )
        out["active"] = {"value": v, "signal": ago}
    else:
        out["active"] = {"value": 0.0, "signal": None}
    if variants and variants >= 1:
        out["variants"] = {"value": clip(math.log2(variants) / math.log2(VARIANT_SAT)),
                           "signal": variants}  # fmt: skip
    else:
        out["variants"] = {"value": 0.0, "signal": None}
    r = clip((regions - 1) / (REGION_SAT - 1)) if regions else 0.0
    n = clip((networks - 1) / (NETWORK_SAT - 1)) if networks else 0.0
    out["spread"] = {
        "value": max(r, n),
        "signal": {"regions": regions or 0, "networks": networks or 0}
        if (regions or networks)
        else None,
    }
    ev = times_shown if times_shown is not None else reach
    if ev is not None and ev >= 0:
        out["volume"] = {"value": clip(math.log10(1 + ev) / VOLUME_LOG_SAT), "signal": ev,
                         "kind": "times_shown" if times_shown is not None else "reach"}  # fmt: skip
    else:
        out["volume"] = {"value": 0.0, "signal": None}
    return out


def total(p: dict[str, dict[str, Any]], weights: dict[str, float]) -> float:
    return round(100 * sum(weights[k] * p[k]["value"] for k in weights), 1)


def cluster_key(row: dict[str, Any]) -> str | None:
    """Same advertiser, same tagged hook and mechanic: one concept. Untagged: no cluster."""
    hook, mech = row.get("hook"), row.get("mechanic")
    if not hook or hook in ("unknown", "other") or not mech:
        return None
    return f"{row.get('advertiser_key')}|{hook}|{mech}"


def score_rows(
    rows: list[dict[str, Any]], *, as_of: dt.date, active_days: int, weights: dict[str, float]
) -> list[dict[str, Any]]:
    """Adds `cluster`, `variants`, `parts` and `score` to every row (a new dict each)."""
    sizes = Counter(k for k in (cluster_key(r) for r in rows) if k)
    out = []
    for r in rows:
        k = cluster_key(r)
        n = sizes[k] if k else None  # untagged: the concept is unknown, not a single variant
        p = parts(
            first=r.get("first_shown"),
            last=r.get("last_shown"),
            as_of=as_of,
            active_days=active_days,
            variants=n,
            regions=r.get("regions_n"),
            networks=len(r.get("platforms") or []) or None,
            times_shown=r.get("times_shown_min"),
            reach=r.get("eu_reach"),
        )
        out.append({**r, "cluster": k, "variants": n, "parts": p, "score": total(p, weights)})
    return out


def band(score: float) -> str:
    return "strong" if score >= 60 else ("medium" if score >= 35 else "weak")
