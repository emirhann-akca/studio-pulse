"""Collector settings: the `collector:` block of pulse.yaml, environment, CLI flags.

    collector:
      data_dir: local/collector          # local mode: NDJSON segments + collector.sqlite3
      url: http://127.0.0.1:8787         # where `pulse serve` reads /live
      write_keys: ["${PULSE_WRITE_KEY}"] # public, write-only keys compiled into the SDK (two during rotation)
      read_key: ${PULSE_READ_KEY}        # optional: required by /live when set
      bundle_prefixes: [com.grich.]      # optional allowlist; other apps are quarantined
      country_header: CF-IPCountry       # trust this header for the country (only behind a proxy that sets it)
      geoip: local/collector/geoip/dbip-country-lite.mmdb   # optional, `pulse collect setup-geoip`
      live_min_installs: 20              # production installs in 24 h before a game can be live
      mode: local                        # or bigquery (Cloud Run), with bigquery: {project, dataset}

Environment variables (Cloud Run) override the file: PULSE_DATA_DIR,
PULSE_WRITE_KEYS (comma separated), PULSE_READ_KEY, PULSE_COLLECTOR_MODE,
PULSE_BQ_PROJECT, PULSE_BQ_DATASET, PULSE_BQ_LOCATION, PULSE_COUNTRY_HEADER,
PULSE_TRUSTED_PROXY_HOPS, PULSE_BUNDLE_PREFIXES, PULSE_GEOIP.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Accepted when no key is configured, so a laptop demo works out of the box.
# The collector warns when it is reachable from other machines with only this key.
DEFAULT_LOCAL_KEY = "sp_pub_local"
DEFAULT_PORT = 8787


def key_id(key: str) -> str:
    """Short, non-reversible label of a write key, stored with each row (rotation)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


@dataclass
class CollectorSettings:
    data_dir: Path
    mode: str = "local"
    write_keys: list[str] = field(default_factory=list)
    read_key: str | None = None
    bundle_prefixes: list[str] = field(default_factory=list)
    country_header: str | None = None
    trusted_proxy_hops: int = 0
    geoip: Path | None = None
    fsync: bool = True
    rate_per_ip: float = 50.0
    burst_per_ip: int = 500
    rate_per_install: float = 5.0
    burst_per_install: int = 30
    limit_loopback: bool = False
    live_window_s: int = 300
    spark_minutes: int = 30
    new_app_banner_s: int = 900
    dedupe_days: int = 7
    live_min_installs: int = 20
    store_check_country: str = "us"
    url: str | None = None
    bigquery: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in ("local", "bigquery"):
            raise ValueError(f"collector mode must be local or bigquery, not {self.mode!r}")
        if not self.write_keys:
            self.write_keys = [DEFAULT_LOCAL_KEY]

    @property
    def uses_default_key(self) -> bool:
        return self.write_keys == [DEFAULT_LOCAL_KEY]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "collector.sqlite3"

    def key_ids(self) -> dict[str, str]:
        return {k: key_id(k) for k in self.write_keys}

    def public(self) -> dict[str, Any]:
        """For /healthz and logs: no keys."""
        return {
            "mode": self.mode,
            "write_keys": len(self.write_keys),
            "default_key": self.uses_default_key,
            "read_key": bool(self.read_key),
            "bundle_prefixes": self.bundle_prefixes,
            "country_source": self.country_header
            or ("geoip" if self.geoip and self.geoip.exists() else "none"),
        }


def from_dict(
    block: dict[str, Any] | None, base_dir: Path, env: dict[str, str] | None = None
) -> CollectorSettings:
    env = dict(os.environ if env is None else env)
    b = dict(block or {})

    def path(v: Any) -> Path | None:
        if not v:
            return None
        p = Path(str(v)).expanduser()
        return p if p.is_absolute() else base_dir / p

    data_dir = path(env.get("PULSE_DATA_DIR") or b.get("data_dir") or "local/collector")
    bq = dict(b.get("bigquery") or {})
    for k, e in (("project", "PULSE_BQ_PROJECT"), ("dataset", "PULSE_BQ_DATASET")):
        if env.get(e):
            bq[k] = env[e]
    if env.get("PULSE_BQ_LOCATION"):
        bq["location"] = env["PULSE_BQ_LOCATION"]
    geo = path(env.get("PULSE_GEOIP") or b.get("geoip"))
    if geo is None and data_dir is not None:
        default = data_dir / "geoip" / "dbip-country-lite.mmdb"
        geo = default if default.exists() else None
    return CollectorSettings(
        data_dir=data_dir,  # type: ignore[arg-type]
        mode=str(env.get("PULSE_COLLECTOR_MODE") or b.get("mode") or "local"),
        write_keys=_list(env.get("PULSE_WRITE_KEYS") or b.get("write_keys")),
        read_key=env.get("PULSE_READ_KEY") or b.get("read_key") or None,
        bundle_prefixes=_list(env.get("PULSE_BUNDLE_PREFIXES") or b.get("bundle_prefixes")),
        country_header=env.get("PULSE_COUNTRY_HEADER") or b.get("country_header") or None,
        trusted_proxy_hops=int(
            env.get("PULSE_TRUSTED_PROXY_HOPS") or b.get("trusted_proxy_hops") or 0
        ),
        geoip=geo,
        fsync=bool(b.get("fsync", True)),
        rate_per_ip=float(b.get("rate_per_ip", 50.0)),
        burst_per_ip=int(b.get("burst_per_ip", 500)),
        rate_per_install=float(b.get("rate_per_install", 5.0)),
        burst_per_install=int(b.get("burst_per_install", 30)),
        live_window_s=int(b.get("live_window_s", 300)),
        live_min_installs=int(b.get("live_min_installs", 20)),
        store_check_country=str(b.get("store_check_country", "us")),
        url=b.get("url") or None,
        bigquery=bq,
    )


def from_config(cfg: Any, env: dict[str, str] | None = None) -> CollectorSettings:
    """Settings from a loaded studio_pulse Config (its raw `collector:` block)."""
    return from_dict((cfg.raw or {}).get("collector"), cfg.base_dir, env)
