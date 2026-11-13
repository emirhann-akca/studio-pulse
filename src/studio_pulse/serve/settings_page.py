"""/settings: connect data sources without editing YAML, and set up the daily refresh.

What it writes, and where:
- source entries go to pulse.settings.yaml next to the config (config.apply_settings
  merges it at load time); pulse.yaml itself is never rewritten;
- App Store Connect key ids and the key file's *path* go to pulse.settings.env (mode 600),
  which load_config reads like `env_file:`. The key itself stays where the user keeps it,
  and no page ever shows a stored value: only "set" or "missing" per variable name.

Google Play needs no new key: the Play Console's reports bucket is read with the Google
account already signed in for the GA4 / BigQuery sources (Application Default
Credentials), so the form asks only for the bucket id and the package name.

Every POST needs the page token and a same-machine origin, like /run and /ask; the
"test" buttons make one read-only request each, only when pressed.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from markupsafe import escape

from .. import schedule
from ..config import SETTINGS_ENV, SETTINGS_FILE
from .ui import S, request_lang

PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
BUCKET = re.compile(r"(?:gs://)?(pubsite_prod_(?:rev_)?\d{6,})/?.*")
DIGITS = re.compile(r"\d{5,12}")
KEY_ID = re.compile(r"[A-Z0-9]{10}")
ISSUER = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ASC_VARS = ("PULSE_ASC_KEY_ID", "PULSE_ASC_ISSUER_ID", "PULSE_ASC_PRIVATE_KEY_PATH")
SLACK_HOOK = re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")

KINDS = {
    "ga4_bigquery": ("GA4 (BigQuery)", "GA4 (BigQuery)"),
    "ga4_files": ("GA4 (dışa aktarım dosyaları)", "GA4 (export files)"),
    "crashlytics_bigquery": ("Crashlytics (BigQuery)", "Crashlytics (BigQuery)"),
    "crashlytics_files": ("Crashlytics (dosyalar)", "Crashlytics (files)"),
    "pulse_sdk": ("Studio Pulse SDK", "Studio Pulse SDK"),
    "play_console": ("Google Play raporları", "Google Play reports"),
    "app_store": ("App Store satış raporları", "App Store sales reports"),
    "play_reviews": ("Google Play yorumları", "Google Play reviews"),
    "app_store_reviews": ("App Store yorumları", "App Store reviews"),
    "android_vitals": ("Android vitals", "Android vitals"),
    "revenuecat": ("RevenueCat", "RevenueCat"),
    "admob": ("AdMob", "AdMob"),
    "unity_jsonl": ("Unity telemetri", "Unity telemetry"),
    "meta_ads": ("Meta reklam harcaması", "Meta ad spend"),
    "google_ads": ("Google Ads harcaması", "Google Ads spend"),
    "tiktok_ads": ("TikTok reklam harcaması", "TikTok ad spend"),
    "ua_csv": ("Reklam harcaması (CSV)", "Ad spend (CSV)"),
}


def _kind(name: str) -> str:
    return name.split("@", 1)[0]


def read_overlay(base_dir: Path) -> dict[str, Any]:
    f = base_dir / SETTINGS_FILE
    if not f.is_file():
        return {}
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def write_overlay(base_dir: Path, app_id: str, sources: dict[str, Any]) -> Path:
    data = read_overlay(base_dir)
    app = data.setdefault("apps", {}).setdefault(app_id, {})
    app.setdefault("sources", {}).update(sources)
    f = base_dir / SETTINGS_FILE
    tmp = f.with_suffix(".tmp")
    tmp.write_text(
        "# Written by Studio Pulse > Settings. Merged into the config at load time;\n"
        "# a source set to null here is removed. Keys live in "
        + SETTINGS_ENV
        + ".\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(f)
    return f


def write_env(base_dir: Path, values: dict[str, str]) -> Path:
    """Set NAME=VALUE lines in pulse.settings.env (created with mode 600) and in this process."""
    f = base_dir / SETTINGS_ENV
    current: dict[str, str] = {}
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                current[k.strip()] = v.strip()
    current.update(values)
    body = (
        "# Written by Studio Pulse > Settings. Private: keep out of version control.\n"
        + "".join(f"{k}={v}\n" for k, v in current.items())
    )
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(f, 0o600)
    except OSError:  # Windows: ACLs, not mode bits
        pass
    os.environ.update(values)
    return f


def test_play(bucket: str, package: str, client: Any = None) -> tuple[bool, str]:
    """One read-only listing of the bucket's install reports for this package."""
    try:
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        names = [
            b.name
            for b in client.list_blobs(bucket, prefix="stats/installs/", max_results=500)
            if package in b.name
        ]
    except ImportError:
        return False, "google-cloud-storage"
    except Exception as exc:  # noqa: BLE001 - shown to the user as the reason
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    return bool(names), str(len(names))


