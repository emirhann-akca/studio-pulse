"""`pulse radar dossier <apple id | play package>` (registered by radar/cli.py)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from ...config import load_config


def cmd_radar_dossier(args: argparse.Namespace) -> int:
    from .collect import TargetError
    from .run import build_dossier

    cfg = load_config(args.config)
    session = None
    sleep_kw = {}
    if args.replay:
        from .replay import DossierReplaySession

        session = DossierReplaySession(Path(args.replay))
        sleep_kw["sleep"] = lambda _s: None
    s = None
    if args.screenshots is not None:
        from .settings import from_config

        s = from_config(cfg)
        if args.screenshots is not None:
            s.screenshots = max(0, min(10, args.screenshots))
    try:
        res = build_dossier(
            cfg,
            args.target,
            session=session,
            day=dt.date.fromisoformat(args.date) if args.date else None,
            use_llm=not args.no_llm,
            ads=not args.no_ads,
            max_requests=args.max_requests,
            forge=args.forge,
            s=s,
            log=lambda m: print(m, file=sys.stderr),
            **sleep_kw,
        )
    except TargetError as exc:
        print(f"dossier: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {"key": res.key, "files": res.files, "notes": res.notes},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        sys.stdout.write(Path(res.files[args.lang]).read_text(encoding="utf-8"))
        print(json.dumps({"files": res.files}, ensure_ascii=False), file=sys.stderr)
    return 0 if res.ok else 1


def add_parser(rs: argparse._SubParsersAction, with_config) -> None:
    p = with_config(
        rs.add_parser(
            "dossier",
            help="game dossier: store data, screenshots, grounded LLM deconstruction, reviews, "
            "competitor ads and a 'should we make it?' card",
        )
    )
    p.add_argument("target", help="Apple app id (digits), an App Store URL or a Play package")
    p.add_argument("--lang", choices=["tr", "en"], default="tr")
    p.add_argument("--date", help="snapshot date (default: today or the config's as_of)")
    p.add_argument("--no-llm", action="store_true", help="no LLM call (no deconstruction/themes)")
    p.add_argument("--no-ads", action="store_true", help="skip BigQuery and the Meta Ad Library")
    p.add_argument("--screenshots", type=int, help="screenshots to fetch and send (0-10)")
    p.add_argument("--max-requests", type=int, default=60, help="cap on store requests")
    p.add_argument("--replay", help="answer every store request from a recorded directory")
    p.add_argument(
        "--forge",
        action="store_true",
        help="also merge the first-test lesson into creative-forge's market_trends.yaml",
    )
    p.add_argument("--json", action="store_true", help="print the written files as JSON")
    p.set_defaults(func=cmd_radar_dossier)
