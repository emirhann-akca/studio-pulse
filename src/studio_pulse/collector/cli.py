"""CLI: `pulse collect` (+ setup-geoip, purge) and `pulse apps` (list, refresh)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..config import ConfigError, load_config
from .settings import DEFAULT_PORT, CollectorSettings, from_config, from_dict, parse_write_keys


def settings_for(args: argparse.Namespace) -> CollectorSettings:
    """--data-dir wins; then the config's collector block; then ./local/collector."""
    if getattr(args, "data_dir", None):
        return from_dict({"data_dir": args.data_dir}, Path.cwd())
    try:
        cfg = load_config(args.config)
    except ConfigError:
        if args.config:
            raise
        return from_dict(None, Path.cwd())
    return from_config(cfg)


def cmd_collect(args: argparse.Namespace) -> int:
    s = settings_for(args)
    if args.key:
        s.write_keys, s.key_apps = parse_write_keys(args.key)
    if args.country_header:
        s.country_header = args.country_header
    if args.mode:
        s.mode = args.mode
    if args.no_fsync:
        s.fsync = False
    from .app import serve

    return serve(s, host=args.host, port=args.port)


def cmd_setup_geoip(args: argparse.Namespace) -> int:
    from .geo import GeoError, setup_geoip

    s = settings_for(args)
    try:
        setup_geoip(s.data_dir / "geoip", url=args.url, sha1=args.sha1)
    except (GeoError, OSError) as exc:
        print(f"setup-geoip failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # network errors from requests
        print(f"setup-geoip failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    from .ingest import purge

    s = settings_for(args)
    stats = purge(s.data_dir, retention_days=args.retention_days)
    print(json.dumps(stats))
    return 0


def _table(rows: list[dict[str, Any]]) -> str:
    cols = [
        "app_id",
        "platform",
        "status",
        "display_name",
        "last_version",
        "prod_installs_24h",
        "store_note",
    ]
    out = ["  ".join(f"{c:<18}" for c in cols)]
    for r in rows:
        out.append("  ".join(f"{str(r.get(c) if r.get(c) is not None else '-'):<18}" for c in cols))
    return "\n".join(out)


def cmd_apps_list(args: argparse.Namespace) -> int:
    from .registry import read_registry

    rows = read_registry(settings_for(args).data_dir)
    print(json.dumps(rows, indent=2) if args.json else _table(rows))
    return 0


def cmd_apps_refresh(args: argparse.Namespace) -> int:
    from .registry import StoreChecker, refresh

    s = settings_for(args)
    checker = None
    if args.replay:
        from ..connectors.replay import ReplaySession

        checker = StoreChecker(ReplaySession(args.replay), min_interval_s=0)
    rows = refresh(
        s.data_dir,
        min_installs=args.min_installs if args.min_installs is not None else s.live_min_installs,
        checker=checker or StoreChecker(country=s.store_check_country),
        store_check=not args.no_store_check,
    )
    print(json.dumps(rows, indent=2) if args.json else _table(rows))
    return 0


def register(sub: Any, with_config: Any) -> None:
    c = with_config(
        sub.add_parser(
            "collect",
            help="run the SDK event collector (POST /v2/batch, /live); subcommands: setup-geoip, purge",
        )
    )
    c.add_argument(
        "--data-dir", help="local store folder (default: collector.data_dir or ./local/collector)"
    )
    c.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    c.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    c.add_argument(
        "--key",
        action="append",
        help="accepted public write key (repeatable); key=app_id|app_id binds it to those apps",
    )
    c.add_argument(
        "--country-header", help="trust this request header for the country (e.g. CF-IPCountry)"
    )
    c.add_argument("--mode", choices=["local", "bigquery"], help="store (default: from the config)")
    c.add_argument("--no-fsync", action="store_true", help=argparse.SUPPRESS)
    c.set_defaults(func=cmd_collect)
    csub = c.add_subparsers(dest="collect_cmd")
    g = with_config(
        csub.add_parser("setup-geoip", help="install DB-IP Country Lite (CC BY 4.0) for countries")
    )
    g.add_argument("--data-dir")
    g.add_argument(
        "--url", help="explicit .mmdb.gz URL (default: read from the DB-IP download page)"
    )
    g.add_argument("--sha1", help="expected SHA1 of the uncompressed .mmdb")
    g.set_defaults(func=cmd_setup_geoip)
    pg = with_config(
        csub.add_parser("purge", help="remove deleted installs and expired days from the store")
    )
    pg.add_argument("--data-dir")
    pg.add_argument(
        "--retention-days", type=int, help="default: the contract's raw_retention_days (396)"
    )
    pg.set_defaults(func=cmd_purge)

    a = sub.add_parser("apps", help="SDK app registry: list, refresh (new -> testing -> live)")
    asub = a.add_subparsers(dest="apps_cmd", required=True)
    al = with_config(asub.add_parser("list", help="registry rows"))
    al.add_argument("--data-dir")
    al.add_argument("--json", action="store_true")
    al.set_defaults(func=cmd_apps_list)
    ar = with_config(
        asub.add_parser(
            "refresh", help="live detection: production installs + public store listing"
        )
    )
    ar.add_argument("--data-dir")
    ar.add_argument(
        "--min-installs",
        type=int,
        help="production installs in 24 h (default: collector.live_min_installs, 20)",
    )
    ar.add_argument("--no-store-check", action="store_true", help="never call the stores")
    ar.add_argument("--replay", help="answer store checks from recorded responses (tests)")
    ar.add_argument("--json", action="store_true")
    ar.set_defaults(func=cmd_apps_refresh)
