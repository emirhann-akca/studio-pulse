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

from . import studio_metrics as sm
from .ui import request_lang

STATIC_FORGE_URL = "https://github.com/emirhann-akca/creative-forge"

STATUS_ORDER = {"live": 0, "testing": 1, "new": 2, "ga4": 3, "waiting": 4}

T: dict[str, dict[str, str]] = {
    "tr": {
        "apps": "Uygulamalar",
        "new_section": "Yeni gelenler",
        "tools": "Araçlar",
        "radar": "Pazar radarı",
        "m_title": "Pazar radarı",
        "m_sub": "Mağaza listeleri ve sayfalarından günlük gözlem; her sayı GÖZLEM / MAĞAZA VERİSİ / TAHMİN etiketli.",
        "m_none": "Henüz radar koşusu yok: `pulse radar run` ya da radar.enabled ile `pulse run`.",
        "m_summary": "Günün özeti",
        "m_rising": "Bugün yükselenler",
        "m_late": "Geç yükselenler ve izlenenler",
        "m_trends": "Mekanikler (bugünkü listelerde)",
        "m_ideas": "Bize ne öğretir / ne yapabiliriz",
        "m_ideas_note": "Çıkarım, veri değil: bir reklam testi için yön.",
        "m_game": "Oyun",
        "m_mech": "Mekanik",
        "m_rank": "En iyi sıra",
        "m_age": "Yaş (gün)",
        "m_play": "Play kurulum",
        "m_llm": "LLM, alıntıyla doğrulandı",
        "m_kw": "anahtar kelime tahmini",
        "m_test": "Creative Forge'da bu mekanikle reklam testi",
        "m_games": "oyun",
        "m_weekly": "haftalık eğilim için en az 5 haftalık geçmiş gerekiyor",
        "ads": "Reklamlar (Creative Forge)",
        "health": "Veri sağlığı",
        "alerts": "Uyarılar",
        "dashboard": "Ayrıntılı pano",
        "live": "canlı",
        "testing": "test",
        "new": "yeni",
        "ga4": "GA4",
        "waiting": "bekliyor",
        "waiting_long": "SDK bekleniyor: hiçbir derleme henüz veri göndermiyor",
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
        "store30": "Mağaza · son 30 gün",
        "x_back": "Geri: {app}",
        "x_period": "Bu dönem",
        "x_prev": "Önceki dönem",
        "x_change": "Değişim",
        "x_n": "Örneklem (n)",
        "x_floor": "{n} altında sayı gösterilmez",
        "x_daily": "Günlük değerler",
        "x_date": "Tarih",
        "x_value": "Değer",
        "x_sql": "Bu sayının sorgusu",
        "x_sql_note": "Kutu ve bu sayfa aynı sorguyu çalıştırır; parametreler: uygulama, başlangıç, bitiş.",
        "x_none": "Bu dönemde bu metrik için satır yok.",
        "period": "Dönem",
        "days_n": "{n} gün",
        "period_line": "{start} – {end} · karşılaştırma: {pstart} – {pend}",
        "too_few": "örneklem yetersiz",
        "need_n": "en az {n} gerekli",
        "no_rows": "bu dönemde veri yok",
        "vs_prev_short": "öncekine göre",
        "prev_short": "önceki dönem",
        "dau_period": "Günlük aktif kullanıcı · son {days} gün",
        "prev_period": "önceki dönem",
        "empty_title": "Bu uygulamanın verisi yok",
        "empty_waiting": "SDK mağaza derlemesinde kapalı; hiçbir derleme henüz veri göndermiyor. Mağaza verisi bağlanınca indirme, puan ve çökme oranı yine görünür.",
        "empty_cta": "Veri kaynaklarına bak",
        "store_col": "Mağaza",
        "installs30": "Kurulum",
        "uninstalls30": "Kaldırma",
        "data_through": "Son gün",
        "store_missing": "Mağaza verisi bağlı değil. Play Console ve App Store Connect raporları bağlanınca kurulum, kaldırma ve puan burada görünür; az kullanıcıda bile anlamlıdır.",
        "store_cta": "Veri kaynakları",
        "builds": "SDK derlemeleri",
        "no_levels": "Bölüm verisi yok: oyun level_start / level_complete / level_fail gönderince görünür.",
        "no_recs": "Şimdilik öneri yok.",
        "no_apps_title": "Henüz uygulama yok",
        "no_apps": "pulse.yaml dosyasına bir uygulama ekleyin ya da SDK'lı bir oyunu toplayıcıya bağlayın; ilk olayla burada görünür.",
        "store_exports": "GA4 / mağaza dışa aktarımları",
        "static_banner": "Sentetik demo verisi.",
        "static_note": "Bu, `pulse demo`nun uydurma oyunları için Stüdyo'nun durağan bir kopyası; hiçbir gerçek ürünü, stüdyoyu ya da oyuncuyu anlatmaz. Uygulamada soldaki liste 10 saniyede bir yenilenir ve bu panel sorulara cevap verir.",
        "static_ask": "Soru kutusu çalışan uygulamada açılır (`pulse app`).",
        "static_dash": "Pano",
        "static_ua": "Kullanıcı edinme",
        "static_exp": "Deneyler",
    },
    "en": {
        "apps": "Apps",
        "new_section": "New arrivals",
        "tools": "Tools",
        "radar": "Market radar",
        "m_title": "Market radar",
        "m_sub": "Daily observation of store charts and pages; every number labelled OBSERVED / STORE-REPORTED / ESTIMATE.",
        "m_none": "No radar run yet: `pulse radar run`, or `pulse run` with radar.enabled.",
        "m_summary": "Today in short",
        "m_rising": "Rising today",
        "m_late": "Late risers and watch list",
        "m_trends": "Mechanics (on today's charts)",
        "m_ideas": "What it teaches us / what we can do",
        "m_ideas_note": "Inference, not data: a direction for an ad test.",
        "m_game": "Game",
        "m_mech": "Mechanic",
        "m_rank": "Best rank",
        "m_age": "Age (days)",
        "m_play": "Play installs",
        "m_llm": "LLM, verified quote",
        "m_kw": "keyword guess",
        "m_test": "An ad test with this mechanic in Creative Forge",
        "m_games": "games",
        "m_weekly": "a weekly trend needs at least 5 weeks of history",
        "ads": "Ads (Creative Forge)",
        "health": "Data health",
        "alerts": "Alerts",
        "dashboard": "Full dashboard",
        "live": "live",
        "testing": "testing",
        "new": "new",
        "ga4": "GA4",
        "waiting": "waiting",
        "waiting_long": "awaiting SDK: no build sends data yet",
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
        "store30": "Store · last 30 days",
        "x_back": "Back: {app}",
        "x_period": "This period",
        "x_prev": "Previous period",
        "x_change": "Change",
        "x_n": "Sample (n)",
        "x_floor": "no number under {n}",
        "x_daily": "Daily values",
        "x_date": "Date",
        "x_value": "Value",
        "x_sql": "The query behind this number",
        "x_sql_note": "The tile and this page run the same query; parameters: app, start, end.",
        "x_none": "No rows for this metric in this period.",
        "period": "Period",
        "days_n": "{n} days",
        "period_line": "{start} – {end} · compared with {pstart} – {pend}",
        "too_few": "not enough data",
        "need_n": "needs {n}+",
        "no_rows": "no data in this period",
        "vs_prev_short": "vs previous",
        "prev_short": "previous period",
        "dau_period": "Daily active users · last {days} days",
        "prev_period": "previous period",
        "empty_title": "No data for this app yet",
        "empty_waiting": "The SDK is off in store builds; no build sends data yet. Once store data is connected, installs, rating and crash rate show up anyway.",
        "empty_cta": "Check data sources",
        "store_col": "Store",
        "installs30": "Installs",
        "uninstalls30": "Uninstalls",
        "data_through": "Last day",
        "store_missing": "Store data is not connected. Once the Play Console and App Store Connect reports are connected, installs, uninstalls and rating show here; they mean something even with few users.",
        "store_cta": "Data sources",
        "builds": "SDK builds",
        "no_levels": "No level data: it appears once the game sends level_start / level_complete / level_fail.",
        "no_recs": "No recommendations yet.",
        "no_apps_title": "No apps yet",
        "no_apps": "Add an app to pulse.yaml or point an SDK game at the collector; it shows up here with its first event.",
        "store_exports": "GA4 / store exports",
        "static_banner": "Synthetic demo data.",
        "static_note": "A static copy of the Studio for the fictional games of `pulse demo`; it does not describe any real product, studio or player. In the app the list on the left refreshes every 10 seconds and this panel answers questions.",
        "static_ask": "The question box opens in the running app (`pulse app`).",
        "static_dash": "Dashboard",
        "static_ua": "User acquisition",
        "static_exp": "Experiments",
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
        # a configured app with only an SDK source and no SDK data yet is waiting, not GA4
        kinds = {a.id: {k.split("@", 1)[0] for k in a.sources} for a in cfg.apps}
        for m in meta:
            k = kinds.get(m["app_id"], set())
            out[m["app_id"]] = {
                "id": m["app_id"],
                "name": m["name"] or m["app_id"],
                "genre": m["genre"],
                "status": "waiting" if k and k <= {"pulse_sdk"} else "ga4",
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

    def app_view(
        con: Any, app_id: str, lv: dict[str, Any], lang: str, days: int = 7
    ) -> dict[str, Any]:
        t = T[lang]
        s = rows(con, "SELECT * FROM fct_app_summary WHERE app_id = ?", [app_id])
        s = s[0] if s else None
        end = sm.last_date(rows, con, app_id)
        kpis: list[dict[str, Any]] = []
        chart = ""
        if end is not None:
            for key in sm.METRICS:
                k = sm.compute(rows, con, app_id, key, end, days, lang)
                if key in ("installs", "rating") and not k["has_data"]:
                    continue  # store data not connected: the store card says so
                kpis.append(k)
            dau = next(k for k in kpis if k["key"] == "dau")
            chart = sm.area_chart(
                [(r["date"], float(r["value"])) for r in dau["rows"]],
                [float(r["value"]) for r in dau["prev_rows"]],
                t["dau_period"].format(days=days),
                "num",
                lang,
            )
        store_daily = rows(
            con,
            "SELECT store, SUM(installs) AS installs, SUM(uninstalls) AS uninstalls, "
            "MAX(date) AS last_date FROM stg_store_daily WHERE app_id = ? "
            "AND date > CURRENT_DATE - INTERVAL 30 DAY GROUP BY store ORDER BY store",
            [app_id],
        )
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
        now = None
        snap = lv.get("snap")
        if snap:
            mine = [a for a in snap["apps"] if config_id(a["app_id"]) == app_id]
            now = sum(int(a.get("active") or 0) for a in mine) if mine else None  # None: no SDK
        return {
            "summary": s,
            "kpis": kpis,
            "days": days,
            "end": end,
            "chart": chart,
            "store": reg,
            "store_daily": store_daily,
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

    def static() -> bool:
        return bool(getattr(app.state, "studio_static", False))

    def nav(lang: str, page: str) -> dict[str, Any]:
        """Where the Studio's links go: the running app's routes, or, for the static copy
        (docs/demo/studio: English at the top, Turkish under tr/), relative file names."""
        other = "en" if lang == "tr" else "tr"
        if not static():
            return {  # the language travels in a cookie (ui.request_lang)
                "app": lambda a: f"/studio/{a}",
                "market": "/studio/market",
                "other_lang": f"?lang={other}",
                "dash": "/dashboard/",
                "static": False,
            }
        here = f"{page}.html"
        return {
            "app": lambda a: f"{a}.html",
            "market": "market.html",
            "other_lang": f"tr/{here}" if lang == "en" else f"../{here}",
            "dash": "../" if lang == "en" else "../../",
            "static": True,
        }

    def creative_forge_url() -> str | None:
        """The running Creative Forge app's URL, from its own app.json, if it answers."""
        if static():  # the static copy never probes this machine
            return STATIC_FORGE_URL
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

    def page_data(
        app_id: str | None, lang: str, full: bool = True, days: int = 7
    ) -> dict[str, Any]:
        """full=False (the sidebar poll, the market view): the app list only, no app page
        and no insights, so a 10-second poll never recomputes or re-narrates anything."""
        lv = live()
        con, source = open_db(cfg)
        try:
            apps = app_list(con, lv)
            chosen = app_id or (apps[0]["id"] if apps else None)
            view = app_view(con, chosen, lv, lang, days) if chosen and full else None
            side = sidebar_counts(con)
        finally:
            if con is not None:
                con.close()
        current = next((a for a in apps if a["id"] == chosen), None)
        cards: list[dict[str, Any]] = []
        load = getattr(app.state, "brain_insights", None)
        if load and chosen and full:
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
            "collector_error": None if static() else lv.get("error"),
            "forge_url": creative_forge_url(),
            "static": static(),
            "nav": nav(lang, chosen or "index"),
            "periods": sm.PERIODS,
            **side,
        }

    def sidebar_counts(con: Any) -> dict[str, int]:
        """The numbers next to Alerts and Data health in the sidebar."""
        if con is None or static():
            return {"side_alerts": 0, "side_failing": 0}
        from .. import checks

        summary = checks.summarize(checks.latest_results(con))
        last = rows(
            con,
            "SELECT COUNT(*) AS n FROM meta_alert_events WHERE run_id = (SELECT run_id FROM "
            "meta_alert_events ORDER BY fired_at DESC LIMIT 1)",
        )
        return {
            "side_alerts": int(last[0]["n"]) if last else 0,
            "side_failing": summary["errors"] + summary["warnings"],
        }

    def sidebar(lang: str, chosen: str) -> dict[str, Any]:
        """The sidebar of every server page (base.html), from the same data as the Studio."""
        d = page_data(None, lang, full=False)
        keys = ("apps", "arrived", "forge_url", "static", "side_alerts", "side_failing")
        return {**{k: d[k] for k in keys}, "nav": nav(lang, chosen), "chosen": chosen}

    app.state.studio_side = sidebar

    def market_data(lang: str) -> dict[str, Any]:
        """The newest radar facts, shaped for the Studio's market view (local only)."""
        from ..radar.digest import CLASS, COUNTRY, LABELS, LIST
        from ..radar.page import latest_facts

        try:
            from ..radar.settings import from_config as radar_settings

            facts = latest_facts(radar_settings(cfg).out_dir)
        except Exception:  # no radar block or no run yet
            facts = None
        if not facts:
            return {"facts": None}

        def mech(item: dict[str, Any]) -> tuple[str | None, str | None, str]:
            prim = next((c for c in (item.get("profile") or {}).get("claims", [])
                         if c.get("dimension") == "mechanic_primary"), None)  # fmt: skip
            src = (item.get("profile") or {}).get("source")
            return (
                (prim or {}).get("value"),
                (prim or {}).get("quote"),
                "llm" if src == "llm" else "kw",
            )

        def row(item: dict[str, Any]) -> dict[str, Any]:
            m, quote, src = mech(item)
            best = min(item.get("ranks") or [], key=lambda r: r["rank"], default=None)
            return {
                "name": item["name"],
                "developer": item.get("developer"),
                "class": CLASS[lang].get(item["class"], item["class"]),
                "mechanic": m,
                "quote": quote,
                "src": src,
                "best": f"{COUNTRY[lang].get(best['country'], best['country'])} "
                f"{LIST[lang].get(best['list'], best['list'])} #{best['rank']}"
                if best
                else "—",
                "age": (item.get("age") or {}).get("value"),
                "play": (item.get("play") or {}).get("bucket"),
                "store_url": item.get("store_url"),
                "play_url": item.get("play_url"),
            }

        items = facts.get("items") or []
        narr = (facts.get("meta") or {}).get("narration") or {}
        mechs: dict[str, int] = {}
        for r in (facts.get("trends") or {}).get("rows", []):
            mechs[r["mechanic"]] = mechs.get(r["mechanic"], 0) + int(r.get("n_apps") or 0)
        history = ((facts.get("trends") or {}).get("history_days") or {}).get("value") or 0
        return {
            "facts": facts,
            "as_of": facts.get("as_of"),
            "countries": ", ".join(COUNTRY[lang].get(c, c) for c in facts.get("countries") or []),
            "rising": [row(i) for i in items if i["class"] == "breakout"][:10],
            "others": [row(i) for i in items if i["class"] != "breakout"][:10],
            "narration": narr.get(lang) if narr.get("status") == "ok" else None,
            "mechanics": sorted(mechs.items(), key=lambda kv: -kv[1])[:8],
            "weekly_ready": history >= 35,
            "trend_rows": [
                r for r in (facts.get("trends") or {}).get("rows", []) if r.get("is_trend")
            ],
            "ideas": [i for i in facts.get("ideas") or [] if i.get("kind") != "top_mechanic"][:6],
            "observed": LABELS[lang]["observed"],
            "store_reported": LABELS[lang]["store_reported"],
        }

    def render(page: str, lang: str) -> str | None:
        """One Studio page as HTML without a request (the static export)."""
        if page == "market":
            data = page_data(None, lang, full=False)
            data.update(chosen="@market", current=None, view=None, cards=[], recs=[],
                        mode="market", market=market_data(lang), nav=nav(lang, "market"))  # fmt: skip
        else:
            data = page_data(page, lang)
            if data["current"] is None:
                return None
        return templates.get_template("studio.html").render(context(None, "studio", **data))

    app.state.studio_render = render
    app.state.studio_apps = lambda: page_data(None, "en", full=False)

    @app.get("/studio/market", response_class=HTMLResponse)
    def studio_market(request: Request, lang: str | None = None):
        lg = request_lang(request, lang)
        data = page_data(None, lg, full=False)
        data.update(chosen="@market", current=None, view=None, cards=[], recs=[],
                    mode="market", market=market_data(lg), nav=nav(lg, "market"))  # fmt: skip
        return templates.TemplateResponse(
            request, "studio.html", context(request, "studio", **data)
        )

    @app.get("/studio", response_class=HTMLResponse)
    def studio_home(request: Request, lang: str | None = None):
        lg = request_lang(request, lang)
        data = page_data(None, lg, full=False)
        if data["chosen"]:
            q = f"?lang={lang}" if lang in ("tr", "en") else ""
            return RedirectResponse(f"/studio/{data['chosen']}{q}", status_code=307)
        return templates.TemplateResponse(
            request, "studio.html", context(request, "studio", **data)
        )

    @app.get("/studio/fragments/sidebar", response_class=HTMLResponse)
    def studio_sidebar(
        request: Request, chosen: str | None = Query(None, alias="app"), lang: str | None = None
    ):
        data = page_data(chosen, request_lang(request, lang), full=False)
        return templates.TemplateResponse(
            request, "_studio_sidebar.html", context(request, "fragment", **data)
        )

    @app.get("/studio/{app_id}", response_class=HTMLResponse)
    def studio_app(request: Request, app_id: str, lang: str | None = None, days: int = 7):
        days = days if days in sm.PERIODS else 7
        data = page_data(app_id, request_lang(request, lang), days=days)
        if data["current"] is None:
            return HTMLResponse(f"unknown app {app_id!r}", 404)
        return templates.TemplateResponse(
            request, "studio.html", context(request, "studio", **data)
        )

    @app.get("/studio/{app_id}/metric/{key}", response_class=HTMLResponse)
    def studio_metric(
        request: Request, app_id: str, key: str, lang: str | None = None, days: int = 7
    ):
        """One KPI tile opened: its daily rows, the previous period, the SQL behind it."""
        if key not in sm.METRICS:
            return HTMLResponse(f"unknown metric {key!r}", 404)
        days = days if days in sm.PERIODS else 7
        lg = request_lang(request, lang)
        data = page_data(app_id, lg, full=False)
        if data["current"] is None:
            return HTMLResponse(f"unknown app {app_id!r}", 404)
        con, _ = open_db(cfg)
        try:
            end = sm.last_date(rows, con, app_id) if con is not None else None
            m = sm.compute(rows, con, app_id, key, end, days, lg) if end else None
        finally:
            if con is not None:
                con.close()
        chart = ""
        if m and len(m["rows"]) >= 2:
            chart = sm.area_chart(
                [(r["date"], float(r["value"])) for r in m["rows"]],
                [float(r["value"]) for r in m["prev_rows"]],
                m["label"],
                m["fmt"],
                lg,
            )
        data.update(metric=m, metric_key=key, chart=chart, days=days, mode="metric",
                    fmt_value=lambda v: sm.fmt(None if v is None else float(v), m["fmt"], lg) if m else "—")  # fmt: skip
        return templates.TemplateResponse(
            request, "studio_metric.html", context(request, "studio", **data)
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
        lang = request_lang(request, (form.get("lang") or [None])[0])
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


def export_static(
    cfg: Any,
    out: Path,
    *,
    live: dict[str, Any] | None = None,
) -> list[Path]:
    """Write the Studio as static pages (the public demo): one page per app plus the
    market view, English in out/ and Turkish in out/tr/. `live` is a collector
    snapshot taken when the data was generated; without one the "playing now" counts
    are left out. No question box, no polling, no probe of this machine."""
    import copy

    from .app import create_app

    def fetch() -> dict[str, Any]:
        if live is None:
            raise OSError("no live snapshot")
        return copy.deepcopy(live)

    app = create_app(cfg, token="static", live_fetcher=fetch)
    app.state.studio_static = True
    listed = app.state.studio_apps()
    ids = [a["id"] for a in listed["apps"] + listed["arrived"]]
    written: list[Path] = []
    for lang in ("en", "tr"):  # the layout nav() links to
        d = out if lang == "en" else out / lang
        d.mkdir(parents=True, exist_ok=True)
        for page in [*ids, "market"]:
            html = app.state.studio_render(page, lang)
            if html is None:
                continue
            (d / f"{page}.html").write_text(html, encoding="utf-8")
            written.append(d / f"{page}.html")
        if ids:  # the folder opens on the first configured app (the config's order)
            start = next((a.id for a in cfg.apps if a.id in ids), ids[0])
            first = app.state.studio_render(start, lang) or ""
            (d / "index.html").write_text(first, encoding="utf-8")
            written.append(d / "index.html")
    return written