def test_app_store(
    vendor: str, session: Any = None, today: dt.date | None = None
) -> tuple[bool, str]:
    """One sales-report request for a day three days ago: 200 or 404 means the key works."""
    try:
        from ..connectors.reviews import asc_token
        from ..connectors.stores import ASC_SALES_URL

        tok = asc_token()
    except ImportError:
        return False, "pyjwt"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    import requests

    day = (today or dt.date.today()) - dt.timedelta(days=3)
    r = (session or requests).get(
        ASC_SALES_URL,
        params={
            "filter[frequency]": "DAILY",
            "filter[reportType]": "SALES",
            "filter[reportSubType]": "SUMMARY",
            "filter[vendorNumber]": vendor,
            "filter[reportDate]": day.isoformat(),
            "filter[version]": "1_0",
        },  # fmt: skip
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/a-gzip"},
        timeout=30,
    )
    if r.status_code in (200, 404):
        return True, str(r.status_code)
    try:
        detail = r.json()["errors"][0].get("detail", "")
    except (ValueError, KeyError, IndexError, TypeError):
        detail = ""
    return False, f"HTTP {r.status_code} {detail}".strip()


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
    base_dir = Path(cfg.base_dir)

    def guard(request: Request) -> PlainTextResponse | None:
        token = str(templates.env.globals.get("token", ""))
        if not token or not secrets.compare_digest(request.headers.get("x-pulse-token", ""), token):
            return PlainTextResponse("missing or wrong token", 403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "") not in hosts:
            return PlainTextResponse("cross-origin request refused", 403)
        return None

    async def form(request: Request) -> dict[str, str]:
        raw = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        return {k: (v[0] if v else "").strip() for k, v in raw.items()}

    def note(kind: str, text: str) -> HTMLResponse:
        return HTMLResponse(f'<div class="callout {kind}" role="status">{escape(text)}</div>')

    def app_cfg(app_id: str) -> Any:
        return next((a for a in cfg.apps if a.id == app_id), None)

    def state(lang: str) -> dict[str, Any]:
        con, source = open_db(cfg)
        try:
            fresh = rows(con, "SELECT * FROM fct_source_freshness") if con is not None else []
        finally:
            if con is not None:
                con.close()
        over = read_overlay(base_dir).get("apps") or {}
        today = cfg.as_of or dt.date.today()
        apps = []
        for a in cfg.apps:
            mine = over.get(a.id, {}).get("sources") or {}
            srcs = []
            for name in a.sources:
                f = next((r for r in fresh if r["app_id"] == a.id and r["source"] == name), None)
                through = f["data_through"] if f else None
                lag = (today - through).days if through else None
                srcs.append({
                    "name": name,
                    "label": KINDS.get(_kind(name), (name, name))[0 if lang == "tr" else 1],
                    "from_settings": name in mine,
                    "through": through,
                    "lag": lag,
                    "rows": f["rows_loaded"] if f else None,
                    "status": "ok" if through and lag is not None and lag <= 3
                    else ("stale" if through else "nodata"),
                })  # fmt: skip
            play = a.sources.get("play_console") or {}
            store = a.sources.get("app_store") or {}
            bucket = BUCKET.fullmatch(str(play.get("path", "")))
            pkgs = (a.sources.get("pulse_sdk") or {}).get("bundle_ids") or []
            apps.append({
                "id": a.id,
                "name": a.name,
                "sources": srcs,
                "play_bucket": bucket.group(1) if bucket else "",
                "play_package": play.get("package") or next((p for p in pkgs if "." in p), ""),
                "apple_id": str(store.get("apple_id") or (a.sources.get("app_store_reviews") or {}).get("apple_id") or ""),
                "vendor": str(store.get("vendor_number") or ""),
            })  # fmt: skip
        try:
            sched = schedule.backend().status(schedule.DEFAULT_NAME)
        except schedule.ScheduleError as exc:
            sched = {"registered": False, "note": str(exc)}
        return {
            "set_apps": apps,
            "asc_vars": [(v, bool(os.environ.get(v))) for v in ASC_VARS],
            "slack_set": bool(os.environ.get("PULSE_SLACK_WEBHOOK_URL")),
            "schedule": sched,
            "settings_file": str(base_dir / SETTINGS_FILE),
            "source": source,
        }

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, lang: str | None = None):
        lg = request_lang(request, lang)
        return templates.TemplateResponse(
            request, "settings.html", context(request, "settings", lang=lg, **state(lg))
        )

    @app.post("/settings/play/{app_id}", response_class=HTMLResponse)
    async def save_play(request: Request, app_id: str):
        if (bad := guard(request)) is not None:
            return bad
        s = S[request_lang(request)]
        a = app_cfg(app_id)
        f = await form(request)
        m = BUCKET.fullmatch(f.get("bucket", ""))
        pkg = f.get("package", "")
        if a is None:
            return note("bad", s["set_unknown_app"])
        if not m:
            return note("bad", s["set_bad_bucket"])
        if not PACKAGE.fullmatch(pkg):
            return note("bad", s["set_bad_package"])
        bucket = m.group(1)
        sources = {
            "play_console": {"path": f"gs://{bucket}/stats/", "package": pkg},
            "play_reviews": {"path": f"gs://{bucket}/reviews/", "package": pkg},
        }
        write_overlay(base_dir, app_id, sources)
        a.sources.update(sources)
        return note("info", s["set_saved"])

    @app.post("/settings/appstore/{app_id}", response_class=HTMLResponse)
    async def save_app_store(request: Request, app_id: str):
        if (bad := guard(request)) is not None:
            return bad
        s = S[request_lang(request)]
        a = app_cfg(app_id)
        f = await form(request)
        if a is None:
            return note("bad", s["set_unknown_app"])
        apple_id, vendor = f.get("apple_id", ""), f.get("vendor", "")
        if not DIGITS.fullmatch(apple_id) or not DIGITS.fullmatch(vendor):
            return note("bad", s["set_bad_ids"])
        env: dict[str, str] = {}
        key_id, issuer, key_path = (
            f.get("key_id", ""),
            f.get("issuer_id", "").lower(),
            f.get("key_path", ""),
        )
        if key_id or issuer or key_path:
            if not KEY_ID.fullmatch(key_id) or not ISSUER.fullmatch(issuer):
                return note("bad", s["set_bad_key"])
            kp = Path(key_path).expanduser()
            if kp.suffix != ".p8" or not kp.is_file():  # only checked to exist, never read here
                return note("bad", s["set_bad_key_path"])
            env = {"PULSE_ASC_KEY_ID": key_id, "PULSE_ASC_ISSUER_ID": issuer,
                   "PULSE_ASC_PRIVATE_KEY_PATH": str(kp.resolve())}  # fmt: skip
        sources = {
            "app_store": {"vendor_number": vendor, "apple_id": apple_id},
            "app_store_reviews": {"apple_id": apple_id},
        }
        write_overlay(base_dir, app_id, sources)
        if env:
            write_env(base_dir, env)
        a.sources.update(sources)
        return note("info", s["set_saved"])

    @app.post("/settings/test/{app_id}/{kind}", response_class=HTMLResponse)
    async def test_source(request: Request, app_id: str, kind: str):
        if (bad := guard(request)) is not None:
            return bad
        s = S[request_lang(request)]
        a = app_cfg(app_id)
        if a is None:
            return note("bad", s["set_unknown_app"])
        if kind == "play":
            play = a.sources.get("play_console") or {}
            m = BUCKET.fullmatch(str(play.get("path", "")))
            if not m or not play.get("package"):
                return note("warn", s["set_test_first"])
            ok, detail = test_play(m.group(1), str(play["package"]))
            if ok:
                return note("info", s["set_play_ok"].format(n=detail))
            if detail == "0":
                return note("warn", s["set_play_empty"])
            return note("bad", s["set_test_fail"].format(detail=detail))
        if kind == "appstore":
            vendor = str((a.sources.get("app_store") or {}).get("vendor_number") or "")
            if not vendor:
                return note("warn", s["set_test_first"])
            ok, detail = test_app_store(vendor)
            if ok:
                return note("info", s["set_asc_ok"])
            return note("bad", s["set_test_fail"].format(detail=detail))
        return PlainTextResponse("unknown source", 404)

    @app.post("/settings/slack", response_class=HTMLResponse)
    async def save_slack(request: Request):
        """The Slack incoming webhook alerts are sent to (scheduled runs with notify)."""
        if (bad := guard(request)) is not None:
            return bad
        s = S[request_lang(request)]
        url = (await form(request)).get("webhook", "")
        if not SLACK_HOOK.fullmatch(url):
            return note("bad", s["set_bad_slack"])
        write_env(base_dir, {"PULSE_SLACK_WEBHOOK_URL": url})
        return note("info", s["set_slack_saved"])

    @app.post("/settings/schedule", response_class=HTMLResponse)
    async def install_schedule(request: Request):
        if (bad := guard(request)) is not None:
            return bad
        s = S[request_lang(request)]
        f = await form(request)
        try:
            hour, minute = schedule.parse_time(f.get("at") or "08:30")
            job = schedule.daily_job(cfg, hour, minute, notify=f.get("notify") == "1")
            b = schedule.backend()
            b.install(job)
            schedule.save_record(job, b.kind)
        except (schedule.ScheduleError, ValueError, OSError) as exc:
            return note("bad", s["set_test_fail"].format(detail=str(exc)[:200]))
        return note("info", s["set_sched_ok"].format(at=f"{hour:02d}:{minute:02d}"))
