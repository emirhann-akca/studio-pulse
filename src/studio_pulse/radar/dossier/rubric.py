"""The "Yapmalı mıyız? / Should we make it?" card: a transparent, configurable rubric.

Six factors, each a deterministic score in [0, 1] (1 = good for us) with its inputs and their
labels; weights, thresholds and the studio profile come from radar.dossier (settings.py):

  momentum          the game's radar class and its mechanic's weekly chart-share trend
  saturation        similar games in today's lists, how many of them gross, top-3 publisher share
  monetization_fit  the store's ads/IAP flags and the monetisation the deconstruction found,
                    against the models the studio runs
  capability_fit    mechanic vs the studio's shipped mechanics and genres; art style and
                    features the studio avoids (PvP, battle pass ...)
  production        person-months (a studio assumption, benchmarks.yaml effort) vs capacity
  cpi_risk          AppsFlyer Q3 2026 CPI of the genre in the launch market vs the studio's
                    max CPI

total = sum(w * s) / sum(w). Hard gates (production even at the low end over capacity, a
feature the studio avoids at the core, capability below 0.25) make the verdict YAPMA; an
unknown mechanic, a missing deconstruction or a benchmark CPI at least cpi_cap_ratio (2) times
the studio's max CPI caps it at BEKLE. Otherwise YAP at
total >= yap_min, BEKLE at >= bekle_min, else YAPMA. The three strongest reasons are the
factors with the largest weighted distance from neutral (0.5). Retention is not scored (a
competitor's retention is not observable): the GameAnalytics 2026 bands are the gates of the
first test's next step.
"""

from __future__ import annotations

import math
from typing import Any

from ... import benchmarks as ga
from ..digest import fmt
from ..taxonomy import Taxonomy
from .settings import DossierSettings

FACTOR_NAMES = {
    "tr": {
        "momentum": "Pazar ivmesi",
        "saturation": "Doygunluk",
        "monetization_fit": "Para modeli uyumu",
        "capability_fit": "Yetenek uyumu",
        "production": "Üretim tahmini",
        "cpi_risk": "CPI riski",
    },
    "en": {
        "momentum": "Market momentum",
        "saturation": "Saturation",
        "monetization_fit": "Monetisation fit",
        "capability_fit": "Capability fit",
        "production": "Production estimate",
        "cpi_risk": "CPI risk",
    },
}
VERDICTS = {
    "tr": {"YAP": "YAP", "BEKLE": "BEKLE", "YAPMA": "YAPMA"},
    "en": {"YAP": "MAKE IT", "BEKLE": "WAIT", "YAPMA": "DON'T"},
}
PUZZLE_FAMILY = {"puzzle", "match"}


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _pct(x: float | None, lang: str) -> str:
    return "-" if x is None else fmt(x * 100, lang, 1)


def _r(tr: str, en: str) -> dict[str, str]:
    return {"tr": tr, "en": en}


def apple_genres(d: dict[str, Any]) -> list[str]:
    """The Apple primary genre ("Games" for every game) and then the listed subgenres."""
    a = (d.get("store") or {}).get("apple") or {}
    out = [a.get("primary_genre"), *(a.get("genres") or [])]
    p = (d.get("store") or {}).get("play") or {}
    out += [p.get("genre"), *(p.get("categories") or [])]
    return [g for g in out if g]


def genre_of(mechanic: str | None, genres: list[str], bm: dict[str, Any]) -> str | None:
    if mechanic and mechanic in (bm.get("mechanic_genre") or {}):
        return bm["mechanic_genre"][mechanic]
    table = bm.get("apple_genre") or {}
    return next((table[g] for g in genres if g in table), None)


def family_of(mechanic: str | None, genres: list[str], bm: dict[str, Any]) -> str | None:
    g = genre_of(mechanic, genres, bm)
    if g in PUZZLE_FAMILY:
        return "puzzle"
    if g == "hypercasual":
        return "casual"
    if g:
        return g
    named = [x.lower() for x in genres if x.lower() not in ("games", "game")]
    return named[0] if named else None


