"""Configuration loading.

A config is a YAML file (see pulse.example.yaml). Values may reference
environment variables as ${NAME} or ${NAME:-default}; credentials are never
stored in the config itself. Relative paths are resolved against the directory
of the config file.

Lookup order when no path is given: $PULSE_CONFIG, ./pulse.local.yaml,
./pulse.yaml.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .connectors.dialect import check_tz

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set")

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class AppConfig:
    id: str
    name: str
    genre: str = "app"
    platforms: list[str] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    # IANA name of the GA4 property's reporting time zone. GA4 event_date is
    # already in it; UTC timestamps from Crashlytics, RevenueCat and the Unity
    # package are converted to it so every source agrees on what "a day" is.
    timezone: str = "UTC"
    # A/B tests read from the GA4 export (connectors/ga4.py ExperimentSpec).
    experiments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Config:
    path: Path | None
    base_dir: Path
    warehouse: Path
    site_dir: Path
    apps: list[AppConfig]
    lookback_days: int = 60
    end_date: dt.date | None = None
    as_of: dt.date | None = None
    title: str = "Studio Pulse"
    is_demo: bool = False
    alerts_path: Path | None = None
    checks_path: Path | None = None
    # Run lock, heartbeat, run history, logs and the warehouse snapshot the UI
    # falls back to while a refresh holds DuckDB's single write lock.
    state_dir: Path | None = None
    fx_rates: dict[str, float] = field(default_factory=dict)
    max_bytes_billed: int = 2_000_000_000
    # UA settings: creative_parser, min_creative_impressions (docs/METRICS.md).
    ua: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_dir is None:
            self.state_dir = self.warehouse.parent / "pulse-state"

    def window(self) -> tuple[dt.date, dt.date]:
        """Inclusive [start, end] ingest window."""
        end = self.end_date or (dt.date.today() - dt.timedelta(days=1))
        start = end - dt.timedelta(days=self.lookback_days - 1)
        return start, end

    def app(self, app_id: str) -> AppConfig:
        for a in self.apps:
            if a.id == app_id:
                return a
        raise ConfigError(f"unknown app {app_id!r}")

    def resolve(self, p: str | Path) -> Path:
        path = Path(p).expanduser()
        return path if path.is_absolute() else (self.base_dir / path)


def _date(v: Any) -> dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def _timezone(value: Any, app_id: str) -> str:
    tz = str(value or "UTC")
    try:
        check_tz(tz)
        zoneinfo.ZoneInfo(tz)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
        raise ConfigError(f"app {app_id!r}: unknown time zone {tz!r}") from exc
    return tz


def _experiments(value: Any, app_id: str) -> list[dict[str, Any]]:
    from .connectors.ga4 import ExperimentSpec

    out = []
    for e in value or []:
        try:
            ExperimentSpec.from_dict(dict(e))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"app {app_id!r}: {exc}") from exc
        out.append(dict(e))
    ids = [e["id"] for e in out]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"app {app_id!r}: experiment ids must be unique")
    return out


def find_config(explicit: str | Path | None = None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    elif os.environ.get("PULSE_CONFIG"):
        candidates.append(Path(os.environ["PULSE_CONFIG"]))
    else:
        candidates += [Path("pulse.local.yaml"), Path("pulse.yaml")]
    for c in candidates:
        if c.expanduser().exists():
            return c.expanduser().resolve()
    raise ConfigError(
        "no config found (tried: " + ", ".join(str(c) for c in candidates) + "). "
        "Copy pulse.example.yaml to pulse.local.yaml or run `pulse demo`."
    )


def load_env_files(spec: Any, base_dir: Path) -> list[Path]:
    """`env_file:` (a path or a list, relative to the config): NAME=VALUE lines into the
    environment, without overriding what is already set. For launches without a shell
    (the desktop icon, a scheduled run): project ids and keys stay in a private file
    instead of the config. Blank lines and `#` comments are skipped."""
    loaded = []
    for item in spec if isinstance(spec, list) else [spec]:
        f = (base_dir / str(item)).expanduser()
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip().removeprefix("export ").strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                os.environ.setdefault(name, value.strip().strip('"').strip("'"))
        loaded.append(f)
    return loaded


SETTINGS_FILE = "pulse.settings.yaml"  # written by the Settings page, next to the config
SETTINGS_ENV = "pulse.settings.env"  # its keys (mode 600), never in the config itself


def apply_settings(data: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Merge the Settings page's overlay into the config: `apps.<id>.sources.<name>` adds or
    replaces that source (null removes it). The config file itself is never rewritten."""
    f = base_dir / SETTINGS_FILE
    if not f.is_file():
        return data
    over = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    by_id = (over.get("apps") or {}) if isinstance(over, dict) else {}
    for a in data.get("apps") or []:
        extra = (by_id.get(a.get("id")) or {}).get("sources") or {}
        if not extra:
            continue
        merged = dict(a.get("sources") or {})
        for name, src in extra.items():
            if src is None:
                merged.pop(name, None)
            else:
                merged[name] = src
        a["sources"] = merged
    return data


