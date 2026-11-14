"""`pulse radar adintel refresh|show` (registered by radar/cli.py)."""

from __future__ import annotations

import argparse
import datetime as dt
import json

from ... import warehouse
from ...config import load_config


def cmd_refresh(args: argparse.Namespace) -> int:
    from ..dossier.run import default_project
    from . import google_atc

    cfg = load_config(args.config)
    s = google_atc.settings(cfg)
    if args.max_gb is not None:
        if not 0 < args.max_gb <= google_atc.HARD_MAX_GB:
            raise SystemExit(f"--max-gb must be in (0, {google_atc.HARD_MAX_GB:g}]")
        s.cap_bytes = int(args.max_gb * 1e9)
    con = warehouse.connect(cfg.warehouse)
    try:
        res = google_atc.refresh(
            con,
            project=default_project(),
            s=s,
            today=dt.date.fromisoformat(args.date) if args.date else None,
            force=args.force,
            dry_run_only=args.dry_run,
        )
    finally:
        con.close()
    print(json.dumps(res, indent=2, default=str, ensure_ascii=False))
    return 0 if res["status"] in ("ok", "fresh", "dry_run") else 1


def cmd_show(args: argparse.Namespace) -> int:
    from . import google_atc

    cfg = load_config(args.config)
    con = warehouse.connect(cfg.warehouse, read_only=True)
    try:
        res = google_atc.lookup_local(con, [args.name], dt.date.today())
    finally:
        con.close()
    print(json.dumps(res, indent=2, default=str, ensure_ascii=False))
    return 0


def add_parser(rs: argparse._SubParsersAction, with_config) -> None:
    a = rs.add_parser(
        "adintel", help="competitor ads: weekly Google Ads Transparency extract (BigQuery)"
    )
    sub = a.add_subparsers(dest="adintel_cmd", required=True)
    r = with_config(sub.add_parser("refresh", help="scan the public dataset once (dry-run first)"))
    r.add_argument("--force", action="store_true", help="refresh even if the extract is fresh")
    r.add_argument("--dry-run", action="store_true", help="only the dry run: bytes, no scan")
    r.add_argument("--max-gb", type=float, help="cap for this run (default radar.adintel.max_gb)")
    r.add_argument("--date", help="extract date (default: today)")
    r.set_defaults(func=cmd_refresh)
    sh = with_config(sub.add_parser("show", help="look an advertiser up in the local extract"))
    sh.add_argument("name", help="developer or advertiser name")
    sh.set_defaults(func=cmd_show)