def cpi_for(bm: dict[str, Any], genre: str | None, cc: str, platform: str) -> float | None:
    row = (((bm.get("cpi") or {}).get("values") or {}).get(genre or "") or {}).get(cc.lower())
    if not row:
        return None
    v = row[0 if platform == "ios" else 1]
    return float(v) if v is not None else None


# -- factors ----------------------------------------------------------------------------------


def f_momentum(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    ctx, mk = d.get("warehouse") or {}, d.get("market") or {}
    score, notes, conf = 0.5, [], "high"
    cls = (ctx.get("breakout") or {}).get("class")
    bonus = {"breakout": 0.2, "late_riser": 0.1, "watch": 0.1}.get(cls or "", 0.0)
    score += bonus
    rows = [r for r in mk.get("trend") or [] if (r.get("prior_weeks") or 0) >= 4]
    hist = ctx.get("history_days") or 0
    if rows:
        best = max(rows, key=lambda r: r["delta"] or -1)
        score += _clip((best["delta"] or 0) / 0.03 * 0.3, -0.3, 0.3)
        if any(r["is_trend"] for r in rows):
            score = max(score, 0.7 + bonus / 2)
        tr = f"{mk['mechanic']} payı {best['country'].upper()} listesinde %{_pct(best['share'], 'tr')} (önceki 4 hafta %{_pct(best['prior_mean'], 'tr')}, z={fmt(best['z'], 'tr', 2)})"
        en = f"{mk['mechanic']} share in {best['country'].upper()} {_pct(best['share'], 'en')}% (previous 4 weeks {_pct(best['prior_mean'], 'en')}%, z={fmt(best['z'], 'en', 2)})"
    else:
        conf = "low"
        tr = f"mekanik trendi hesaplanamadı: radarın geçmişi {hist} gün, trend testi 4 önceki hafta ister"
        en = f"no mechanic trend yet: the radar has {hist} day(s) of history, the test needs 4 previous weeks"
        if not mk.get("mechanic"):
            tr, en = "mekanik bilinmiyor; trend aranamadı", "mechanic unknown; no trend to look up"
    if cls:
        tr = f"radar sınıfı {cls}; " + tr
        en = f"radar class {cls}; " + en
    notes.append(_r(tr, en))
    return {
        "score": _clip(score),
        "confidence": conf,
        "reason": notes[0],
        "inputs": {"class": cls, "trend_rows": len(rows), "history_days": hist},
    }


def f_saturation(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    sat = (d.get("market") or {}).get("saturation")
    if not sat:
        return {
            "score": 0.5,
            "confidence": "low",
            "reason": _r(
                "doygunluk ölçülemedi (mekanik ya da radar verisi yok)",
                "saturation not measurable (no mechanic or radar data)",
            ),
            "inputs": {},
        }
    n, g, top3 = sat["similar_apps"], sat["similar_in_grossing"], sat["top3_share"]
    score = 0.5 * _clip(1 - n / 25) + 0.25 * (1 - (top3 or 0)) + 0.25 * _clip(1 - g / 5)
    m = (d.get("market") or {}).get("mechanic")
    return {
        "score": round(score, 4),
        "confidence": "high",
        "reason": _r(
            f"bugünkü listelerde {m} etiketli {n} başka oyun, {g} tanesi hasılat listesinde; ilk 3 yayıncının satır payı %{_pct(top3, 'tr')}",
            f"{n} other {m}-tagged games in today's lists, {g} of them in a grossing list; top-3 publishers hold {_pct(top3, 'en')}% of their rows",
        ),
        "inputs": {"similar_apps": n, "in_grossing": g, "top3_share": top3},
    }


def monetization_model(d: dict[str, Any]) -> str | None:
    p = (d.get("store") or {}).get("play") or {}
    apple = (d.get("store") or {}).get("apple") or {}
    prices = [x.get("price") for x in apple.get("storefronts") or [] if x.get("price")]
    if prices and max(prices) > 0:
        return "premium"
    if p.get("contains_ads") is None and p.get("offers_iap") is None:
        return None
    ads, iap = bool(p.get("contains_ads")), bool(p.get("offers_iap"))
    return "hybrid" if ads and iap else "iap" if iap else "ads" if ads else "free"


def f_monetization(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    model = monetization_model(d)
    kinds = sorted(
        {
            c.get("kind")
            for c in (d.get("deconstruction") or {}).get("monetization") or []
            if c.get("kind")
        }
    )
    ours = [str(x) for x in s.studio.get("monetization") or []]
    avoid = set(s.studio.get("avoid") or [])
    if model is None:
        score, conf = 0.5, "low"
    else:
        score, conf = (0.8 if model in ours else 0.35), "high"
    hits = sorted(k for k in kinds if k in avoid)
    score -= 0.2 * len(hits)
    tr = f"mağaza modeli {model or 'bilinmiyor'} (bizim modellerimiz: {', '.join(ours) or '-'})"
    en = f"store model {model or 'unknown'} (ours: {', '.join(ours) or '-'})"
    if kinds:
        tr += f"; görülen: {', '.join(kinds)}"
        en += f"; seen: {', '.join(kinds)}"
    if hits:
        tr += f"; kaçındığımız: {', '.join(hits)}"
        en += f"; we avoid: {', '.join(hits)}"
    return {
        "score": round(_clip(score), 4),
        "confidence": conf,
        "reason": _r(tr, en),
        "inputs": {"model": model, "kinds": kinds, "avoid_hits": hits},
    }


def _art_kind(d: dict[str, Any]) -> str | None:
    arts = [
        c.get("kind")
        for c in (d.get("deconstruction") or {}).get("art_style") or []
        if c.get("kind")
    ]
    return arts[0] if arts else None


def _features(d: dict[str, Any]) -> set[str]:
    """Twists and features the radar's tags or the deconstruction found (for the avoid list)."""
    out = {
        t["value"]
        for t in (d.get("warehouse") or {}).get("tags") or []
        if t.get("dimension") in ("twist", "monetization_feature", "meta")
    }
    dec = d.get("deconstruction") or {}
    out |= {c.get("kind") for c in dec.get("monetization") or [] if c.get("kind")}
    out |= {p["name"] for p in dec.get("proposed_tags") or []}
    return {x for x in out if x}


def _core_features(d: dict[str, Any]) -> set[str]:
    """Gameplay twists (radar tags) and proposed tags: what the game is built on, unlike a
    monetisation feature that can be left out of our version."""
    out = {
        t["value"]
        for t in (d.get("warehouse") or {}).get("tags") or []
        if t.get("dimension") == "twist"
    }
    out |= {p["name"] for p in (d.get("deconstruction") or {}).get("proposed_tags") or []}
    return {x for x in out if x}


def f_capability(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    mech = d.get("mechanic", {}).get("value")
    apple_genre = apple_genres(d)
    fam = family_of(mech, apple_genre, s.benchmarks)
    strengths = set(s.studio.get("strengths") or [])
    genres = {str(g).lower() for g in s.studio.get("genres") or []}
    if not mech and fam and fam in genres:
        score, why_tr, why_en, conf = (
            0.55,
            f"mekanik bilinmiyor; tür ({fam}) bizim türümüz",
            f"mechanic unknown; the genre ({fam}) is ours",
            "low",
        )
    elif not mech and fam:
        score, why_tr, why_en, conf = (
            0.2,
            f"mekanik bilinmiyor ve tür ({fam}) bizim türlerimiz dışında",
            f"mechanic unknown and the genre ({fam}) is outside ours",
            "high",
        )
    elif not mech:
        score, why_tr, why_en, conf = (
            0.4,
            "mekanik ve tür bilinmiyor",
            "mechanic and genre unknown",
            "low",
        )
    elif mech in strengths:
        score, why_tr, why_en, conf = (
            1.0,
            f"{mech} daha önce yaptığımız mekanik",
            f"{mech} is a mechanic we have shipped",
            "high",
        )
    elif fam in genres:
        score, why_tr, why_en, conf = (
            0.75,
            f"{mech} bizim türümüzde ({fam})",
            f"{mech} is in our genre ({fam})",
            "high",
        )
    else:
        score, why_tr, why_en, conf = (
            0.3,
            f"{mech} türü ({fam or '?'}) bizim türlerimiz dışında",
            f"{mech} ({fam or '?'}) is outside our genres",
            "high",
        )
    art = _art_kind(d)
    arts = set(s.studio.get("art") or [])
    if art in ("realistic-3d", "stylized-3d") and art not in arts:
        pen = 0.25 if art == "realistic-3d" else 0.15
        score -= pen
        why_tr += f"; sanat stili {art} bizim üretebildiklerimiz dışında"
        why_en += f"; art style {art} is outside what we produce"
    avoid = set(s.studio.get("avoid") or [])
    hits = sorted(_features(d) & avoid)
    core_hits = sorted(_core_features(d) & avoid)
    if hits:
        score -= 0.3 * len(core_hits) + 0.15 * (len(hits) - len(core_hits))
        why_tr += f"; kaçındığımız özellik: {', '.join(hits)}"
        why_en += f"; features we avoid: {', '.join(hits)}"
    liveops = len((d.get("deconstruction") or {}).get("liveops") or [])
    if liveops >= 3:
        score -= 0.1
        why_tr += "; yoğun LiveOps işaretleri"
        why_en += "; heavy LiveOps signals"
    return {
        "score": round(_clip(score), 4),
        "confidence": conf,
        "reason": _r(why_tr, why_en),
        "inputs": {
            "mechanic": mech,
            "family": fam,
            "art": art,
            "avoid_hits": hits,
            "core_avoid_hits": core_hits,
            "liveops_claims": liveops,
        },
    }


def effort(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    e = s.benchmarks.get("effort") or {}
    base_t, add = e.get("base") or {}, e.get("adders") or {}
    mech = d.get("mechanic", {}).get("value")
    base = float(base_t.get(mech or "unknown", base_t.get("unknown", 6.0)))
    dec = d.get("deconstruction") or {}
    parts = {"base": base}
    mult = 1.0
    metas = min(3, len(dec.get("meta_layer") or []))
    if metas:
        mult += add.get("meta_layer", 0.25) * metas
        parts["meta_layer"] = metas
    if dec.get("liveops"):
        mult += add.get("liveops", 0.2)
        parts["liveops"] = 1
    if (_art_kind(d) or "").endswith("3d"):
        mult += add.get("art_3d", 0.3)
        parts["art_3d"] = 1
    if any("pvp" in f or "multiplayer" in f for f in _features(d)):
        mult += add.get("pvp", 0.6)
        parts["pvp"] = 1
    mid = base * mult
    r = float(e.get("range", 0.35))
    return {
        "low": round(mid * (1 - r), 1),
        "mid": round(mid, 1),
        "high": round(mid * (1 + r), 1),
        "unit": "person-months",
        "method": "benchmarks.yaml effort: base(mechanic) x (1 + adders)",
        "parts": parts,
        "basis": "assumption",
    }


def f_production(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    est = effort(d, s)
    cap = float(s.studio.get("team_size", 2)) * float(s.studio.get("months", 3))
    ratio = est["high"] / cap if cap else math.inf
    score = _clip(1 - (ratio - 0.6))
    return {
        "score": round(score, 4),
        "confidence": "low" if not d.get("mechanic", {}).get("value") else "high",
        "reason": _r(
            f"ilk sürüm {fmt(est['low'], 'tr', 1)}–{fmt(est['high'], 'tr', 1)} kişi-ay (VARSAYIM), kapasitemiz {fmt(cap, 'tr', 1)} kişi-ay",
            f"first version {fmt(est['low'], 'en', 1)}–{fmt(est['high'], 'en', 1)} person-months (ASSUMPTION), our capacity {fmt(cap, 'en', 1)} person-months",
        ),
        "inputs": {"effort": est, "capacity": cap},
        "gate": est["low"] > cap,
    }


def f_cpi(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    mech = d.get("mechanic", {}).get("value")
    apple_genre = apple_genres(d)
    genre = genre_of(mech, apple_genre, s.benchmarks)
    plat = str(s.studio.get("launch_platform", "android"))
    max_cpi = float(s.studio.get("max_cpi_usd", 1.5))
    vals = [
        (cc, cpi_for(s.benchmarks, genre, cc, plat))
        for cc in s.studio.get("target_markets") or ["us"]
    ]
    vals = [(cc, v) for cc, v in vals if v is not None]
    if not vals:
        return {
            "score": 0.5,
            "confidence": "low",
            "reason": _r(
                f"{genre or 'bu tür'} için CPI kıyası yok",
                f"no CPI benchmark for {genre or 'this genre'}",
            ),
            "inputs": {"genre": genre},
        }
    cc, v = max(vals, key=lambda x: x[1])
    score = _clip(1.5 - v / max_cpi)
    src = (s.benchmarks.get("cpi") or {}).get("source", "")
    return {
        "score": round(score, 4),
        "confidence": "high",
        "reason": _r(
            f"{genre} CPI {cc.upper()} {plat}: {fmt(v, 'tr', 2)} USD (KIYAS, {src}); taşıyabildiğimiz en yüksek CPI {fmt(max_cpi, 'tr', 2)} USD",
            f"{genre} CPI {cc.upper()} {plat}: {fmt(v, 'en', 2)} USD (BENCHMARK, {src}); the most we can carry {fmt(max_cpi, 'en', 2)} USD",
        ),
        "inputs": {
            "genre": genre,
            "country": cc,
            "platform": plat,
            "cpi_usd": {"value": v, "basis": "benchmark"},
            "max_cpi_usd": {"value": max_cpi, "basis": "assumption"},
        },
    }


FACTORS = {
    "momentum": f_momentum,
    "saturation": f_saturation,
    "monetization_fit": f_monetization,
    "capability_fit": f_capability,
    "production": f_production,
    "cpi_risk": f_cpi,
}


def retention_bars() -> list[dict[str, Any]]:
    out = []
    for day_n, bands in ga.RETENTION.items():
        for b in bands:
            out.append(
                {"day": day_n, "label": b.label, "cut": b.cut, "text": b.text, "basis": "benchmark"}
            )
    return out


def decide(d: dict[str, Any], s: DossierSettings) -> dict[str, Any]:
    factors = []
    for key, fn in FACTORS.items():
        res = fn(d, s)
        w = float(s.weights.get(key, 0))
        factors.append(
            {
                "key": key,
                "weight": {"value": w, "basis": "assumption"},
                "score": {"value": round(res["score"], 4), "basis": "rule_score"},
                "confidence": res["confidence"],
                "reason": res["reason"],
                "inputs": {"basis": "rule_score", **res.get("inputs", {})},
                "gate": bool(res.get("gate")),
            }
        )
    wsum = sum(f["weight"]["value"] for f in factors) or 1.0
    total = sum(f["weight"]["value"] * f["score"]["value"] for f in factors) / wsum
    gates: list[dict[str, str]] = []
    prod = next(f for f in factors if f["key"] == "production")
    if prod["gate"]:
        gates.append(
            _r(
                "üretim tahmininin alt ucu bile kapasitemizi aşıyor",
                "even the low end of the production estimate exceeds our capacity",
            )
        )
    cap = next(f for f in factors if f["key"] == "capability_fit")
    if cap["score"]["value"] < 0.25:
        gates.append(_r("yetenek uyumu çok düşük", "capability fit is too low"))
    if cap["inputs"].get("core_avoid_hits"):
        hits = ", ".join(cap["inputs"]["core_avoid_hits"])
        gates.append(
            _r(
                f"oyunun özünde kaçındığımız özellik var: {hits}",
                f"the game is built on a feature we avoid: {hits}",
            )
        )
    capped = []
    if not d.get("mechanic", {}).get("value"):
        capped.append(_r("mekanik bilinmiyor", "mechanic unknown"))
    if (d.get("deconstruction") or {}).get("status") != "ok":
        capped.append(_r("LLM yapısökümü yok", "no LLM deconstruction"))
    cpi_in = next(f for f in factors if f["key"] == "cpi_risk")["inputs"]
    if cpi_in.get("cpi_usd") and cpi_in.get("max_cpi_usd"):
        ratio = cpi_in["cpi_usd"]["value"] / cpi_in["max_cpi_usd"]["value"]
        if ratio >= s.cpi_cap_ratio:
            capped.append(
                _r(
                    f"hedef pazardaki CPI kıyası taşıyabileceğimizin {fmt(ratio, 'tr', 1)} katı",
                    f"the target market's benchmark CPI is {fmt(ratio, 'en', 1)}x what we can carry",
                )
            )
    if gates:
        verdict = "YAPMA"
    elif total >= s.yap_min:
        verdict = "YAP"
    elif total >= s.bekle_min:
        verdict = "BEKLE"
    else:
        verdict = "YAPMA"
    if capped and verdict == "YAP":
        verdict = "BEKLE"
    ranked = sorted(factors, key=lambda f: -abs(f["weight"]["value"] * (f["score"]["value"] - 0.5)))
    reasons = [{"kind": "gate", "text": g} for g in gates]
    for f in ranked:
        if len(reasons) >= 3:
            break
        sign = "+" if f["score"]["value"] >= 0.5 else "-"
        reasons.append({"kind": "factor", "factor": f["key"], "sign": sign, "text": f["reason"]})
    return {
        "verdict": verdict,
        "score": {"value": round(total, 4), "basis": "rule_score"},
        "thresholds": {"yap_min": s.yap_min, "bekle_min": s.bekle_min, "basis": "assumption"},
        "factors": factors,
        "gates": gates,
        "capped": capped,
        "reasons": reasons,
        "assumptions": assumptions(d, s),
        "retention_bars": retention_bars(),
    }


def assumptions(d: dict[str, Any], s: DossierSettings) -> list[dict[str, str]]:
    st = s.studio
    cpi = s.benchmarks.get("cpi") or {}
    hist = (d.get("warehouse") or {}).get("history_days") or 0
    n_rev = (d.get("reviews") or {}).get("n") or 0
    out = [
        _r(
            f"Stüdyo profili (yapılandırma): {st.get('team_size')} kişi, {st.get('engine')}, türler {', '.join(st.get('genres') or [])}, en fazla {st.get('months')} ay, bütçe {st.get('budget_usd')} USD, yaptığımız mekanikler: {', '.join(st.get('strengths') or []) or '-'}",
            f"Studio profile (config): {st.get('team_size')} people, {st.get('engine')}, genres {', '.join(st.get('genres') or [])}, at most {st.get('months')} months, budget {st.get('budget_usd')} USD, mechanics we shipped: {', '.join(st.get('strengths') or []) or '-'}",
        ),
        _r(
            "Üretim süresi tablosu bir stüdyo varsayımıdır (benchmarks.yaml effort), ölçülmüş veri değildir.",
            "The production effort table is a studio assumption (benchmarks.yaml effort), not measured data.",
        ),
        _r(
            f"CPI kıyasları: {cpi.get('source')} ({cpi.get('url')}); türe eşleme yaklaşık (AppsFlyer tür tanımlarını yayımlamıyor).",
            f"CPI benchmarks: {cpi.get('source')} ({cpi.get('url')}); the genre mapping is approximate (AppsFlyer does not publish its genre definitions).",
        ),
        _r(
            f"Radar geçmişi {hist} gün; mekanik trendi 4 önceki hafta olmadan puanlanmaz (nötr 0,5).",
            f"Radar history is {hist} day(s); the mechanic trend is not scored before 4 previous weeks (neutral 0.5).",
        ),
        _r(
            "Rakibin tutundurma oranı gözlenemez; GameAnalytics 2026 bantları yalnızca bizim prototip kapımızdır.",
            "A competitor's retention is not observable; the GameAnalytics 2026 bands are only our prototype's gate.",
        ),
    ]
    if n_rev:
        out.append(
            _r(
                f"Yorum temaları {n_rev} yorumdan (Play sayfasındaki 'en alakalı' sıra, rastgele örnek değil); etiketler LLM'in, alıntılar yorum metninde doğrulandı.",
                f"Review themes come from {n_rev} reviews (Play's 'most relevant' order, not a random sample); labels are the LLM's, quotes were checked in the review text.",
            )
        )
    return out


# -- first test ---------------------------------------------------------------------------------


def _ceil10(x: float) -> int:
    return int(math.ceil(x / 10.0) * 10)


def first_test(d: dict[str, Any], s: DossierSettings, tax: Taxonomy) -> dict[str, Any]:
    t = s.test
    mech = d.get("mechanic", {}).get("value")
    apple_genre = apple_genres(d)
    genre = genre_of(mech, apple_genre, s.benchmarks)
    geo = str(s.studio.get("test_market", "br")).lower()
    plat = str(s.studio.get("test_platform", "android"))
    cpi = cpi_for(s.benchmarks, genre, geo, plat)
    cpi_note = None
    if cpi is None:
        vals = ((s.benchmarks.get("cpi") or {}).get("values") or {}).get(genre or "") or {}
        cand = [
            (cc, v[1 if plat == "android" else 0])
            for cc, v in vals.items()
            if v[1 if plat == "android" else 0] is not None
        ]
        if cand:
            geo, cpi = min(cand, key=lambda x: x[1])
            cpi_note = "test market without a benchmark: cheapest benchmarked country used"
    variants = int(t["variants"])
    per = int(t["installs_per_variant"])
    budget = low = None
    if cpi is not None:
        floor = variants * float(t["min_per_variant_usd"])
        low = _ceil10(max(variants * per * cpi, floor))
        budget = _ceil10(max(variants * per * cpi * float(t["safety"]), floor))
    hooks = tax.hooks(mech) or ["challenge", "near-miss"]
    core = ((d.get("deconstruction") or {}).get("core_loop") or [None])[0] or {}
    plan_variants = [
        {
            "id": f"V{i + 1}",
            "hook": hooks[i % len(hooks)],
            "show": core.get("text"),
            "show_tr": core.get("text_tr") or core.get("text"),
        }
        for i in range(variants)
    ]
    ipm = float(s.studio.get("ipm_gate", 8))
    over_budget = budget is not None and budget > float(s.studio.get("budget_usd", 0) or 0)
    return {
        "kind": "creative_fake_door",
        "mechanic": mech,
        "mechanic_shown": tax.forge(mech) if mech else "other",
        "geo": geo,
        "platform": plat,
        "variants": plan_variants,
        "n_variants": {"value": variants, "basis": "assumption"},
        "installs_per_variant": {"value": per, "basis": "assumption"},
        "days": {"value": int(t["days"]), "basis": "assumption"},
        "min_per_variant_usd": {"value": float(t["min_per_variant_usd"]), "basis": "assumption"},
        "cpi_reference": {
            "value": cpi,
            "basis": "benchmark",
            "source": (s.benchmarks.get("cpi") or {}).get("source"),
            "note": cpi_note,
        }
        if cpi is not None
        else None,
        "budget_usd": {
            "value": budget,
            "low": low,
            "high": budget,
            "method": "variants x installs per variant x benchmark CPI (high: x safety), "
            "at least the minimum per variant",
            "n": variants,
            "basis": "estimate",
        }
        if budget is not None
        else None,
        "ipm_gate": {"value": ipm, "basis": "assumption"},
        "cpi_gate": {"value": cpi, "basis": "benchmark"} if cpi is not None else None,
        "over_budget": over_budget,
        "next_gate": [
            b for b in retention_bars() if b["label"] == "top 25%" and b["day"] in (1, 7)
        ],
    }


def forge_doc(d: dict[str, Any], tax: Taxonomy) -> dict[str, Any]:
    """The first test as a creative-forge market_trends.yaml document (schema v1, one lesson)."""
    import datetime as dt

    test, dec, app = d["first_test"], d["decision"], d["app"]
    mech = test.get("mechanic") or "unknown"
    day = d["as_of"]
    reasons = "; ".join(r["text"]["en"] for r in dec["reasons"])
    budget = (test.get("budget_usd") or {}).get("value")
    cpi = (test.get("cpi_reference") or {}).get("value")
    text = (
        f"Game dossier {day}: {app['name']} ({app.get('developer')}), primary mechanic {mech} "
        f"({d.get('mechanic', {}).get('source', 'unknown')}). Verdict for our studio: {dec['verdict']} "
        f"(rule score {dec['score']['value']:.2f}); reasons: {reasons}. "
        f"First test: a creative fake-door test with {test['n_variants']['value']} video variants "
        f"(hooks {', '.join(v['hook'] for v in test['variants'])}) in {test['geo'].upper()} "
        f"{test['platform']} for {test['days']['value']} days"
        + (
            f", budget about {budget} USD (estimate from a benchmark CPI of {cpi} USD)"
            if budget
            else ""
        )
        + f"; pass if IPM >= {test['ipm_gate']['value']:g} [assumption]"
        + (f" and CPI <= {cpi} USD [benchmark]" if cpi else "")
        + ". Strengths to show (LLM, evidence-checked): "
        + "; ".join(c["text"] for c in (d.get("deconstruction") or {}).get("strengths", [])[:3])
        + ". A direction for a test, not proof of performance."
    )
    themes = sorted(
        {
            t["value"]
            for t in (d.get("warehouse") or {}).get("tags") or []
            if t.get("dimension") == "theme"
        }
    )
    twists = sorted(
        {
            t["value"]
            for t in (d.get("warehouse") or {}).get("tags") or []
            if t.get("dimension") == "twist"
        }
    )
    ev = []
    if app.get("apple_id"):
        ev.append(f"lookup:{d['countries'][0]}:{app['apple_id']}@{day}")
    if app.get("play_package"):
        ev.append(f"play:{app['play_package']}@{day}")
    ev += [
        f"rss:{r['country']}:{r['list']}#{r['rank']}@{(d.get('warehouse') or {}).get('latest_day')}"
        for r in ((d.get("warehouse") or {}).get("ranks_today") or [])[:3]
    ]
    return {
        "version": 1,
        "schema": "studio-pulse/market_trends/v1",
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "as_of": day,
        "source": "studio-pulse game dossier (store texts, screenshots, reviews; LLM claims evidence-checked)",
        "countries": d["countries"],
        "lessons": [
            {
                "id": f"DT-{day}-{d['key']}",
                "title": f"Game dossier {day}: {app['name']} - {mech} first test ({dec['verdict']})",
                "text": text,
                "kind": "market_trend",
                "mechanic": mech,
                "mechanic_shown": test["mechanic_shown"],
                "gamerefinery": tax.gamerefinery(mech),
                "themes": themes,
                "twists": twists,
                "hooks": [v["hook"] for v in test["variants"]],
                "apps": [
                    {
                        "app_key": d.get("app_key") or d["key"],
                        "name": app["name"],
                        "class": ((d.get("warehouse") or {}).get("breakout") or {}).get("class"),
                    }
                ],
                "evidence": ev,
                "signal": "dossier_first_test",
                "tags": "llm" if d.get("mechanic", {}).get("source") == "llm" else "keyword",
                "test": {
                    "variants": test["n_variants"]["value"],
                    "budget_usd": budget,
                    "days": test["days"]["value"],
                    "ipm_gate": test["ipm_gate"]["value"],
                    "cpi_gate_usd": cpi,
                    "geo": test["geo"],
                    "platform": test["platform"],
                },
                "basis": {
                    "verdict": "rule_score",
                    "budget": "estimate",
                    "cpi": "benchmark",
                    "ipm_gate": "assumption",
                    "hooks": "inference",
                },
            }
        ],
    }
