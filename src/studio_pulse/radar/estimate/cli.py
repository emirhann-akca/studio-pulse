"""`pulse radar watch add|remove|list` and `pulse radar plan` (registered by radar/cli.py)."""

from __future__ import annotations

import argparse
import json
import sys

from ... import warehouse
from ...config import load_config


def _open(args: argparse.Namespace):
    from .. import store
    from . import stage

    cfg = load_config(args.config)
    con = warehouse.connect(cfg.warehouse)
    store.init_schema(con)
    stage.init_schema(con)
    return cfg, con


def cmd_watch(args: argparse.Namespace) -> int:
    from .collect import WatchError, watch_add, watch_remove, watch_rows

    _cfg, con = _open(args)
    try:
        if args.watch_cmd == "list":
            rows = watch_rows(con)
            if not rows:
                print("watchlist is empty: pulse radar watch add <apple id | play package>")
            for r in rows:
                print(
                    f"{r['store']:5} {r['app_id']:40} {r['name'] or '-'}"
                    f"  (added {r['added_at']:%Y-%m-%d}, last read {r['last_seen'] or 'never'})"
                    + (f"  {r['note']}" if r["note"] else "")
                )
            return 0
        try:
            if args.watch_cmd == "add":
                ok = watch_add(con, args.target, args.note)
                print("added" if ok else "already on the watchlist", args.target)
            else:
                ok = watch_remove(con, args.target)
                print("removed" if ok else "not on the watchlist", args.target)
        except WatchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        con.execute("CHECKPOINT")
        return 0
    finally:
        con.close()


def cmd_plan(args: argparse.Namespace) -> int:
    from ..settings import from_config
    from .plan import request_plan
    from .settings import from_config as est_from_config

    cfg = load_config(args.config)
    con = None
    if cfg.warehouse.exists():
        try:
            con = warehouse.connect(cfg.warehouse, read_only=True)
        except Exception:
            con = None
    try:
        plan = request_plan(from_config(cfg), est_from_config(cfg), con)
    finally:
        if con is not None:
            con.close()
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0 if plan["within_budget"] else 1


def add_parsers(rs: argparse._SubParsersAction, with_config) -> None:
    w = rs.add_parser("watch", help="games looked up every day even when not charted")
    ws = w.add_subparsers(dest="watch_cmd", required=True)
    for name, helptext in (("add", "watch a game"), ("remove", "stop watching a game")):
        p = with_config(ws.add_parser(name, help=helptext))
        p.add_argument("target", help="Apple app id, Play package name or a store URL")
        if name == "add":
            p.add_argument("--note", help="a short note shown by `watch list`")
        p.set_defaults(func=cmd_watch)
    lp = with_config(ws.add_parser("list", help="list watched games"))
    lp.set_defaults(func=cmd_watch)
    plan = with_config(rs.add_parser("plan", help="expected store requests per day"))
    plan.set_defaults(func=cmd_plan)
