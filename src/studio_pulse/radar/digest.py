"""The daily digest: facts from SQL, rendered in Turkish and English, optional LLM narration.

Every market number in the facts carries a `basis`:
- observed (GÖZLEM / OBSERVED): read from Apple charts or Lookup, or computed only from them
  (rank, age, rating count, ratings/day, the rule-based score when no Play data went in);
- store_reported (MAĞAZA VERİSİ / STORE-REPORTED): Google Play page fields (install bucket,
  realInstalls, an undocumented page field) and anything computed from them;
- estimate (TAHMİN / ESTIMATE): always a range with low, high, method and n. The renderer
  refuses an estimate without them. No rank-to-downloads estimate is shown in v1.
Run metadata (requests, cost, checks) lives under `meta` and `quality` and is not market data.

The LLM may only rephrase supplied facts: `verify_narration` extracts every number from its
text (Turkish and English separators) and rejects the narration if one is not in the facts.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Callable
from typing import Any

import duckdb

from ..llm import LLMClient, LLMError, LLMRequest
from .settings import RadarSettings
from .taxonomy import Taxonomy

LABELS = {
    "tr": {"observed": "GÖZLEM", "store_reported": "MAĞAZA VERİSİ", "estimate": "TAHMİN"},
    "en": {"observed": "OBSERVED", "store_reported": "STORE-REPORTED", "estimate": "ESTIMATE"},
}
COUNTRY = {
    "tr": {
        "us": "ABD",
        "tr": "Türkiye",
        "gb": "Birleşik Krallık",
        "de": "Almanya",
        "fr": "Fransa",
        "br": "Brezilya",
        "jp": "Japonya",
        "kr": "Güney Kore",
    },
    "en": {
        "us": "US",
        "tr": "Turkey",
        "gb": "UK",
        "de": "Germany",
        "fr": "France",
        "br": "Brazil",
        "jp": "Japan",
        "kr": "South Korea",
    },
}
LIST = {
    "tr": {
        "topfreeapplications:6014": "Oyunlar",
        "topfreeapplications:7012": "Bulmaca",
        "topfreeapplications:7003": "Gündelik",
        "topfreeapplications:7015": "Simülasyon",
        "topgrossingapplications:6014": "Oyunlar (hasılat)",
        "newfreeapplications:6014": "Yeni ücretsiz oyunlar",
        "toppaidapplications:6014": "Ücretli oyunlar",
    },
    "en": {
        "topfreeapplications:6014": "Games",
        "topfreeapplications:7012": "Puzzle",
        "topfreeapplications:7003": "Casual",
        "topfreeapplications:7015": "Simulation",
        "topgrossingapplications:6014": "Games (grossing)",
        "newfreeapplications:6014": "New free games",
        "toppaidapplications:6014": "Paid games",
    },
}
CLASS = {
    "tr": {"breakout": "YÜKSELİYOR", "watch": "İZLE", "late_riser": "GEÇ YÜKSELEN"},
    "en": {"breakout": "BREAKOUT", "watch": "WATCH", "late_riser": "LATE RISER"},
}
T = {
    "tr": {
        "title": "Pazar Radarı",
        "intro": (
            "Kaynaklar: Apple eski RSS listeleri ({lists} liste, {countries}), iTunes "
            "Lookup ve Google Play ayrıntı sayfaları (yalnızca robots.txt'in izin verdiği "
            "yol). Etiketler: GÖZLEM = mağaza listesinden ya da Lookup'tan okunan değer; "
            "MAĞAZA VERİSİ = Play sayfa alanı (belgelenmemiş); TAHMİN = aralıklı tahmin. "
            "Sıradan indirme tahmini bu sürümde gösterilmez."
        ),
        "summary": "Özet",
        "narr_note": "(LLM anlatımı; içindeki her sayı olgularla doğrulandı)",
        "breakout": "Bugün yükselenler",
        "watch": "İzlemeye değer",
        "late_riser": "Geç yükselenler (60-120 gün)",
        "none": "Bugün bu sınıfta oyun yok.",
        "shown": "Bu sınıfta {total} oyun var; puanı en yüksek {n} tanesi aşağıda (tamamı ambarda, fct_mr_breakout_daily).",
        "score": "Kural puanı M",
        "parts": "hız {v} · tırmanış {c} · sıra {r} · yayılım {s} · yeni {n}",
        "lists": "Liste",
        "age": "Yaş",
        "age_text": "{d} gün (ilk çıkış {rel}, {src}; radarda ilk görülme {fs})",
        "src_lookup": "Lookup",
        "src_feed": "RSS tarihi",
        "ratings": "Puan sayısı",
        "per_day": "≈{v}/gün",
        "lifetime": "çıkıştan beri ortalama",
        "window": "{d} günlük fark",
        "reset": "puan sıfırlanmış, hız hesaplanmadı",
        "unconfirmed": "tek günlük düşüş, doğrulanmadı; hız hesaplanmadı",
        "play": "Play",
        "play_text": "{bucket} kovası, {real} kurulum (Play sayfa alanı; belgelenmemiş), Play çıkışı {rel}",
        "play_vel": "≈{v} kurulum/gün ({m})",
        "no_play": "Play eşlemesi yok",
        "estimate": "Tahmini iOS günlük kurulum ({c})",
        "est_method": "puan hızı ÷ Play puan/kurulum oranı %{lo}–%{hi} ({rng}), n={n}",
        "rng_iqr": "çeyrekler arası",
        "rng_minmax": "en düşük–en yüksek",
        "src_llm": "LLM, alıntıyla doğrulandı",
        "src_keyword": "anahtar kelime eşleşmesi",
        "dims": {
            "mechanic": "ek mekanik",
            "twist": "bükülme",
            "theme": "tema",
            "meta": "meta",
            "monetization_feature": "para özelliği",
        },
        "what": "Ne",
        "unknown_mech": "mekanik metinden çıkarılamadı",
        "mono": "para modeli",
        "evidence": "Kanıt",
        "trends": "Haftalık mekanik trendleri",
        "trend_short": (
            "Trend testi 4 önceki hafta istiyor; radarın geçmişi {d} gün. Bu haftanın "
            "mekanik payları ({c} Oyunlar listesi, etiketli oyunlar, GÖZLEM):"
        ),
        "trend_row": "{m}: %{share} ({apps} oyun, {days} gün)",
        "trend_hit": "{c}: {m} payı %{share}, önceki 4 hafta ortalaması %{prev}, z={z} ({apps} oyun)",
        "trend_none": "Bu hafta trend eşiğini geçen mekanik yok.",
        "coverage": "Profili olan liste satırı oranı (anahtar kelime ya da LLM): %{v}.",
        "ideas": "Bize ne öğretir / ne yapabiliriz (ÇIKARIM)",
        "idea": (
            "{name}: {mech} çekirdeği ({src}){twist}{theme}. Ne yapabiliriz: creative-forge'da "
            "mechanic_shown={forge}, kanca {hooks} ile bir kreatif testi (kanıt: {ev})."
        ),
        "idea_count": "Bugünkü adaylarda en sık birincil mekanik: {m} ({n} oyun, GÖZLEM).",
        "no_ideas": "Sınıflandırılmış aday yok; fikir üretilmedi.",
        "quality": "Veri kalitesi ve koşum",
        "q_line": "{passed}/{total} kontrol geçti, {err} hata, {warn} uyarı.",
        "q_req": "İstekler: {n} (ana bilgisayara göre: {hosts}); önbellekten {c}.",
        "q_llm": "LLM: {p} {m}, {r} istek, {cost} USD (tavan {cap}).",
        "q_llm_none": "LLM: kapalı (yalnızca anahtar kelime etiketleri).",
        "visual": "görsel çıkarım",
        "twist_w": ", bükülme {v}",
        "theme_w": ", tema {v}",
        "publishers": "İzlenen yayıncıların yeni oyunları (son 30 gün)",
    },
    "en": {
        "title": "Market Radar",
        "intro": (
            "Sources: Apple legacy RSS charts ({lists} lists, {countries}), iTunes Lookup "
            "and Google Play details pages (only the path robots.txt allows). Labels: "
            "OBSERVED = read from a chart or Lookup; STORE-REPORTED = a Play page field "
            "(undocumented); ESTIMATE = a range estimate. No rank-to-downloads estimate is "
            "shown in this version."
        ),
        "summary": "Summary",
        "narr_note": "(LLM narration; every number in it was checked against the facts)",
        "breakout": "Rising today",
        "watch": "Worth watching",
        "late_riser": "Late risers (60-120 days)",
        "none": "No game in this class today.",
        "shown": "{total} games in this class; the {n} with the highest score are below (all in the warehouse, fct_mr_breakout_daily).",
        "score": "Rule score M",
        "parts": "velocity {v} · climb {c} · rank {r} · spread {s} · new {n}",
        "lists": "Charts",
        "age": "Age",
        "age_text": "{d} days (first release {rel}, {src}; first seen by the radar {fs})",
        "src_lookup": "Lookup",
        "src_feed": "RSS date",
        "ratings": "Ratings",
        "per_day": "≈{v}/day",
        "lifetime": "average since release",
        "window": "{d}-day difference",
        "reset": "ratings were reset, no velocity",
        "unconfirmed": "one-day drop, not confirmed; no velocity",
        "play": "Play",
        "play_text": "{bucket} bucket, {real} installs (Play page field; undocumented), Play release {rel}",
        "play_vel": "≈{v} installs/day ({m})",
        "no_play": "no Play mapping",
        "estimate": "Estimated iOS daily installs ({c})",
        "est_method": "rating velocity ÷ Play ratings-per-install {lo}%–{hi}% ({rng}), n={n}",
        "rng_iqr": "interquartile",
        "rng_minmax": "min–max",
        "src_llm": "LLM, verified quote",
        "src_keyword": "keyword match",
        "dims": {
            "mechanic": "secondary mechanic",
            "twist": "twist",
            "theme": "theme",
            "meta": "meta",
            "monetization_feature": "monetisation feature",
        },
        "what": "What",
        "unknown_mech": "mechanic not stated in the text",
        "mono": "monetisation",
        "evidence": "Evidence",
        "trends": "Weekly mechanic trends",
        "trend_short": (
            "The trend test needs 4 previous weeks; the radar has {d} day(s) of history. "
            "This week's mechanic shares ({c} Games chart, tagged games, OBSERVED):"
        ),
        "trend_row": "{m}: {share}% ({apps} games, {days} days)",
        "trend_hit": "{c}: {m} share {share}%, previous 4-week mean {prev}%, z={z} ({apps} games)",
        "trend_none": "No mechanic passed the trend thresholds this week.",
        "coverage": "Tag coverage: {v}% of chart rows have a profile (keyword or LLM).",
        "ideas": "What it teaches us / what we can do (INFERENCE)",
        "idea": (
            "{name}: {mech} core ({src}){twist}{theme}. What we can do: a creative test in "
            "creative-forge with mechanic_shown={forge} and hooks {hooks} (evidence: {ev})."
        ),
        "idea_count": "Most frequent primary mechanic among today's candidates: {m} ({n} games, OBSERVED).",
        "no_ideas": "No classified candidates; no ideas generated.",
        "quality": "Data quality and run",
        "q_line": "{passed}/{total} checks passed, {err} error(s), {warn} warning(s).",
        "q_req": "Requests: {n} (by host: {hosts}); {c} from cache.",
        "q_llm": "LLM: {p} {m}, {r} request(s), {cost} USD (cap {cap}).",
        "q_llm_none": "LLM: off (keyword tags only).",
        "visual": "visual inference",
        "twist_w": ", twist {v}",
        "theme_w": ", theme {v}",
        "publishers": "New games from watched publishers (last 30 days)",
    },
}


class LabelError(ValueError):
    """A number without a basis, or an estimate without a range."""


# -- number formatting ------------------------------------------------------------------


def fmt(x: float | int | None, lang: str, decimals: int = 0) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    s = f"{x:,.{decimals}f}"
    if lang == "tr":
        s = s.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return s


def fmt_compact(x: float, lang: str) -> str:
    if abs(x) >= 1_000_000:
        v, unit = x / 1_000_000, {"tr": " mn", "en": "M"}[lang]
    elif abs(x) >= 1000:
        v, unit = x / 1000, {"tr": " bin", "en": "k"}[lang]
    else:
        return fmt(x, lang)
    return fmt(round(v, 1), lang, 1) + unit


def num(value: Any, basis: str, **extra: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if basis not in ("observed", "store_reported", "estimate"):
        raise LabelError(f"unknown basis {basis!r}")
    return {"value": value, "basis": basis, **extra}


def estimate(low: float, high: float, method: str, n: int, **extra: Any) -> dict[str, Any]:
    if low is None or high is None or not method or not n:
        raise LabelError("an estimate needs low, high, method and n")
    return {"low": low, "high": high, "method": method, "n": n, "basis": "estimate", **extra}


def check_labels(facts: dict[str, Any]) -> list[str]:
    """Every number outside meta/quality must sit in a dict with a basis; estimates need ranges."""
    problems: list[str] = []

    def walk(node: Any, path: str, has_basis: bool) -> None:
        if isinstance(node, dict):
            b = "basis" in node
            if node.get("basis") == "estimate" and not all(
                node.get(k) not in (None, "") for k in ("low", "high", "method", "n")
            ):
                problems.append(f"{path}: estimate without low/high/method/n")
            for k, v in node.items():
                if path == "$" and k in ("meta", "quality"):
                    continue
                walk(v, f"{path}.{k}", b)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", has_basis)
        elif isinstance(node, (int, float)) and not isinstance(node, bool) and not has_basis:
            problems.append(f"{path}: number {node} without a basis")

    walk(facts, "$", False)
    return problems


# -- facts --------------------------------------------------------------------------------


def _rows(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    try:
        cur = con.execute(sql, params or [])
    except duckdb.CatalogException:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _iso(d: Any) -> str | None:
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else (str(d) if d else None)


def _tags(con: duckdb.DuckDBPyConnection, app_key: str) -> dict[str, Any]:
    rows = _rows(
        con, "SELECT * FROM fct_mr_app_tags WHERE app_key = ? ORDER BY dimension, value", [app_key]
    )
    prof: dict[str, Any] = {"source": None, "claims": []}
    for r in rows:
        if r["source"] in ("llm", "keyword"):
            prof["source"] = r["source"]
        prof["claims"].append(
            {
                "dimension": r["dimension"],
                "value": r["value"],
                "field": r["field"],
                "quote": r["quote"],
                "visual": bool(r["visual"]),
                "source": r["source"],
            }
        )
    return prof


def _first(prof: dict[str, Any], dim: str) -> dict[str, Any] | None:
    return next((c for c in prof["claims"] if c["dimension"] == dim), None)


def _all(prof: dict[str, Any], dim: str) -> list[dict[str, Any]]:
    return [c for c in prof["claims"] if c["dimension"] == dim]


def build_facts(
    con: duckdb.DuckDBPyConnection,
    s: RadarSettings,
    day: dt.date,
    tax: Taxonomy,
    *,
    meta: dict[str, Any] | None = None,
    checks: list[Any] | None = None,
) -> dict[str, Any]:
    items = _rows(
        con,
        "SELECT * FROM fct_mr_breakout_daily WHERE snapshot_date = ? "
        "AND radar_class IS NOT NULL ORDER BY momentum DESC, app_id",
        [day],
    )
    # Play ratings per install over the day's Play pages: the interquartile range when there are
    # at least 8 pages (the extremes span two orders of magnitude), else min-max
    ratio = _rows(
        con,
        "WITH r AS (SELECT play_ratings / real_installs::DOUBLE AS x FROM stg_mr_install_daily "
        "WHERE snapshot_date = ? AND play_ratings > 0 AND real_installs > 0) "
        "SELECT COUNT(*) AS n, MIN(x) AS mn, MAX(x) AS mx, quantile_cont(x, 0.25) AS q1, "
        "quantile_cont(x, 0.75) AS q3 FROM r",
        [day],
    )
    r0 = ratio[0] if ratio else {"n": 0}
    iqr = (r0.get("n") or 0) >= 8
    r0["lo"], r0["hi"] = (r0.get("q1"), r0.get("q3")) if iqr else (r0.get("mn"), r0.get("mx"))
    out_items = []
    limits = {"breakout": 10, "watch": 10, "late_riser": 5}
    seen: dict[str, int] = {}
    for it in items:
        cls = it["radar_class"]
        seen[cls] = seen.get(cls, 0) + 1
        if seen[cls] > limits[cls]:
            continue
        app_id, key = it["app_id"], it["app_key"]
        ranks = _rows(
            con,
            "SELECT country, chart || ':' || genre_id AS list, rank, list_len "
            "FROM stg_mr_chart WHERE snapshot_date = ? AND app_id = ? "
            "ORDER BY rank, country, list",
            [day, app_id],
        )
        ratings = _rows(
            con,
            "SELECT country, rating_count, rv, rv_method, window_days, rating_reset "
            "FROM stg_mr_rating_daily WHERE snapshot_date = ? AND app_id = ? "
            "ORDER BY rating_count DESC, country",
            [day, app_id],
        )
        play_used = (
            it["iv"] is not None
            and it["s_vel"] > 0
            and (
                it["rv_max"] is None
                or math.log10(1 + it["iv"]) / math.log10(1 + s.params["iv_saturation"])
                >= math.log10(1 + it["rv_max"]) / math.log10(1 + s.params["rv_saturation"])
            )
        )
        score_basis = "store_reported" if play_used else "observed"
        ev = [f"rss:{r['country']}:{r['list']}#{r['rank']}@{day}" for r in ranks]
        ev += [f"lookup:{r['country']}:{app_id}@{day}" for r in ratings]
        if it["play_id"]:
            ev.append(f"play:{it['play_id']}@{day}")
        est = None
        if it["rv_max"] and r0["n"] and r0["n"] >= 3 and r0["lo"] and r0["hi"]:
            est = estimate(
                it["rv_max"] / r0["hi"],
                it["rv_max"] / r0["lo"],
                "apple_rating_velocity / play_ratings_per_install"
                + (" (interquartile)" if iqr else " (min-max)"),
                int(r0["n"]),
                country=it["rv_country"],
                ratio_low=r0["lo"],
                ratio_high=r0["hi"],
                ratio_range="iqr" if iqr else "minmax",
            )
        out_items.append(
            {
                "app_key": key,
                "app_id": app_id,
                "name": it["name"],
                "developer": it["developer"],
                "class": cls,
                "store_url": f"https://apps.apple.com/{it['best_country']}/app/id{app_id}",
                "play_url": (
                    f"https://play.google.com/store/apps/details?id={it['play_id']}"
                    if it["play_id"]
                    else None
                ),
                "momentum": num(round(it["momentum"], 4), score_basis, kind="rule_score"),
                "components": {
                    k: num(round(it[k], 4), score_basis if k == "s_vel" else "observed")
                    for k in ("s_vel", "s_climb", "s_rank_best", "s_spread", "s_new")
                },
                "ranks": [
                    {
                        "country": r["country"],
                        "list": r["list"],
                        "rank": r["rank"],
                        "list_len": r["list_len"],
                        "basis": "observed",
                        "source": "apple_rss",
                    }
                    for r in ranks
                ],
                "age": num(
                    it["age_days"],
                    "observed",
                    release_date=_iso(it["release_date"]),
                    release_source=it["release_source"],
                    first_seen=_iso(it["first_seen"]),
                    first_seen_known=bool(it["first_seen_known"]),
                ),
                "ratings": [
                    {
                        "country": r["country"],
                        "count": r["rating_count"],
                        "per_day": round(r["rv"], 1) if r["rv"] is not None else None,
                        "method": r["rv_method"],
                        "window_days": r["window_days"],
                        "reset": bool(r["rating_reset"]),
                        "basis": "observed",
                        "source": "itunes_lookup",
                    }
                    for r in ratings
                ],
                "climb": num(round(it["climb_max"], 3), "observed", list=it["climb_list"])
                if it["climb_max"] is not None
                else None,
                "spread": num(
                    it["spread"], "observed", countries=list(it["spread_countries"] or [])
                ),
                "play": (
                    {
                        "package": it["play_id"],
                        "bucket": it["installs_bucket"],
                        "min_installs": it["min_installs"],
                        "real_installs": it["real_installs"],
                        "per_day": round(it["iv"], 1) if it["iv"] is not None else None,
                        "method": it["iv_method"],
                        "window_days": it["iv_window_days"],
                        "released": _iso(it["play_released"]),
                        "basis": "store_reported",
                        "source": "play_details_page",
                    }
                    if it["play_id"]
                    else None
                ),
                "estimate": est,
                "profile": _tags(con, key),
                "evidence": ev,
            }
        )
    trend_rows = _rows(
        con,
        "SELECT * FROM fct_mr_trend_weekly WHERE week = (SELECT MAX(week) "
        "FROM fct_mr_trend_weekly) ORDER BY country, share DESC, mechanic",
    )
    hist = _rows(con, "SELECT COUNT(DISTINCT snapshot_date) AS d FROM stg_mr_chart")
    trends = {
        "history_days": num(hist[0]["d"] if hist else 0, "observed"),
        "week": _iso(trend_rows[0]["week"]) if trend_rows else None,
        "rows": [
            {
                "country": r["country"],
                "mechanic": r["mechanic"],
                "share": r["share"],
                "prior_mean": r["prior_mean"],
                "delta": r["delta"],
                "z": None if r["z"] is None else round(r["z"], 2),
                "n_apps": r["n_apps"],
                "days": r["days"],
                "new_entries": r["new_entries"],
                "tagged_share": r["tagged_share"],
                "is_trend": bool(r["is_trend"]),
                "prior_weeks": r["prior_weeks_seen"],
                "basis": "observed",
            }
            for r in trend_rows
        ],
    }
    pubs = _rows(
        con,
        "SELECT DISTINCT a.app_id, a.name, a.developer, a.release_date FROM raw_mr_app a "
        "WHERE a.source = 'publisher' AND a.snapshot_date = (SELECT MAX(snapshot_date) "
        "FROM raw_mr_app WHERE source = 'publisher') AND a.release_date >= ? - 30 "
        "AND a.release_date <= ? ORDER BY a.release_date DESC, a.app_id",
        [day, day],
    )
    facts = {
        "as_of": day.isoformat(),
        "countries": s.countries,
        "items": out_items,
        "class_counts": {k: {"value": v, "basis": "observed"} for k, v in sorted(seen.items())},
        "trends": trends,
        "publisher_releases": [
            {
                "app_id": p["app_id"],
                "name": p["name"],
                "developer": p["developer"],
                "release": num(_iso(p["release_date"]), "observed"),
            }
            for p in pubs
        ],
        "ideas": ideas(out_items, tax),
        "meta": meta or {},
        "quality": {"checks": [c if isinstance(c, dict) else c.__dict__ for c in checks or []]},
    }
    problems = check_labels(facts)
    if problems:
        raise LabelError("; ".join(problems[:5]))
    return facts


def ideas(items: list[dict[str, Any]], tax: Taxonomy) -> list[dict[str, Any]]:
    out = []
    counts: dict[str, int] = {}
    for it in items:
        prim = _first(it["profile"], "mechanic_primary")
        if not prim:
            continue
        m = prim["value"]
        counts[m] = counts.get(m, 0) + 1
        out.append(
            {
                "app_key": it["app_key"],
                "name": it["name"],
                "class": it["class"],
                "mechanic": m,
                "quote": prim["quote"],
                "field": prim["field"],
                "source": it["profile"]["source"],
                "twists": [c["value"] for c in _all(it["profile"], "twist")],
                "themes": [c["value"] for c in _all(it["profile"], "theme")],
                "forge_mechanic": tax.forge(m),
                "hooks": tax.hooks(m),
                "gamerefinery": tax.gamerefinery(m),
                "evidence": it["evidence"][:3],
            }
        )
    if counts:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out.insert(
            0,
            {
                "kind": "top_mechanic",
                "mechanic": top[0],
                "count": {"value": top[1], "basis": "observed"},
            },
        )
    return out


# -- rendering ----------------------------------------------------------------------------


def _list_name(lang: str, list_key: str) -> str:
    return LIST[lang].get(list_key, list_key)


def _country(lang: str, cc: str) -> str:
    return COUNTRY[lang].get(cc, cc.upper())


def _label(lang: str, basis: str, source: str | None = None) -> str:
    lab = LABELS[lang][basis]
    return f"({lab}{', ' + source if source else ''})"


def render(facts: dict[str, Any], lang: str, narration: str | None = None) -> str:
    t = T[lang]
    L: list[str] = []
    day = facts["as_of"]
    L.append(f"# {t['title']} · {day}")
    L.append("")
    meta = facts.get("meta") or {}
    countries = ", ".join(
        _country(lang, c) for c in meta.get("countries_observed") or facts["countries"]
    )
    L.append(t["intro"].format(lists=meta.get("lists_observed", "?"), countries=countries))
    L.append("")
    if narration:
        L += [f"## {t['summary']}", "", f"{narration} {t['narr_note']}", ""]
    for cls in ("breakout", "watch", "late_riser"):
        L.append(f"## {t[cls]}")
        L.append("")
        group = [i for i in facts["items"] if i["class"] == cls]
        if not group:
            L += [t["none"], ""]
        total = ((facts.get("class_counts") or {}).get(cls) or {}).get("value", len(group))
        if total > len(group):
            L += [t["shown"].format(n=len(group), total=total), ""]
        for n, it in enumerate(group, start=1):
            L += _render_item(it, lang, n)
    L += _render_trends(facts, lang)
    if facts.get("publisher_releases"):
        L += [f"## {t['publishers']}", ""]
        for p in facts["publisher_releases"]:
            L.append(
                f"- {p['name']} ({p['developer']}): {p['release']['value']} "
                f"{_label(lang, 'observed', 'Lookup')}"
            )
        L.append("")
    L += [f"## {t['ideas']}", ""]
    ideas_ = facts.get("ideas") or []
    if not ideas_:
        L.append(t["no_ideas"])
    for idea in ideas_:
        if idea.get("kind") == "top_mechanic":
            L.append(
                "- "
                + t["idea_count"].format(m=idea["mechanic"], n=fmt(idea["count"]["value"], lang))
            )
            continue
        twist = t["twist_w"].format(v=", ".join(idea["twists"])) if idea["twists"] else ""
        theme = t["theme_w"].format(v=", ".join(idea["themes"])) if idea["themes"] else ""
        L.append(
            "- "
            + t["idea"].format(
                name=idea["name"],
                mech=idea["mechanic"],
                src=t["src_llm"] if idea.get("source") == "llm" else t["src_keyword"],
                twist=twist,
                theme=theme,
                forge=idea["forge_mechanic"],
                hooks="/".join(idea["hooks"]),
                ev=", ".join(idea["evidence"]),
            )
        )
    L.append("")
    L += _render_quality(facts, lang)
    return "\n".join(L).rstrip() + "\n"


def _render_item(it: dict[str, Any], lang: str, n: int) -> list[str]:
    t = T[lang]
    L = [f"### {n}. {it['name']} ({it['developer']}) · {CLASS[lang][it['class']]}", ""]
    m = it["momentum"]
    c = it["components"]
    parts = t["parts"].format(
        **{
            k: fmt(c[x]["value"], lang, 2)
            for k, x in (
                ("v", "s_vel"),
                ("c", "s_climb"),
                ("r", "s_rank_best"),
                ("s", "s_spread"),
                ("n", "s_new"),
            )
        }
    )
    L.append(f"- {t['score']}: {fmt(m['value'], lang, 2)} {_label(lang, m['basis'])}; {parts}")
    ranks = "; ".join(
        f"{_country(lang, r['country'])} {_list_name(lang, r['list'])} #{r['rank']}"
        for r in it["ranks"]
    )
    L.append(f"- {t['lists']}: {ranks} {_label(lang, 'observed', 'Apple RSS')}")
    a = it["age"]
    src = t["src_lookup"] if a["release_source"] == "lookup" else t["src_feed"]
    L.append(
        f"- {t['age']}: "
        + t["age_text"].format(
            d=fmt(a["value"], lang), rel=a["release_date"], src=src, fs=a["first_seen"]
        )
        + f" {_label(lang, 'observed')}"
    )
    for r in it["ratings"][:4]:
        if r["reset"]:
            how = t["reset"]
        elif r["method"] == "unconfirmed_drop":
            how = t["unconfirmed"]
        elif r["per_day"] is None:
            how = ""
        else:
            how = (
                t["per_day"].format(v=fmt(r["per_day"], lang, 1))
                + " ("
                + (
                    t["lifetime"]
                    if r["method"] == "lifetime"
                    else t["window"].format(d=r["window_days"])
                )
                + ")"
            )
        L.append(
            f"- {t['ratings']} {_country(lang, r['country'])}: {fmt(r['count'], lang)}"
            f"{', ' + how if how else ''} {_label(lang, 'observed', 'Lookup')}"
        )
    p = it.get("play")
    if p:
        txt = t["play_text"].format(
            bucket=p["bucket"], real=fmt(p["real_installs"], lang), rel=p["released"] or "-"
        )
        if p["per_day"] is not None:
            meth = (
                t["lifetime"]
                if p["method"] == "lifetime"
                else t["window"].format(d=p.get("window_days") or "-")
            )
            txt += "; " + t["play_vel"].format(v=fmt(p["per_day"], lang), m=meth)
        L.append(f"- {t['play']}: {txt} {_label(lang, 'store_reported')}")
    else:
        L.append(f"- {t['play']}: {t['no_play']}")
    e = it.get("estimate")
    if e:
        if not all(e.get(k) not in (None, "") for k in ("low", "high", "method", "n")):
            raise LabelError("refusing to render an estimate without a range")
        lo_pct = fmt(e["ratio_low"] * 100, lang, 2)
        hi_pct = fmt(e["ratio_high"] * 100, lang, 2)
        rng = t["rng_iqr"] if e.get("ratio_range") == "iqr" else t["rng_minmax"]
        meth = t["est_method"].format(lo=lo_pct, hi=hi_pct, rng=rng, n=e["n"])
        L.append(
            f"- {t['estimate'].format(c=_country(lang, e['country']))}: "
            f"{fmt_compact(e['low'], lang)}–{fmt_compact(e['high'], lang)} "
            f"({LABELS[lang]['estimate']}: {meth})"
        )
    prof = it["profile"]
    prim = _first(prof, "mechanic_primary")
    bits = []
    if prim:
        bits.append(f'{prim["value"]} ("{prim["quote"]}"; {prim["field"]}, {prof["source"]})')
    else:
        bits.append(t["unknown_mech"])
    for dim in ("mechanic", "twist", "theme", "meta", "monetization_feature"):
        for cl in _all(prof, dim):
            bits.append(f'{t["dims"][dim]} {cl["value"]} ("{cl["quote"]}"; {cl["field"]})')
    for cl in prof["claims"]:
        if cl["dimension"].startswith("visual_"):
            bits.append(f"{cl['dimension'][7:]} {cl['value']} ({t['visual']}, {cl['field']})")
    mono = _first(prof, "monetization_model")
    if mono:
        bits.append(f"{t['mono']} {mono['value']} {_label(lang, 'store_reported', mono['field'])}")
    summ = _first(prof, "twist_summary")
    if summ:
        bits.append(f"LLM: {summ['value']}")
    L.append(f"- {t['what']}: " + "; ".join(bits))
    L.append(f"- {t['evidence']}: " + ", ".join(it["evidence"][:6]))
    L.append(f"- {it['store_url']}" + (f" · {it['play_url']}" if it.get("play_url") else ""))
    L.append("")
    return L


def _render_trends(facts: dict[str, Any], lang: str) -> list[str]:
    t = T[lang]
    tr = facts["trends"]
    L = [f"## {t['trends']}", ""]
    rows = tr["rows"]
    enough = any(r["prior_weeks"] >= 4 for r in rows)
    if not enough:
        by_c: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_c.setdefault(r["country"], []).append(r)
        L.append(
            t["trend_short"].format(
                d=fmt(tr["history_days"]["value"], lang),
                c=", ".join(_country(lang, c) for c in by_c),
            )
        )
        for cc, rs in by_c.items():
            top = [r for r in rs if r["n_apps"] > 0][:5]
            if not top:
                continue
            L.append(
                f"- {_country(lang, cc)}: "
                + "; ".join(
                    t["trend_row"].format(
                        m=r["mechanic"],
                        share=fmt(r["share"] * 100, lang, 1),
                        apps=r["n_apps"],
                        days=r["days"],
                    )
                    for r in top
                )
                + f" {_label(lang, 'observed')}"
            )
            L.append("  " + t["coverage"].format(v=fmt(rs[0]["tagged_share"] * 100, lang, 0)))
    else:
        hits = [r for r in rows if r["is_trend"]]
        if not hits:
            L.append(t["trend_none"])
        for r in hits:
            L.append(
                "- "
                + t["trend_hit"].format(
                    c=_country(lang, r["country"]),
                    m=r["mechanic"],
                    share=fmt(r["share"] * 100, lang, 1),
                    prev=fmt(r["prior_mean"] * 100, lang, 1),
                    z=fmt(r["z"], lang, 1),
                    apps=r["n_apps"],
                )
                + f" {_label(lang, 'observed')}"
            )
    L.append("")
    return L


def _render_quality(facts: dict[str, Any], lang: str) -> list[str]:
    t = T[lang]
    meta = facts.get("meta") or {}
    checks = facts.get("quality", {}).get("checks") or []
    passed = sum(c["status"] == "pass" for c in checks)
    err = sum(c["severity"] == "error" and c["status"] != "pass" for c in checks)
    warn = sum(c["severity"] == "warn" and c["status"] != "pass" for c in checks)
    L = [
        f"## {t['quality']}",
        "",
        "- " + t["q_line"].format(passed=passed, total=len(checks), err=err, warn=warn),
    ]
    for c in checks:
        if c["status"] != "pass":
            L.append(f"  - [{c['severity']}] {c['name']}: {c['message']}")
    req = meta.get("requests") or {}
    if req:
        hosts = ", ".join(f"{h} {n}" for h, n in (req.get("by_host") or {}).items())
        L.append(
            "- "
            + t["q_req"].format(
                n=req.get("sent", 0), hosts=hosts or "-", c=req.get("cache_hits", 0)
            )
        )
    llm = meta.get("llm")
    if llm:
        cap = llm.get("budget_usd")
        L.append(
            "- "
            + t["q_llm"].format(
                p=llm["provider"],
                m=llm["model"],
                r=llm["requests"],
                cost=f"{llm['cost_usd']:.4f}",
                cap="-" if cap is None else f"{cap:.2f}",
            )
        )
    else:
        L.append("- " + t["q_llm_none"])
    return L


# -- narration ----------------------------------------------------------------------------

_NUM = re.compile(
    r"(?<![\w])([-+]?\d[\d.,]*)(\s*(?:k|bin|thousand|bn|mn|m|million|milyon|milyar|billion)\b)?",
    re.IGNORECASE,
)
_SCALE = {
    "k": 1e3,
    "bin": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "milyon": 1e6,
    "bn": 1e9,
    "milyar": 1e9,
    "billion": 1e9,
}


def _candidates(token: str) -> set[float]:
    tok = token.strip(".,")
    out: set[float] = set()
    for thou, dec in ((",", "."), (".", ",")):
        t = tok.replace(thou, "").replace(dec, ".")
        try:
            out.add(float(t))
        except ValueError:
            pass
    return out


def fact_numbers(facts: dict[str, Any]) -> set[float]:
    nums: set[float] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            nums.add(float(node))
        elif isinstance(node, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", node):
            y, m, d = node.split("-")
            nums.update({float(y), float(m), float(d)})

    walk({k: v for k, v in facts.items() if k not in ("meta", "quality")})
    return nums


def verify_narration(text: str, facts: dict[str, Any]) -> list[str]:
    """Numbers in `text` that no fact supports (empty: the narration may be shown).

    Plain numbers must match a fact (rounding tolerance; a fraction may be written as a
    percentage); a number with a unit (544k, 1,2 bin, 3 million) must be within 5 % of a fact."""
    allowed = fact_numbers(facts)
    scaled = allowed | {round(a * 100, 6) for a in allowed if abs(a) <= 1}
    bad = []
    for m in _NUM.finditer(text or ""):
        unit = (m.group(2) or "").strip().lower()
        cands = _candidates(m.group(1))
        if unit:
            mult = _SCALE[unit]
            ok = any(abs(c * mult - a) <= abs(a) * 0.05 for c in cands for a in allowed if a)
        else:
            ok = any(abs(c - a) <= max(0.051, abs(a) * 0.005) for c in cands for a in scaled)
        if not ok:
            bad.append(m.group(0).strip())
    return bad


NARRATE_SYSTEM = """You write a short summary of a daily mobile-game market radar in Turkish and
English. Use only the facts given as JSON. Do not add any number, date or claim that is not in
the facts; prefer no numbers at all. Do not estimate downloads or revenue. Two to four
sentences per language."""


def narrate(
    facts: dict[str, Any], client: LLMClient | None, log: Callable[[str], None] = print
) -> dict[str, Any]:
    if client is None or not facts["items"]:
        return {"status": "skipped"}
    brief = {
        "as_of": facts["as_of"],
        "items": [
            {
                "name": i["name"],
                "developer": i["developer"],
                "class": i["class"],
                "ranks": [
                    {"country": r["country"], "list": r["list"], "rank": r["rank"]}
                    for r in i["ranks"][:3]
                ],
                "age_days": i["age"]["value"],
                "mechanic": (_first(i["profile"], "mechanic_primary") or {}).get("value"),
                "themes": [c["value"] for c in _all(i["profile"], "theme")],
            }
            for i in facts["items"][:6]
        ],
    }
    req = LLMRequest(
        prompt="Facts (JSON):\n" + json.dumps(brief, ensure_ascii=False),
        system=NARRATE_SYSTEM,
        schema={
            "type": "object",
            "properties": {
                "tr": {"type": "string", "maxLength": 900},
                "en": {"type": "string", "maxLength": 900},
            },
            "required": ["tr", "en"],
            "additionalProperties": False,
        },
        max_output_tokens=900,
        purpose="radar_narrate",
    )
    try:
        res = client.generate(req)
    except LLMError as exc:
        log(f"    radar: narration skipped ({exc})")
        return {"status": "error", "error": str(exc)[:200]}
    bad = {lang: verify_narration(res.data.get(lang, ""), facts) for lang in ("tr", "en")}
    if any(bad.values()):
        return {"status": "rejected", "unsupported_numbers": bad}
    return {"status": "ok", "tr": res.data["tr"], "en": res.data["en"]}
