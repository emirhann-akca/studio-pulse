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
    fx_rates: dict[str, float] = field(default_factory=dict)
    max_bytes_billed: int = 2_000_000_000
    raw: dict[str, Any] = field(default_factory=dict)

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


def load_config(path: str | Path | None = None) -> Config:
    p = find_config(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return config_from_dict(data, base_dir=p.parent, path=p)


def config_from_dict(data: dict[str, Any], base_dir: Path, path: Path | None = None) -> Config:
    data = _expand_env(data)
    apps_raw = data.get("apps") or []
    if not apps_raw:
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
            )
        )

    def rp(key: str, default: str) -> Path:
        v = Path(str(data.get(key, default))).expanduser()
        return v if v.is_absolute() else base_dir / v

    alerts = data.get("alerts")
    return Config(
        path=path,
        base_dir=base_dir,
        warehouse=rp("warehouse", "local/pulse.duckdb"),
        site_dir=rp("site_dir", "local/site"),
        apps=apps,
        lookback_days=int(data.get("lookback_days", 60)),
        end_date=_date(data.get("end_date")),
        as_of=_date(data.get("as_of")),
        title=data.get("title", "Studio Pulse"),
        is_demo=bool(data.get("demo", False)),
        alerts_path=(base_dir / alerts) if alerts and not Path(alerts).is_absolute() else (Path(alerts) if alerts else None),
        fx_rates={str(k).upper(): float(v) for k, v in (data.get("fx_rates") or {}).items()},
        max_bytes_billed=int(data.get("max_bytes_billed", 2_000_000_000)),
        raw=data,
    )
