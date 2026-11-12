"""The two places the core pipeline calls into the collector package.

- config.load_config -> add_registry_apps: registry apps join cfg.apps
- pipeline._ingest_source("pulse_sdk") -> ingest_pulse_sdk
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import ConfigError


def add_registry_apps(cfg: Any) -> None:
    from .registry import registry_apps

    cfg.apps.extend(registry_apps(cfg))


def ingest_pulse_sdk(
    con: Any,
    cfg: Any,
    app: Any,
    name: str,
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    log: Any,
) -> Any:
    from .ingest import ingest
    from .settings import from_config

    bundles = [str(b) for b in (src.get("bundle_ids") or [])]
    if not bundles:
        raise ConfigError(f"app {app.id!r}: pulse_sdk needs bundle_ids")
    data_dir = cfg.resolve(src["data_dir"]) if src.get("data_dir") else from_config(cfg).data_dir
    return ingest(
        con,
        app_id=app.id,
        source=name,
        data_dir=data_dir,
        bundle_ids=bundles,
        timezone=app.timezone,
        start=start,
        end=end,
        log=log,
    )
