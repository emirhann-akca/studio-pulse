"""Local web UI for one config: dashboard, refresh health, data quality, alerts.

Concurrency with the scheduled writer: DuckDB allows one writing process, and a
process holding the file open, even read-only, blocks that writer. The UI
therefore never keeps the warehouse open: each request opens it read-only,
reads, and closes it (the writer waits briefly for such readers, see
warehouse.connect). While a refresh is running, or whenever the live file is
locked, pages are served from the snapshot of the last good run.

Safety: the server binds 127.0.0.1 by default, rejects requests whose Host
header is not this machine (DNS rebinding), and "run now" needs a per-process
token that only pages served by this process carry (cross-site requests cannot
set the header).
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import subprocess
import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, checks, runstate, schedule, warehouse
from ..config import Config
from ..dashboard.assets import CHART_CSS, CSS, JS
from . import ui

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _hostname(host_header: str) -> str:
    """Host header -> bare host name (no port, no IPv6 brackets), lower case."""
    return (urlsplit("//" + host_header.strip()).hostname or "").lower()


def _pkg_dir(name: str) -> Path:
    return Path(str(resources.files("studio_pulse.serve").joinpath(name)))


def open_db(cfg: Config) -> tuple[duckdb.DuckDBPyConnection | None, str]:
    """(connection, source); source is live, snapshot or none. Caller closes."""
    hb = runstate.read_heartbeat(cfg.state_dir) or {}
    if hb.get("status") != "running" and cfg.warehouse.exists():
        try:
            return warehouse.connect(cfg.warehouse, read_only=True), "live"
        except warehouse.WarehouseLocked:
            pass
        except duckdb.Error:
            pass
    snap = runstate.snapshot_path(cfg.state_dir)
    if snap.exists():
        try:
            return warehouse.connect(snap, read_only=True), "snapshot"
        except (warehouse.WarehouseLocked, duckdb.Error):
            pass
    return None, "none"


def _rows(
    con: duckdb.DuckDBPyConnection | None, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    if con is None:
        return []
    try:
        cur = con.execute(sql, params or [])
    except duckdb.Error:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def default_launcher(cfg: Config) -> int:
    """Start `pulse-bg run` detached from this server; returns the pid."""
    log = Path(cfg.state_dir) / "logs" / "pulse.log"
    argv = [
        *schedule.background_program(),
        "--log-file",
        str(log),
        "run",
        "--config",
        str(cfg.path),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(cfg.base_dir),
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs).pid


def _age(iso: str | None) -> str:
    t = runstate.parse_iso(iso)
    if t is None:
        return "never"
    s = (dt.datetime.now(dt.UTC) - t).total_seconds()
    if s < 90:
        return f"{int(s)} s ago"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.1f} days ago"


def create_app(
    cfg: Config,
    *,
    token: str | None = None,
    launcher: Callable[[Config], int] | None = None,
    allowed_hosts: set[str] | None = None,
    schedule_name: str = schedule.DEFAULT_NAME,
    live_fetcher: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    launch = launcher or default_launcher
    hosts = LOCAL_HOSTS | {h.lower().strip("[]") for h in (allowed_hosts or set())}
    app = FastAPI(title="studio-pulse", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(_pkg_dir("templates")))
    templates.env.globals.update(
        version=__version__, css=CSS, js=JS, token=token, age=_age,
        ui_css=ui.UI_CSS, ui_js=ui.UI_JS, icon=ui.icon, chart_css=CHART_CSS,
    )  # fmt: skip
    app.mount("/static", StaticFiles(directory=str(_pkg_dir("static"))), name="static")
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(cfg.site_dir), html=True, check_dir=False),
        name="dashboard",
    )

    @app.middleware("http")
    async def remember_lang(request: Request, call_next):
        resp = await call_next(request)
        q = request.query_params.get("lang")
        if q in ui.LANGS and request.cookies.get(ui.COOKIE) != q:
            resp.set_cookie(ui.COOKIE, q, max_age=365 * 86400, samesite="lax", httponly=True)
        return resp

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if _hostname(request.headers.get("host", "")) not in hosts:
            return PlainTextResponse("studio-pulse only answers requests for this machine", 400)
        return await call_next(request)

    def state() -> dict[str, Any]:
        hb = runstate.read_heartbeat(cfg.state_dir)
        lock = runstate.read_json(Path(cfg.state_dir) / runstate.LOCK_NAME)
        return {"hb": hb, "lock": lock, "running": bool(hb and hb.get("status") == "running")}

    from .. import pipeline
    from .alert_text import AlertText

    alert_text = AlertText(pipeline.default_file(cfg, None, "alerts_path", "alerts.yaml"))

    def context(request: Request | None, page: str, **extra: Any) -> dict[str, Any]:
        """Everything a page in the shell needs: its data, the language, the sidebar."""
        lang = ui.request_lang(request, extra.pop("lang", None))
        other = "en" if lang == "tr" else "tr"
        base: dict[str, Any] = {
            "request": request,
            "page": page,
            "title": cfg.title,
            "config_path": str(cfg.path),
            "dashboard_ready": (cfg.site_dir / "index.html").exists(),
            "lang": lang,
            "s": ui.S[lang],
            "age": lambda iso: ui.age(iso, lang),
            "alert_text": lambda a: alert_text(a, lang),
            "static": False,
            "chosen": "@" + page,
            "source": None,
            "nav": {
                "app": lambda a: f"/studio/{a}",
                "market": "/studio/market",
                "other_lang": f"?lang={other}",
                "dash": "/dashboard/",
            },
            **state(),
        }
        side = getattr(app.state, "studio_side", None)
        if side is not None and "apps" not in extra and page != "fragment":
            base.update(side(lang, "@" + page))
        out = {**base, **extra}
        out["commands"] = ui.commands(
            out["s"], [*out.get("apps", []), *out.get("arrived", [])], out["nav"]
        )
        return out

    def load(con: duckdb.DuckDBPyConnection | None) -> dict[str, Any]:
        results = checks.latest_results(con) if con is not None else []
        last_run = _rows(
            con, "SELECT MAX(fired_at) AS t, ANY_VALUE(run_id) AS r FROM meta_alert_events"
        )
        latest_alerts = _rows(
            con,
            "SELECT * FROM meta_alert_events WHERE run_id = (SELECT run_id FROM meta_alert_events "
            "ORDER BY fired_at DESC LIMIT 1) ORDER BY severity, app_id, rule",
        )
        freshness = _rows(con, "SELECT * FROM fct_source_freshness ORDER BY app_id, source")
        as_of = cfg.as_of or dt.date.today()
        for r in freshness:
            through = r.get("data_through")
            r["lag"] = (as_of - through).days if through else None
        return {
            "checks": results,
            "check_summary": checks.summarize(results),
            "alerts": latest_alerts,
            "last_alert_run": last_run[0] if last_run else None,
            "freshness": freshness,
            "as_of": as_of,
        }

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/studio", status_code=307)

    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request):
        con, source = open_db(cfg)
        try:
            data = load(con)
        finally:
            if con is not None:
                con.close()
        try:
            sched = schedule.backend().status(schedule_name)
        except schedule.ScheduleError as exc:
            sched = {"registered": False, "note": str(exc)}
        return templates.TemplateResponse(
            request,
            "health.html",
            context(
                request,
                "health",
                source=source,
                history=runstate.read_history(cfg.state_dir, 20),
                schedule=sched,
                schedule_name=schedule_name,
                **data,
            ),
        )

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request):
        con, source = open_db(cfg)
        try:
            data = load(con)
            history = _rows(
                con,
                "SELECT * FROM meta_alert_events ORDER BY fired_at DESC, severity, app_id LIMIT 200",
            )
        finally:
            if con is not None:
                con.close()
        return templates.TemplateResponse(
            request,
            "alerts.html",
            context(request, "alerts", source=source, history=history, **data),
        )

    @app.get("/fragments/status", response_class=HTMLResponse)
    def status_fragment(request: Request):
        return templates.TemplateResponse(request, "_status.html", context(request, "fragment"))

    @app.post("/run", response_class=HTMLResponse)
    def run_now(request: Request):
        if not secrets.compare_digest(request.headers.get("x-pulse-token", ""), token):
            return PlainTextResponse("missing or wrong token", 403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "") not in hosts:
            return PlainTextResponse("cross-origin request refused", 403)
        st = state()
        lock = st["lock"]
        s = ui.S[ui.request_lang(request)]
        if lock and not runstate.RunLock(Path(cfg.state_dir) / runstate.LOCK_NAME).is_stale(lock):
            msg = s["run_busy"].format(pid=lock.get("pid"), since=lock.get("started_at"))
            return HTMLResponse(f'<div class="callout warn" role="status">{msg}</div>', 409)
        pid = launch(cfg)
        msg = s["run_started"].format(pid=pid)
        return HTMLResponse(f'<div class="callout info" role="status">{msg}</div>', 202)

    # SDK pages (Canlı / Live, levels and heatmaps): src/studio_pulse/collector/web.py
    from ..collector.web import register as register_live

    templates.env.globals["nav_extra"] = register_live(
        app, templates, cfg, context=context, open_db=open_db, rows=_rows, fetch_live=live_fetcher
    )

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        con, source = open_db(cfg)
        try:
            results = checks.latest_results(con) if con is not None else []
        finally:
            if con is not None:
                con.close()
        st = state()
        return JSONResponse(
            {
                "version": __version__,
                "config": str(cfg.path),
                "database": source,
                "heartbeat": st["hb"],
                "running": st["running"],
                "checks": checks.summarize(results),
                "pid": os.getpid(),
            }
        )

    from ..brain.web import register as register_brain  # /insights and /ask

    register_brain(app, cfg, templates, context=context, hosts=hosts)

    from ..radar.page import register as register_radar

    register_radar(app, cfg, templates, context)

    from .studio import register as register_studio  # /studio: the one-window app view

    register_studio(app, cfg, templates, context=context, open_db=open_db, rows=_rows, hosts=hosts)

    from .settings_page import register as register_settings  # /settings: sources, schedule

    register_settings(
        app, cfg, templates, context=context, open_db=open_db, rows=_rows, hosts=hosts
    )
    return app
