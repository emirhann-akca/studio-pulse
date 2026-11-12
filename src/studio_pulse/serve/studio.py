"""Studio: one window for every app (`/studio`, what the desktop app opens).

Left: the apps. Configured apps (GA4 / store exports) and every app the SDK collector
has registered, with its status (live / testing / new, from the collector's registry)
and how many installs are active right now; a game whose SDK sends its first event
appears at the bottom without a refresh. Below them: Market radar, Ads (creative-forge,
when its app is running), data health, alerts.

Middle: the selected app. Its 7-day numbers from fct_app_summary, the last 30 days of
DAU, players right now, store presence (registry), the hardest levels and the insights
the brain stored for it.

Right: the app's AI panel. The brain's recommendations for this app (each with its
evidence id) and a question box scoped to the app; answers come from the same router as
/ask (verified queries, the catalog, then a checked SELECT), so numbers are code-formatted
and verified, never invented.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

STATUS_ORDER = {"live": 0, "testing": 1, "new": 2, "ga4": 3}

T: dict[str, dict[str, str]] = {
    "tr": {
        "apps": "Uygulamalar",
        "new_section": "Yeni gelenler",
        "tools": "Araçlar",
        "radar": "Pazar radarı",
        "ads": "Reklamlar (Creative Forge)",
        "health": "Veri sağlığı",
        "alerts": "Uyarılar",
        "dashboard": "Ayrıntılı pano",
        "live": "canlı",
        "testing": "test",
        "new": "yeni",
        "ga4": "GA4",
        "now": "şu an",
        "players_now": "Şu an oynayan",
        "dau7": "Günlük aktif (7 gün ort.)",
        "new7": "Yeni kullanıcı (7 gün)",
        "d1": "D1 elde tutma",
        "d7": "D7 elde tutma",
        "rev7": "Gelir (7 gün)",
        "arpdau": "ARPDAU (7 gün)",
        "crash": "Çökmesiz kullanıcı (7 gün)",
        "rating": "Mağaza puanı",
        "vs_prev": "önceki 7 güne göre",
        "dau30": "Günlük aktif kullanıcı, son 30 gün",
        "store": "Mağaza",
        "listed": "mağazada yayında",
        "not_listed": "mağazada henüz yok",
        "hard_levels": "En zor bölümler",
        "level": "Bölüm",
        "fail_rate": "başarısızlık",
        "quit_here": "burada bırakan",
        "insights": "İçgörüler",
        "no_insights": "Bu uygulama için kayıtlı içgörü yok (önce `pulse run`).",
        "ai": "Yapay zekâ",
        "ai_note": "Sayıları kod hesaplar ve kanıtla karşılaştırır; model yalnız anlatır.",
        "recs": "Öneriler",
        "ask": "Bu uygulamaya sor",
        "ask_ph": "ör. en zor bölümler hangileri?",
        "send": "Sor",
        "no_data": "Ambarda bu uygulamanın verisi yok. SDK verisi `pulse run` ile ambara girer; canlı sayılar yine de solda görünür.",
        "ads_off": "Creative Forge açık değil: simgesinden ya da `forge app` ile açın.",
        "open_full": "Tam sayfa",
        "sdk_page": "SDK ayrıntıları (bölümler, ısı haritası)",
        "collector_down": "Toplayıcıya ulaşılamıyor; canlı sayılar yok.",
        "evidence": "kanıt",
    },
    "en": {
        "apps": "Apps",
        "new_section": "New arrivals",
        "tools": "Tools",
        "radar": "Market radar",
        "ads": "Ads (Creative Forge)",
        "health": "Data health",
        "alerts": "Alerts",
        "dashboard": "Full dashboard",
        "live": "live",
        "testing": "testing",
        "new": "new",
        "ga4": "GA4",
        "now": "now",
        "players_now": "Playing now",
        "dau7": "Daily active (7-day avg)",
        "new7": "New users (7 days)",
        "d1": "D1 retention",
        "d7": "D7 retention",
        "rev7": "Revenue (7 days)",
        "arpdau": "ARPDAU (7 days)",
        "crash": "Crash-free users (7 days)",
        "rating": "Store rating",
        "vs_prev": "vs the 7 days before",
        "dau30": "Daily active users, last 30 days",
        "store": "Store",
        "listed": "listed in the store",
        "not_listed": "not in the store yet",
        "hard_levels": "Hardest levels",
        "level": "Level",
        "fail_rate": "fail rate",
        "quit_here": "quit here",
        "insights": "Insights",
        "no_insights": "No stored insights for this app (run `pulse run` first).",
        "ai": "AI",
        "ai_note": "Code computes the numbers and checks them against evidence; the model only words them.",
        "recs": "Recommendations",
        "ask": "Ask about this app",
        "ask_ph": "e.g. which levels are the hardest?",
        "send": "Ask",
        "no_data": "No warehouse data for this app yet. SDK data reaches the warehouse with `pulse run`; live numbers still show on the left.",
        "ads_off": "Creative Forge is not open: start it from its icon or with `forge app`.",
        "open_full": "Full page",
        "sdk_page": "SDK details (levels, heatmap)",
        "collector_down": "The collector is not reachable; no live numbers.",
        "evidence": "evidence",
    },
}


def _lang(value: str | None) -> str:
    return value if value in T else "tr"


def _pct(v: Any, lang: str, digits: int = 1) -> str:
    if v is None:
        return "—"
    s = f"{float(v) * 100:.{digits}f}"
    return f"%{s.replace('.', ',')}" if lang == "tr" else f"{s}%"


def _num(v: Any, lang: str, digits: int = 0) -> str:
    if v is None:
        return "—"
    s = f"{float(v):,.{digits}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "tr" else s


def _usd(v: Any, lang: str) -> str:
    if v is None:
        return "—"
    return f"${_num(v, lang, 2)}"


def dau_chart(points: list[tuple[Any, Any]], label: str) -> str:
    """The last 30 days of DAU as a small inline SVG area chart (no JS, no library)."""
    vals = [float(v or 0) for _, v in points]
    if len(vals) < 2:
        return ""
    w, h, pad = 560.0, 120.0, 6.0
    top = max(vals) or 1.0
    xs = [pad + i * (w - 2 * pad) / (len(vals) - 1) for i in range(len(vals))]
    ys = [h - pad - v / top * (h - 2 * pad) for v in vals]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    area = f"{xs[0]:.1f},{h - pad:.1f} {line} {xs[-1]:.1f},{h - pad:.1f}"
    first, last = points[0][0], points[-1][0]
    return (
        f'<svg class="dau" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{label}">'
        f'<polygon points="{area}" class="area"/><polyline points="{line}" class="line"/>'
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3.5" class="dot"/></svg>'
        f'<div class="axis"><span>{first}</span><span>{last}</span></div>'
    )


def register(
    app: FastAPI,
    cfg: Any,
    templates: Any,
    *,
    context: Any,
    open_db: Any,
    rows: Any,
    hosts: set[str],
) -> None:
    def live() -> dict[str, Any]:
        fn = getattr(app.state, "live_data", None)
        return fn() if fn else {"snap": None, "error": "off"}

    def config_id(app_id: str) -> str:
        from ..collector.web import slug

        return getattr(app.state, "bundle_to_app", {}).get(app_id) or slug(app_id)

    def app_list(con: Any, lv: dict[str, Any]) -> list[dict[str, Any]]:
        meta = rows(con, "SELECT app_id, name, genre, platforms FROM meta_apps ORDER BY app_id")
        reg = rows(con, "SELECT app_id, platform, status FROM fct_sdk_apps")
        out: dict[str, dict[str, Any]] = {}
        for m in meta:
            out[m["app_id"]] = {
                "id": m["app_id"],
                "name": m["name"] or m["app_id"],
                "genre": m["genre"],
                "status": "ga4",
                "now": None,
                "arrived": False,
            }
        for r in reg:
            a = out.setdefault(r["app_id"], {"id": r["app_id"], "name": r["app_id"],
                                             "genre": None, "status": "new", "now": None,
                                             "arrived": True})  # fmt: skip
            if (
                STATUS_ORDER.get(r["status"], 9) < STATUS_ORDER.get(a["status"], 9)
                or a["status"] == "ga4"
            ):
                a["status"] = r["status"]
        snap = lv.get("snap")
        for s in (snap or {}).get("apps", []):
            cid = config_id(s["app_id"])
            a = out.get(cid)
            if a is None:  # registered by the collector, not in the warehouse yet
                a = out[cid] = {"id": cid, "name": s.get("name") or cid, "genre": s.get("genre"),
                                "status": s.get("status", "new"), "now": 0, "arrived": True}  # fmt: skip
            else:
                if (
                    a["status"] == "ga4"
                    or STATUS_ORDER.get(s.get("status"), 9) < STATUS_ORDER[a["status"]]
                ):
                    a["status"] = s.get("status", a["status"])
            a["now"] = (a["now"] or 0) + int(s.get("active") or 0)
        new_ids = {config_id(n["app_id"]) for n in (snap or {}).get("new_apps", [])}
        for a in out.values():
            a["arrived"] = a["arrived"] and (a["status"] == "new" or a["id"] in new_ids)
        return sorted(
            out.values(),
            key=lambda a: (a["arrived"], STATUS_ORDER.get(a["status"], 9), a["name"].lower()),
        )

    def app_view(con: Any, app_id: str, lv: dict[str, Any], lang: str) -> dict[str, Any]:
        t = T[lang]
        s = rows(con, "SELECT * FROM fct_app_summary WHERE app_id = ?", [app_id])
        s = s[0] if s else None
        daily = rows(
            con,
            "SELECT date, dau FROM fct_app_daily WHERE app_id = ? AND dau IS NOT NULL "
            "ORDER BY date DESC LIMIT 30",
            [app_id],
        )[::-1]
        reg = rows(con, "SELECT platform, status, store_listed, store_url, store_version, last_version "
                        "FROM fct_sdk_apps WHERE app_id = ? ORDER BY platform", [app_id])  # fmt: skip
        hard = rows(
            con,
            "SELECT level, CAST(fails AS DOUBLE) / NULLIF(completes + fails, 0) AS fail_rate, "
            "churn_rate, completes + fails AS n FROM fct_level_funnel "
            "WHERE app_id = ? AND completes + fails >= 200 ORDER BY fail_rate DESC, level LIMIT 5",
            [app_id],
        )
        sdk_levels = rows(
            con, "SELECT COUNT(*) AS n FROM fct_sdk_level_funnel WHERE app_id = ?", [app_id]
        )
        tiles = []
        if s:
            change = s.get("dau_change")
            tiles = [
                (t["dau7"], _num(s.get("dau_7d"), lang, 0),
                 f"{'+' if (change or 0) >= 0 else ''}{_pct(change, lang)} {t['vs_prev']}" if change is not None else ""),
                (t["new7"], _num(s.get("new_users_7d"), lang), ""),
                (t["d1"], _pct(s.get("d1_retention"), lang), ""),
                (t["d7"], _pct(s.get("d7_retention"), lang), ""),
                (t["rev7"], _usd(s.get("revenue_7d_usd"), lang), ""),
                (t["arpdau"], _usd(s.get("arpdau_7d"), lang), ""),
                (t["crash"], _pct(s.get("crash_free_users_7d"), lang, 2), ""),
                (t["rating"], _num(s.get("rating"), lang, 2) if s.get("rating") else "—", ""),
            ]  # fmt: skip
        now = None
        snap = lv.get("snap")
        if snap:
            mine = [a for a in snap["apps"] if config_id(a["app_id"]) == app_id]
            now = sum(int(a.get("active") or 0) for a in mine) if mine else None  # None: no SDK
        return {
            "summary": s,
            "tiles": tiles,
            "chart": dau_chart([(r["date"], r["dau"]) for r in daily], t["dau30"]),
            "store": reg,
            "hard": [
                {
                    "level": h["level"],
                    "fail": _pct(h["fail_rate"], lang),
                    "quit": _pct(h["churn_rate"], lang),
                    "n": _num(h["n"], lang),
                }
                for h in hard
            ],  # fmt: skip
            "has_sdk_page": bool(sdk_levels and sdk_levels[0]["n"]),
            "now": now,
            "last_date": s.get("last_date") if s else None,
        }

    def creative_forge_url() -> str | None:
        """The running Creative Forge app's URL, from its own app.json, if it answers."""
        import os
        import sys
        import urllib.request

        home = os.environ.get("CREATIVE_FORGE_HOME")
        if home:
            base = Path(home) / "data"
        elif sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "creative-forge"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "creative-forge"
        else:
            base = (
                Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
                / "creative-forge"
            )
        try:
            url = json.loads((base / "app" / "app.json").read_text(encoding="utf-8")).get("url")
            if url:
                with urllib.request.urlopen(url + "__app/health", timeout=1) as r:  # noqa: S310
                    return url if r.status == 200 else None
        except (OSError, ValueError):
            return None
        return None

    def page_data(app_id: str | None, lang: str) -> dict[str, Any]:
        lv = live()
        con, source = open_db(cfg)
        try:
            apps = app_list(con, lv)
            chosen = app_id or (apps[0]["id"] if apps else None)
            view = app_view(con, chosen, lv, lang) if chosen else None
        finally:
            if con is not None:
                con.close()
        current = next((a for a in apps if a["id"] == chosen), None)
        cards: list[dict[str, Any]] = []
        load = getattr(app.state, "brain_insights", None)
        if load and chosen:
            try:
                cards = [c for c in load(lang)["cards"] if c["app"] == chosen]
            except Exception:  # the brain is optional here; the page still shows the data
                cards = []
        recs: list[tuple[str, list[str]]] = []  # one line per distinct action, evidence merged
        for c in cards:
            for text, ev in c.get("actions", []):
                hit = next((r for r in recs if r[0] == text), None)
                if hit is None:
                    recs.append((text, list(ev)))
                else:
                    hit[1].extend(e for e in ev if e not in hit[1])
        recs = recs[:5]
        return {
            "t": T[lang],
            "lang": lang,
            "apps": [a for a in apps if not a["arrived"]],
            "arrived": [a for a in apps if a["arrived"]],
            "current": current,
            "chosen": chosen,
            "view": view,
            "cards": cards[:6],
            "recs": recs,
            "source": source,
            "collector_error": lv.get("error"),
            "forge_url": creative_forge_url(),
        }

    @app.get("/studio", response_class=HTMLResponse)
    def studio_home(request: Request, lang: str | None = None):
        data = page_data(None, _lang(lang))
        if data["chosen"]:
            return RedirectResponse(f"/studio/{data['chosen']}?lang={_lang(lang)}", status_code=307)
        return templates.TemplateResponse(
            request, "studio.html", context(request, "studio", **data)
        )

    @app.get("/studio/fragments/sidebar", response_class=HTMLResponse)
    def studio_sidebar(
        request: Request, chosen: str | None = Query(None, alias="app"), lang: str | None = None
    ):
        data = page_data(chosen, _lang(lang))
        return templates.TemplateResponse(
            request, "_studio_sidebar.html", context(request, "fragment", **data)
        )

    @app.get("/studio/{app_id}", response_class=HTMLResponse)
    def studio_app(request: Request, app_id: str, lang: str | None = None):
        data = page_data(app_id, _lang(lang))
        if data["current"] is None:
            return HTMLResponse(f"unknown app {app_id!r}", 404)
        return templates.TemplateResponse(
            request, "studio.html", context(request, "studio", **data)
        )

    @app.post("/studio/{app_id}/ask", response_class=HTMLResponse)
    async def studio_ask(request: Request, app_id: str):
        token = str(templates.env.globals.get("token", ""))
        if not token or not secrets.compare_digest(request.headers.get("x-pulse-token", ""), token):
            return PlainTextResponse("missing or wrong token", 403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "") not in hosts:
            return PlainTextResponse("cross-origin request refused", 403)
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        q = (form.get("q") or [""])[0].strip()
        lang = _lang((form.get("lang") or ["tr"])[0])
        answer = getattr(app.state, "brain_answer", None)
        bt = getattr(app.state, "brain_templates", None)
        if not q or answer is None or bt is None:
            return HTMLResponse("")
        # scoped to this app: the router reads the app id from the question
        scoped = q if app_id in q else f"{app_id}: {q}"
        ans = await run_in_threadpool(answer, scoped, lang)
        from ..brain.web import UI

        return bt.TemplateResponse(
            request,
            "_answer.html",
            {"request": request, "answer": ans, "t": UI[lang], "lang": lang},
        )
