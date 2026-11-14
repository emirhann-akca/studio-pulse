"""`pulse serve` pages for the SDK: Canlı / Live, and per-app levels + death heatmap.

    /live                 who is playing right now (polls the collector's /live every 5 s)
    /fragments/live       the part htmx swaps in
    /live/apps/{app_id}   level funnel, death heatmap and settings from the warehouse models

The UI process asks the collector (collector.url, default 127.0.0.1:8787) on
the server side, so the read key never reaches the browser. Everything is
rendered by Jinja; the vendored htmx polls; no JavaScript build, no CDN.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from html import escape
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..dashboard import charts
from .registry import slug
from .settings import DEFAULT_PORT, from_config

NAV = [("/live", "Canlı / Live", "live")]


def default_fetcher(cfg: Any) -> Callable[[], dict[str, Any]]:
    settings = from_config(cfg)
    url = (settings.url or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")

    def fetch() -> dict[str, Any]:
        import requests

        headers = {"X-Pulse-Read-Key": settings.read_key} if settings.read_key else {}
        r = requests.get(url + "/live", headers=headers, timeout=3)
        r.raise_for_status()
        out = r.json()
        out["collector_url"] = url
        return out

    fetch.url = url  # type: ignore[attr-defined]
    return fetch


def sparkline(values: list[int], label: str) -> str:
    """Events per minute as a small inline SVG (oldest left, now right).

    The template renders the result with `|safe`, and `label` is the app name an
    SDK sent with a public write key: everything that is not a number is escaped.
    """
    n = len(values)
    top = max(values) if values and max(values) > 0 else 1
    w, h = 120.0, 28.0
    pts = " ".join(
        f"{(i * w / max(n - 1, 1)):.1f},{(h - 2 - v / top * (h - 4)):.1f}"
        for i, v in enumerate(values)
    )
    last = values[-1] if values else 0
    aria = escape(f"{label}: events per minute over the last {n} minutes, last {last}", quote=True)
    return (
        f'<svg class="spark" viewBox="0 0 {w:.0f} {h:.0f}" width="120" height="28" role="img" '
        f'aria-label="{aria}">'
        f'<polyline fill="none" stroke="var(--s1)" stroke-width="1.6" points="{pts}"/></svg>'
    )


def _ago(ms: int | None, now_ms: int) -> str:
    if not ms:
        return "-"
    s = max(0, (now_ms - ms) / 1000)
    if s < 90:
        return f"{int(s)} s ago"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.0f} days ago"


def register(
    app: FastAPI,
    templates: Any,
    cfg: Any,
    *,
    context: Callable[..., dict[str, Any]],
    open_db: Callable[[Any], tuple[Any, str]],
    rows: Callable[..., list[dict[str, Any]]],
    fetch_live: Callable[[], dict[str, Any]] | None = None,
) -> list[tuple[str, str, str]]:
    fetch = fetch_live or default_fetcher(cfg)
    bundle_to_app: dict[str, str] = {}
    for a in cfg.apps:
        for key, src in a.sources.items():
            if key.split("@", 1)[0] == "pulse_sdk":
                for b in (src or {}).get("bundle_ids") or []:
                    bundle_to_app[b] = a.id

    def live_data() -> dict[str, Any]:
        try:
            snap = fetch()
            error = None
        except Exception as exc:  # collector down or unreachable: say so on the page
            snap, error = None, f"{type(exc).__name__}"
        url = getattr(fetch, "url", None) or (snap or {}).get("collector_url", "")
        if snap is None:
            return {"snap": None, "error": error, "collector_url": url}
        now = snap["now_ms"]
        for a in snap["apps"]:
            a["spark"] = sparkline(a["events_per_min"], a["name"])
            a["config_id"] = bundle_to_app.get(a["app_id"]) or slug(a["app_id"])
            a["seen"] = _ago(a.get("last_seen_ms"), now)
        grouped: dict[str, dict[str, Any]] = {}  # one banner per app, its platforms listed
        for n in snap["new_apps"]:
            g = grouped.setdefault(n["app_id"], {**n, "platforms": []})
            g["platforms"].append(n["platform"])
            g["first_seen_ms"] = min(g["first_seen_ms"], n["first_seen_ms"])
        snap["new_apps"] = sorted(grouped.values(), key=lambda n: -n["first_seen_ms"])
        for n in snap["new_apps"]:
            n["platform"] = ", ".join(sorted(n["platforms"]))
            n["ago"] = _ago(n["first_seen_ms"], now)
        snap["events_last_min"] = sum(a["events_last_min"] for a in snap["apps"])
        snap["at"] = dt.datetime.fromtimestamp(now / 1000, dt.UTC).strftime("%H:%M:%S UTC")
        return {"snap": snap, "error": None, "collector_url": url}

    app.state.live_data = live_data  # the Studio sidebar reads the same snapshot
    app.state.bundle_to_app = bundle_to_app

    @app.get("/live", response_class=HTMLResponse)
    def live_page(request: Request):
        return templates.TemplateResponse(
            request, "live.html", context(request, "live", source="collector", **live_data())
        )

    @app.get("/fragments/live", response_class=HTMLResponse)
    def live_fragment(request: Request):
        return templates.TemplateResponse(
            request, "_live.html", context(request, "fragment", **live_data())
        )

    @app.get("/live/apps/{app_id}", response_class=HTMLResponse)
    def sdk_app(request: Request, app_id: str, level: int | None = None):
        from ..serve.ui import request_lang

        tr = request_lang(request) == "tr"
        con, source = open_db(cfg)
        try:
            funnel = rows(
                con, "SELECT * FROM fct_sdk_level_funnel WHERE app_id = ? ORDER BY level", [app_id]
            )
            heat = rows(
                con,
                "SELECT * FROM fct_sdk_fail_heatmap WHERE app_id = ? ORDER BY level, bin_y, bin_x",
                [app_id],
            )
            settings = rows(
                con, "SELECT * FROM fct_sdk_settings WHERE app_id = ? ORDER BY setting", [app_id]
            )
            dq = rows(con, "SELECT * FROM fct_sdk_data_quality WHERE app_id = ?", [app_id])
            reg = rows(
                con, "SELECT * FROM fct_sdk_apps WHERE app_id = ? ORDER BY platform", [app_id]
            )
            meta = rows(con, "SELECT * FROM meta_apps WHERE app_id = ?", [app_id])
        finally:
            if con is not None:
                con.close()
        name = (meta[0]["name"] if meta else None) or (reg[0]["display_name"] if reg else app_id)
        by_level: dict[int, int] = {}
        for h in heat:
            by_level[h["level"]] = by_level.get(h["level"], 0) + h["fails"]
        hot = sorted(by_level, key=lambda lv: (-by_level[lv], lv))[:12]
        chosen = level if level in by_level else (hot[0] if hot else None)
        cells = [h for h in heat if h["level"] == chosen]
        grid = [[None] * 10 for _ in range(10)]
        for h in cells:
            grid[9 - h["bin_y"]][h["bin_x"]] = (grid[9 - h["bin_y"]][h["bin_x"]] or 0) + h["fails"]
        space = cells[0]["pos_space"] if cells else "norm"
        if space == "world" and cells:
            x0, x1 = cells[0]["x_min"], cells[0]["x_max"]
            cols = [f"{x0 + (x1 - x0) * i / 10:.0f}" for i in range(10)]
        else:
            cols = [f"{i / 10:.1f}" for i in range(10)]
        heat_html = (
            charts.heatmap(
                f"{chosen}. bölümde ölümler" if tr else f"Deaths on level {chosen}",
                [f"{(9 - r) / 10:.1f}" for r in range(10)],
                cols,
                [[v for v in row] for row in grid],
                fmt=lambda v: "" if v is None else f"{v:.0f}",
                subtitle=(
                    f"level_fail konumları, 10 x 10 ızgara ({space} koordinat); satırlar y, sütunlar x"
                    if tr
                    else f"level_fail positions on a 10 x 10 grid ({space} coordinates); rows are y, columns x"
                ),
                corner="y \\ x",
            )
            if chosen is not None
            else charts.empty_chart(
                "Ölümler" if tr else "Deaths",
                subtitle="Henüz level_fail konumu yok" if tr else "No level_fail positions yet",
            )
        )
        fail_chart = charts.bar_chart(
            "Bölümlere göre başarısızlık oranı" if tr else "Fail rate by level",
            [str(r["level"]) for r in funnel],
            [
                charts.Series(
                    "başarısızlık" if tr else "fail rate", [r["fail_rate"] for r in funnel]
                )
            ],
            fmt=charts.fmt_pct,
            subtitle=(
                "başarısız / (biten + başarısız); sıçramalar zor bölümler"
                if tr
                else "fails / (completes + fails); spikes are the hard levels"
            ),
            x_name="Bölüm" if tr else "Level",
            max_labels=20,
        )
        table = charts.data_table(
            [
                "Bölüm",
                "Başlama",
                "Bitirme",
                "Başarısız",
                "Çıkış",
                "Başarısızlık",
                "Kazanmak için deneme",
                "Çıkarılan çıkış",
                "Ulaşan oyuncu",
                "Burada bırakan",
                "Bırakma oranı",
            ]
            if tr
            else [
                "Level",
                "Starts",
                "Completes",
                "Fails",
                "Quits",
                "Fail rate",
                "Attempts to win",
                "Inferred quits",
                "Players reached",
                "Churned here",
                "Churn rate",
            ],  # fmt: skip
            [
                [
                    str(r["level"]),
                    charts.fmt_int(r["starts"]),
                    charts.fmt_int(r["completes"]),
                    charts.fmt_int(r["fails"]),
                    charts.fmt_int(r["quits"]),
                    charts.fmt_pct(r["fail_rate"]),
                    charts.fmt_num(r["avg_attempts_to_win"]),
                    charts.fmt_int(r["quit_inferred"]),
                    charts.fmt_int(r["players_reached"]),
                    charts.fmt_int(r["churned_here"]),
                    charts.fmt_pct(r["churn_rate"]),
                ]  # fmt: skip
                for r in funnel
            ],
        )
        return templates.TemplateResponse(
            request,
            "sdk_app.html",
            context(
                request,
                "live",
                source=source,
                app_id=app_id,
                app_name=name,
                funnel=funnel,
                fail_chart=fail_chart,
                funnel_table=table,
                heat_html=heat_html,
                hot_levels=hot,
                level=chosen,
                settings=settings,
                dq=dq[0] if dq else None,
                registry=reg,
                fmt_pct=charts.fmt_pct,
            ),
        )

    return NAV