def load_config(path: str | Path | None = None) -> Config:
    p = find_config(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    load_env_files(SETTINGS_ENV, p.parent)
    if data.get("env_file"):
        load_env_files(data["env_file"], p.parent)
    data = apply_settings(data, p.parent)
    cfg = config_from_dict(data, base_dir=p.parent, path=p)
    if data.get("collector"):  # SDK apps the collector registered appear without a config edit
        from .collector.hooks import add_registry_apps

        add_registry_apps(cfg)
    return cfg


def config_from_dict(data: dict[str, Any], base_dir: Path, path: Path | None = None) -> Config:
    data = _expand_env(data)
    apps_raw = data.get("apps") or []
    if not apps_raw and not data.get("collector"):
        raise ConfigError("config has no apps")
    apps = []
    seen = set()
    for a in apps_raw:
        if "id" not in a:
            raise ConfigError("every app needs an id")
        if not re.fullmatch(r"[a-z0-9_]+", a["id"]):
            raise ConfigError(f"app id {a['id']!r} must match [a-z0-9_]+")
        if a["id"] in seen:
            raise ConfigError(f"duplicate app id {a['id']!r}")
        seen.add(a["id"])
        apps.append(
            AppConfig(
                id=a["id"],
                name=a.get("name", a["id"]),
                genre=a.get("genre", "app"),
                platforms=[str(x).upper() for x in a.get("platforms", [])],
                sources=a.get("sources") or {},
                timezone=_timezone(a.get("timezone"), a["id"]),
                experiments=_experiments(a.get("experiments"), a["id"]),
            )
        )

    def rp(key: str, default: str) -> Path:
        v = Path(str(data.get(key, default))).expanduser()
        return v if v.is_absolute() else base_dir / v

    def optional_path(key: str) -> Path | None:
        v = data.get(key)
        if not v:
            return None
        p = Path(str(v)).expanduser()
        return p if p.is_absolute() else base_dir / p

    warehouse = rp("warehouse", "local/pulse.duckdb")
    return Config(
        path=path,
        base_dir=base_dir,
        warehouse=warehouse,
        site_dir=rp("site_dir", "local/site"),
        apps=apps,
        lookback_days=int(data.get("lookback_days", 60)),
        end_date=_date(data.get("end_date")),
        as_of=_date(data.get("as_of")),
        title=data.get("title", "Studio Pulse"),
        is_demo=bool(data.get("demo", False)),
        alerts_path=optional_path("alerts"),
        checks_path=optional_path("checks"),
        state_dir=optional_path("state_dir") or warehouse.parent / "pulse-state",
        fx_rates={str(k).upper(): float(v) for k, v in (data.get("fx_rates") or {}).items()},
        max_bytes_billed=int(data.get("max_bytes_billed", 2_000_000_000)),
        ua=dict(data.get("ua") or {}),
        raw=data,
    )
