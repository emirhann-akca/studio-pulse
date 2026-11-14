"""`pulse serve` game page: /radar/oyun/<apple id | play package> (local only).

Rank timeline per country (one small chart per country, one line per list), the day's
estimate bands with their basis and calibration status, the version/update timeline,
rating velocity per storefront and the Play install counter. Everything is read from the
local warehouse; nothing here reaches the static dashboard or the public demo.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...config import Config
from ..digest import COUNTRY, LABELS, LIST, fmt, fmt_compact
from .stage import KINDS

LIST_ORDER = [
    "topfreeapplications:6014",
    "topfreeapplications:7012",
    "topfreeapplications:7003",
    "topfreeapplications:7015",
    "topgrossingapplications:6014",
    "newfreeapplications:6014",
    "toppaidapplications:6014",
]

T = {
    "tr": {
        "back": "Pazar radarı",
        "local": "Yerel sayfa: mağaza verisi bu bilgisayarda kalır.",
        "ranks": "Liste sıraları (ülke başına)",
        "ranks_none": "Bu oyun radarın listelerinde hiç görülmedi.",
        "est": "Tahminler",
        "est_none": "Bugün bu oyun için tahmin yok.",
        "kind": "Ne",
        "band": "Bant",
        "mid": "Orta",
        "basis": "Dayanak",
        "calib": "Kalibrasyon durumu",
        "insufficient": "yetersiz veri",
        "no_source": "kamu kaynağı yok",
        "ok": "hazır",
        "acc": "Doğruluk (örneklem dışı)",
        "versions": "Sürüm / güncelleme zaman çizelgesi",
        "versions_none": "Sürüm bilgisi yok.",
        "ratings": "Puan sayısı ve hızı (App Store, ülke başına)",
        "play": "Google Play kurulum sayacı",
        "play_none": "Play eşlemesi ya da okuması yok.",
        "windows": "Kullanılabilir sayaç pencereleri",
        "watch": "İzleme listesinde",
        "not_watched": "İzleme listesinde değil: pulse radar watch add {k}",
        "date": "Tarih",
        "country": "Ülke",
        "list": "Liste",
        "rank": "Sıra",
        "count": "Puan sayısı",
        "per_day": "Puan/gün",
        "installs": "realInstalls",
        "refresh": "Sayaç",
        "refreshed": "yenilendi",
        "stale": "yenilenmedi (puan arttı)",
        "same": "aynı",
        "version": "Sürüm",
        "released": "Mağaza tarihi",
        "first_seen": "Radarda ilk",
        "not_found": "Bu kimlik radarın ambarında yok.",
        "rank_hint": "Dikey eksen: sıra (logaritmik, 1 üstte). Noktanın üstüne gelince tarih ve sıra görünür.",
        "table": "Tablo",
        "history": "Önceki günlerin bantları",
        "status": "Durum",
        "n": "n",
        "scheme": "Yöntem",
        "coverage": "Bant içinde",
        "unit_note": "Bantlar %{pct} aralıktır; TAHMİN etiketli her sayı aralık, yöntem ve n ile gelir.",
    },
    "en": {
        "back": "Market radar",
        "local": "Local page: store data stays on this computer.",
        "ranks": "Chart ranks (per country)",
        "ranks_none": "This game was never seen in the radar's lists.",
        "est": "Estimates",
        "est_none": "No estimate for this game today.",
        "kind": "What",
        "band": "Band",
        "mid": "Middle",
        "basis": "Basis",
        "calib": "Calibration status",
        "insufficient": "insufficient data",
        "no_source": "no public source",
        "ok": "ready",
        "acc": "Accuracy (out of sample)",
        "versions": "Version / update timeline",
        "versions_none": "No version data.",
        "ratings": "Rating count and velocity (App Store, per storefront)",
        "play": "Google Play install counter",
        "play_none": "No Play mapping or read.",
        "windows": "Usable counter windows",
        "watch": "On the watchlist",
        "not_watched": "Not on the watchlist: pulse radar watch add {k}",
        "date": "Date",
        "country": "Country",
        "list": "List",
        "rank": "Rank",
        "count": "Ratings",
        "per_day": "Ratings/day",
        "installs": "realInstalls",
        "refresh": "Counter",
        "refreshed": "refreshed",
        "stale": "not refreshed (ratings grew)",
        "same": "same",
        "version": "Version",
        "released": "Store date",
        "first_seen": "First seen",
        "not_found": "This id is not in the radar's warehouse.",
        "rank_hint": "Vertical axis: rank (log scale, 1 at the top). Hover a point for its date and rank.",
        "table": "Table",
        "history": "Bands of earlier days",
        "status": "Status",
        "n": "n",
        "scheme": "Scheme",
        "coverage": "Inside the band",
        "unit_note": "Bands are {pct}% intervals; every ESTIMATE comes with its range, method and n.",
    },
}


def _rows(con, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    try:
        cur = con.execute(sql, params)
    except Exception:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def resolve(con, key: str) -> tuple[list[str], list[str]]:
    """(apple ids, play packages) for an Apple id or a Play package."""
    if key.isdigit():
        apple = [key]
        play = [
            r["play_id"]
            for r in _rows(
                con,
                "SELECT DISTINCT play_id FROM raw_mr_xref WHERE apple_id = ? AND status = 'mapped' "
                "UNION SELECT DISTINCT app_id FROM raw_mr_app WHERE store = 'play' AND app_id IN "
                "(SELECT bundle_id FROM raw_mr_chart WHERE app_id = ? UNION "
                "SELECT bundle_id FROM raw_mr_app WHERE store = 'apple' AND app_id = ?)",
                [key, key, key],
            )
            if r["play_id"]
        ]
        return apple, sorted(set(play))
    apple = [
        r["a"]
        for r in _rows(
            con,
            "SELECT DISTINCT apple_id AS a FROM raw_mr_xref WHERE play_id = ? AND status = 'mapped' "
            "UNION SELECT DISTINCT app_id FROM raw_mr_app WHERE store = 'apple' AND bundle_id = ? "
            "UNION SELECT DISTINCT app_id FROM raw_mr_chart WHERE bundle_id = ?",
            [key, key, key],
        )
    ]
    return sorted(set(apple)), [key]


def rank_svg(points: list[dict[str, Any]], lang: str, width: int = 640, height: int = 200) -> str:
    """One country's rank lines (one per list) as inline SVG, colours from the UI's series
    tokens in a fixed list order, a <title> tooltip on every point."""
    if not points:
        return ""
    days = sorted({p["snapshot_date"] for p in points})
    d0, d1 = days[0], days[-1]
    span = max(1, (d1 - d0).days)
    pad_l, pad_r, pad_t, pad_b = 40, 14, 10, 26
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    top = 1
    bottom = max(100, max(p["rank"] for p in points))

    def x(d: dt.date) -> float:
        return pad_l + (w * (d - d0).days / span if span else w / 2)

    def y(r: int) -> float:
        return pad_t + h * math.log(r / top) / math.log(bottom / top)

    out = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'style="max-width:{width}px;display:block" aria-label="rank timeline">'
    ]
    for r in (1, 10, 50, 100):
        if r <= bottom:
            out.append(
                f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(r):.1f}" y2="{y(r):.1f}" '
                f'stroke="var(--grid)" stroke-width="1"/>'
                f'<text x="{pad_l - 6}" y="{y(r) + 4:.1f}" text-anchor="end" font-size="12" '
                f'fill="var(--muted)">{r}</text>'
            )
    for d in sorted({d0, d1}):
        out.append(
            f'<text x="{x(d):.1f}" y="{height - 6}" text-anchor="{"start" if d == d0 else "end"}" '
            f'font-size="12" '
            f'fill="var(--muted)">{d.isoformat()}</text>'
        )
    lists = [lk for lk in LIST_ORDER if any(p["list"] == lk for p in points)]
    lists += sorted({p["list"] for p in points} - set(lists))
    for lk in lists:
        color = f"var(--s{LIST_ORDER.index(lk) + 1 if lk in LIST_ORDER else 8})"
        ser = sorted((p for p in points if p["list"] == lk), key=lambda p: p["snapshot_date"])
        name = LIST[lang].get(lk, lk)
        # a gap of more than one day (the list was not observed or the game was not listed)
        # breaks the line instead of drawing a fake connection
        segs: list[list[dict[str, Any]]] = []
        for p in ser:
            if segs and (p["snapshot_date"] - segs[-1][-1]["snapshot_date"]).days == 1:
                segs[-1].append(p)
            else:
                segs.append([p])
        for seg in segs:
            if len(seg) > 1:
                pts = " ".join(f"{x(p['snapshot_date']):.1f},{y(p['rank']):.1f}" for p in seg)
                out.append(
                    f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>'
                )
        for p in ser:
            out.append(
                f'<circle cx="{x(p["snapshot_date"]):.1f}" cy="{y(p["rank"]):.1f}" r="4" '
                f'fill="{color}" stroke="var(--surface)" stroke-width="2"><title>{name} · '
                f"{p['snapshot_date'].isoformat()} · #{p['rank']}</title></circle>"
            )
    out.append("</svg>")
    return "".join(out)


def game_view(con, key: str, lang: str) -> dict[str, Any]:
    apple, play = resolve(con, key)
    name = _rows(
        con,
        "SELECT name, developer, store FROM raw_mr_app WHERE list_contains(?::VARCHAR[], app_id) "
        "ORDER BY store = 'apple' DESC, snapshot_date DESC LIMIT 1",
        [apple + play],
    )
    ranks = _rows(
        con,
        "SELECT snapshot_date, country, chart || ':' || genre_id AS list, MIN(rank) AS rank "
        "FROM stg_mr_chart WHERE list_contains(?::VARCHAR[], app_id) GROUP BY 1, 2, 3 "
        "ORDER BY 2, 3, 1",
        [apple],
    )
    by_country: dict[str, list[dict[str, Any]]] = {}
    for r in ranks:
        by_country.setdefault(r["country"], []).append(r)
    charts = [
        {
            "country": cc,
            "name": COUNTRY[lang].get(cc, cc.upper()),
            "svg": rank_svg(pts, lang),
            "lists": [
                {
                    "name": LIST[lang].get(lk, lk),
                    "color": f"var(--s{LIST_ORDER.index(lk) + 1 if lk in LIST_ORDER else 8})",
                    "last": next(p for p in reversed(pts) if p["list"] == lk),
                }
                for lk in LIST_ORDER + sorted({p["list"] for p in pts} - set(LIST_ORDER))
                if any(p["list"] == lk for p in pts)
            ],
        }
        for cc, pts in sorted(by_country.items())
    ]
    est = _rows(
        con,
        "SELECT * FROM raw_mr_est_game WHERE list_contains(?::VARCHAR[], app_id) AND "
        "snapshot_date = (SELECT MAX(snapshot_date) FROM raw_mr_est_game WHERE "
        "list_contains(?::VARCHAR[], app_id)) ORDER BY kind, country, list_key",
        [apple, apple],
    )
    for e in est:
        e["kind_name"] = KINDS.get(e["kind"], {}).get(lang, e["kind"])
        e["list_name"] = LIST[lang].get(e["list_key"].split(":", 1)[1], e["list_key"])
    est_hist = _rows(
        con,
        "SELECT snapshot_date, kind, country, list_key, rank, low, mid, high FROM raw_mr_est_game "
        "WHERE list_contains(?::VARCHAR[], app_id) ORDER BY snapshot_date DESC, kind, country "
        "LIMIT 60",
        [apple],
    )
    lists_today = sorted({f"{r['country']}:{r['list']}" for r in ranks}) if ranks else []
    calib = _rows(
        con,
        "SELECT * FROM raw_mr_est_fit WHERE computed_on = (SELECT MAX(computed_on) FROM "
        "raw_mr_est_fit) AND list_contains(?::VARCHAR[], list_key) ORDER BY kind, list_key",
        [lists_today],
    )
    for c in calib:
        c["list_name"] = (
            COUNTRY[lang].get(c["country"], c["country"].upper())
            + " · "
            + LIST[lang].get(c["list_key"].split(":", 1)[1], c["list_key"])
        )
    acc = _rows(
        con,
        "SELECT * FROM raw_mr_est_backtest WHERE computed_on = (SELECT MAX(computed_on) FROM "
        "raw_mr_est_backtest) AND (list_contains(?::VARCHAR[], list_key) OR kind = 'revenue') "
        "ORDER BY kind, list_key, scheme",
        [lists_today],
    )
    versions = _rows(
        con,
        "SELECT version, MIN(version_date) AS version_date, MIN(first_seen) AS first_seen, "
        "MAX(last_seen) AS last_seen, string_agg(DISTINCT store || ':' || country, ', ') AS seen_in, "
        "any_value(release_notes) AS release_notes FROM fct_mr_version_timeline "
        "WHERE list_contains(?::VARCHAR[], app_id) GROUP BY version "
        "ORDER BY MIN(version_date) DESC NULLS LAST, MIN(first_seen) DESC LIMIT 30",
        [apple + play],
    )
    ratings = _rows(
        con,
        "SELECT snapshot_date, country, rating_count, rv, rv_method FROM stg_mr_rating_daily "
        "WHERE list_contains(?::VARCHAR[], app_id) ORDER BY snapshot_date DESC, country LIMIT 80",
        [apple],
    )
    counter = _rows(
        con,
        "SELECT * FROM stg_mr_play_counter WHERE list_contains(?::VARCHAR[], play_id) "
        "ORDER BY snapshot_date DESC LIMIT 60",
        [play],
    )
    windows = _rows(
        con,
        "SELECT * FROM raw_mr_est_window WHERE computed_on = (SELECT MAX(computed_on) FROM "
        "raw_mr_est_window) AND list_contains(?::VARCHAR[], play_id) ORDER BY t0",
        [play],
    )
    watched = _rows(
        con,
        "SELECT store, app_id, added_at, note FROM raw_mr_watch WHERE removed_at IS NULL "
        "AND list_contains(?::VARCHAR[], app_id)",
        [apple + play],
    )
    return {
        "key": key,
        "found": bool(name or ranks or est),
        "name": name[0]["name"] if name else key,
        "developer": name[0]["developer"] if name else None,
        "apple_ids": apple,
        "play_ids": play,
        "charts": charts,
        "estimates": est,
        "estimate_history": est_hist,
        "calibration": calib,
        "accuracy": acc,
        "versions": versions,
        "ratings": ratings,
        "counter": counter,
        "windows": windows,
        "watched": watched,
    }


def register(app: FastAPI, cfg: Config, templates: Any, context: Any, open_db: Any) -> None:
    @app.get("/radar/oyun/{key}", response_class=HTMLResponse)
    def radar_game(request: Request, key: str, lang: str | None = None):
        from ...serve.ui import request_lang
        from .settings import from_config

        lang = request_lang(request, lang)
        con, _source = open_db(cfg)
        try:
            view = game_view(con, key, lang) if con is not None else None
        finally:
            if con is not None:
                con.close()
        es = from_config(cfg)
        return templates.TemplateResponse(
            request,
            "radar_game.html",
            context(
                request,
                "radar",
                chosen="@market",
                source="files",
                view=view,
                key=key,
                lang=lang,
                t=T[lang],
                labels=LABELS[lang],
                band_pct=es.band_pct,
                fmt=lambda x, d=0: fmt(x, lang, d),
                fc=lambda x: fmt_compact(x, lang) if x is not None else "-",
            ),
            status_code=200 if view and view["found"] else 404,
        )
