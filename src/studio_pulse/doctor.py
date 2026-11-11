"""`pulse doctor`: check that this machine can run studio-pulse.

Each check returns ok / warn / fail and a one-line detail; fail means pulse
will not work until it is fixed and makes the command exit 1. Credentials are
only ever reported as present or absent with their type: no token, key or
account id is printed.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import os
import platform
import secrets
import shutil
import struct
import sys
import zoneinfo
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__

GCP_SOURCES = ("ga4_bigquery", "crashlytics_bigquery")


@dataclass
class Result:
    name: str
    status: str  # ok | warn | fail | info
    detail: str


def _version(dist: str) -> str | None:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return None


def check_python() -> Result:
    v = sys.version_info
    bits = struct.calcsize("P") * 8
    detail = f"{platform.python_version()} ({bits}-bit, {platform.machine()}) at {sys.executable}"
    if v < (3, 11):
        return Result("python", "fail", detail + "; studio-pulse needs Python 3.11+")
    return Result("python", "ok", detail)


def check_platform() -> Result:
    return Result("platform", "info", f"{platform.platform()} ({sys.platform})")


def check_package() -> Result:
    exe_dir = Path(sys.executable).parent
    bg = "pulse-bg.exe" if sys.platform == "win32" else "pulse-bg"
    found = (exe_dir / bg).exists() or bool(shutil.which("pulse-bg"))
    detail = f"studio-pulse {__version__}; background launcher {'found' if found else 'missing'}"
    return Result("studio-pulse", "ok" if found else "warn", detail)


def check_timezones() -> Result:
    """Windows has no system IANA database: zoneinfo needs the tzdata package there."""
    has_tzdata = _version("tzdata")
    try:
        zoneinfo.ZoneInfo("Europe/Istanbul")
    except zoneinfo.ZoneInfoNotFoundError:
        fix = "install the tzdata package" if not has_tzdata else "reinstall tzdata"
        return Result("time zones", "fail", f"IANA time zones unavailable; {fix}")
    sys_paths = [p for p in zoneinfo.TZPATH if Path(p).is_dir()]
    source = f"tzdata {has_tzdata}" if has_tzdata else "system database"
    if sys_paths and has_tzdata:
        source = f"system database ({sys_paths[0]}), tzdata {has_tzdata} as fallback"
    return Result("time zones", "ok", f"IANA zones from {source}")


def check_duckdb() -> Result:
    try:
        import duckdb

        con = duckdb.connect(":memory:")
        con.execute("SELECT 1").fetchone()
        con.close()
    except Exception as exc:  # broken wheel, missing native library
        return Result("duckdb", "fail", f"cannot open an in-memory database: {exc}")
    return Result("duckdb", "ok", f"duckdb {duckdb.__version__}, pyarrow {_version('pyarrow')}")


def check_ui() -> Result:
    missing = [d for d in ("fastapi", "uvicorn", "jinja2") if not _version(d)]
    if missing:
        return Result("web UI", "warn", "pulse serve unavailable: missing " + ", ".join(missing))
    return Result(
        "web UI",
        "ok",
        f"fastapi {_version('fastapi')}, uvicorn {_version('uvicorn')}, jinja2 {_version('jinja2')}",
    )


def check_encoding() -> Result:
    enc = getattr(sys.stdout, "encoding", None) or "none"
    utf8_mode = bool(sys.flags.utf8_mode)
    detail = f"console {enc}, UTF-8 mode {'on' if utf8_mode else 'off'}; files are always written as UTF-8"
    return Result("encoding", "info", detail)


def check_long_paths() -> Result | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as k:
            enabled = winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        enabled = False
    if enabled:
        return Result("long paths", "ok", "LongPathsEnabled = 1")
    return Result(
        "long paths",
        "info",
        "LongPathsEnabled is off: keep the data folder short (e.g. under %LOCALAPPDATA%)",
    )


def _writable(d: Path) -> str | None:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".pulse-doctor-{secrets.token_hex(4)}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def adc_status() -> tuple[str, str]:
    """(status, detail) for Application Default Credentials; never prints secrets."""
    env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env:
        p = Path(env)
        where = "GOOGLE_APPLICATION_CREDENTIALS"
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
        where = "gcloud default location"
    else:
        cfg = os.environ.get("CLOUDSDK_CONFIG")
        base = Path(cfg) if cfg else Path.home() / ".config" / "gcloud"
        p = base / "application_default_credentials.json"
        where = "gcloud default location"
    if not p.exists():
        return "missing", f"no credentials file at the {where}"
    try:
        kind = json.loads(p.read_text(encoding="utf-8")).get("type", "unknown")
    except (OSError, ValueError):
        return "unreadable", f"credentials file at the {where} is not valid JSON"
    return "present", f"{kind} credentials at the {where}"


def check_config(path: str | None) -> list[Result]:
    from .config import ConfigError, load_config

    try:
        cfg = load_config(path)
    except ConfigError as exc:
        msg = str(exc)
        status = "info" if msg.startswith("no config found") and not path else "fail"
        return [Result("config", status, msg)]
    except Exception as exc:  # YAML syntax errors and the like
        return [Result("config", "fail", f"{type(exc).__name__}: {exc}")]
    out = [Result("config", "ok", f"{cfg.path} ({len(cfg.apps)} app(s))")]
    err = _writable(cfg.warehouse.parent)
    out.append(
        Result("warehouse", "fail" if err else "ok", err or f"{cfg.warehouse} (folder writable)")
    )
    err = _writable(cfg.state_dir)
    out.append(Result("state dir", "fail" if err else "ok", err or f"{cfg.state_dir}"))

    from .pipeline import default_file

    for attr, name in (("alerts_path", "alerts.yaml"), ("checks_path", "checks.yaml")):
        f = default_file(cfg, None, attr, name)
        out.append(
            Result(name, "ok" if f else "warn", str(f) if f else f"no {name}: nothing to evaluate")
        )

    kinds = {k for a in cfg.apps for k, s in a.sources.items() if (s or {}).get("enabled", True)}
    uses_gcp = bool(kinds & set(GCP_SOURCES)) or any(
        str((s or {}).get("path", "")).startswith("gs://")
        for a in cfg.apps
        for s in a.sources.values()
    )
    if uses_gcp:
        if not _version("google-cloud-bigquery"):
            out.append(
                Result("gcp extra", "fail", "config reads BigQuery/GCS: install studio-pulse[gcp]")
            )
        status, detail = adc_status()
        out.append(Result("google credentials", "ok" if status == "present" else "fail", detail))
    else:
        status, detail = adc_status()
        out.append(Result("google credentials", "info", f"not needed by this config ({detail})"))

    out += check_api_sources(cfg)

    from .runstate import read_heartbeat

    hb = read_heartbeat(cfg.state_dir)
    if hb:
        detail = (
            f"last run {hb.get('status')} at {hb.get('last_end') or hb.get('last_start')}; "
            f"last success {hb.get('last_success') or 'never'}"
        )
        bad = hb.get("status") in ("failed", "interrupted")
        out.append(Result("last refresh", "warn" if bad else "ok", detail))
    else:
        out.append(Result("last refresh", "info", "no refresh recorded yet"))
    return out


# Environment variables each API source needs (any one of a tuple suffices); only
# whether they are set is reported, never their values.
API_CREDENTIALS = {
    "meta_ads": (("PULSE_META_ACCESS_TOKEN",), "ua", "dlt"),
    "tiktok_ads": (("PULSE_TIKTOK_ACCESS_TOKEN",), "ua", "dlt"),
    "google_ads": (("PULSE_GOOGLE_ADS_ACCESS_TOKEN", "@adc"), "ua", "dlt"),
    "play_reviews": (("PULSE_PLAY_ACCESS_TOKEN", "@adc"), None, None),
    "android_vitals": (("PULSE_PLAY_ACCESS_TOKEN", "@adc"), None, None),
    "app_store_reviews": (("PULSE_ASC_TOKEN", "PULSE_ASC_KEY_ID"), "stores", "jwt"),
}


def check_api_sources(cfg) -> list[Result]:
    import importlib.util

    out = []
    for app in cfg.apps:
        for key, src in app.sources.items():
            src = src or {}
            kind = key.split("@", 1)[0]
            if kind not in API_CREDENTIALS or src.get("enabled", True) is False:
                continue
            name = f"{app.id}.{key}"
            if src.get("replay"):
                out.append(Result(name, "info", "replays recorded responses (no credential used)"))
                continue
            envs, extra, module = API_CREDENTIALS[kind]
            if src.get("token_env"):
                envs = (str(src["token_env"]),)
            if module and importlib.util.find_spec(module) is None:
                needs_module = not (
                    kind == "app_store_reviews" and os.environ.get("PULSE_ASC_TOKEN")
                )
                if needs_module:
                    out.append(
                        Result(name, "fail", f"install studio-pulse[{extra}] ({module} is missing)")
                    )
                    continue
            found = [e for e in envs if e != "@adc" and os.environ.get(e)]
            if not found and "@adc" in envs and adc_status()[0] == "present":
                found = ["Application Default Credentials"]
            if found:
                out.append(Result(name, "ok", f"credential present: {found[0]}"))
            else:
                want = " or ".join(e if e != "@adc" else "ADC" for e in envs)
                out.append(Result(name, "fail", f"no credential: set {want}"))
    return out


def check_schedules() -> Result:
    from .schedule import ScheduleError, backend, list_records

    recs = list_records()
    try:
        b = backend()
    except ScheduleError as exc:
        return Result("schedule", "info", str(exc))
    if not recs:
        return Result("schedule", "info", f"no daily refresh registered ({b.kind})")
    parts = []
    status = "ok"
    for r in recs:
        st = b.status(r["name"])
        if not st.get("registered"):
            status = "warn"
        parts.append(
            f"{r['name']} at {r['at']}: {'registered' if st.get('registered') else 'NOT registered'}"
        )
    return Result("schedule", status, "; ".join(parts))


def run(config: str | None = None) -> list[Result]:
    results = [
        check_python(),
        check_platform(),
        check_package(),
        check_timezones(),
        check_duckdb(),
        check_ui(),
        check_encoding(),
    ]
    lp = check_long_paths()
    if lp:
        results.append(lp)
    results += check_config(config)
    results.append(check_schedules())
    return results


def as_json(results: list[Result]) -> str:
    return json.dumps(
        {
            "version": __version__,
            "ok": not any(r.status == "fail" for r in results),
            "checks": [asdict(r) for r in results],
        },
        indent=2,
    )


def format_console(results: list[Result]) -> str:
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "  - "}
    lines = [f"[{marks[r.status]}] {r.name:<19} {r.detail}" for r in results]
    fails = sum(r.status == "fail" for r in results)
    lines.append("All required checks passed." if not fails else f"{fails} check(s) failed.")
    return "\n".join(lines)
