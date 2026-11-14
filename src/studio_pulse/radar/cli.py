"""`pulse radar ...` commands (registered by studio_pulse.cli)."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

from ..config import Config, load_config


def _replay_config(replay: Path, workdir: Path) -> Config:
    cfg = load_config(replay / "pulse.yaml")
    workdir.mkdir(parents=True, exist_ok=True)
    raw = dict(cfg.raw)
    radar = dict(raw.get("radar") or {})
    radar["out_dir"] = str(workdir / "radar")
    play = dict(radar.get("play") or {})
    if play.get("map_file"):
        play["map_file"] = str((replay / play["map_file"]).resolve())
    radar["play"] = play
    raw["radar"] = radar
    return dataclasses.replace(
        cfg,
        warehouse=workdir / "radar.duckdb",
        state_dir=workdir / "pulse-state",
        site_dir=workdir / "site",
        raw=raw,
    )


def cmd_radar_run(args: argparse.Namespace) -> int:
    from .run import run_radar

    session = None
    sleep = None
    if args.replay:
        from .replay import RadarReplaySession

        replay = Path(args.replay)
        session = RadarReplaySession(replay)
        cfg = (
            load_config(args.config) if args.config else _replay_config(replay, Path(args.workdir))
        )
        sleep = lambda _s: None  # noqa: E731 - nothing is sent, so nothing to space
    else:
        cfg = load_config(args.config)
    day = dt.date.fromisoformat(args.date) if args.date else None
    kwargs = {
        "session": session,
        "day": day,
        "use_llm": not args.no_llm,
        "max_requests": args.max_requests,
    }
    if sleep:
        kwargs["sleep"] = sleep
    res = run_radar(cfg, **kwargs)
    print(
        json.dumps(
            {
                "ok": res.ok,
                "day": res.day.isoformat(),
                "files": res.files,
                "counts": res.counts,
                "errors": res.errors,
                "notes": res.notes,
            },
            indent=2,
            default=str,
            ensure_ascii=False,
        )
    )
    return 0 if res.ok else 1


def cmd_radar_show(args: argparse.Namespace) -> int:
    from .settings import from_config

    s = from_config(load_config(args.config))
    files = sorted(s.out_dir.glob(f"*.{args.lang}.md"))
    if args.date:
        files = [p for p in files if p.name.startswith(args.date)]
    if not files:
        print(f"no radar digest in {s.out_dir}", file=sys.stderr)
        return 2
    sys.stdout.write(files[-1].read_text(encoding="utf-8"))
    return 0


def add_parser(sub: argparse._SubParsersAction, with_config) -> None:
    r = sub.add_parser("radar", help="daily market radar: store charts, breakouts, trends, digest")
    rs = r.add_subparsers(dest="radar_cmd", required=True)
    run = with_config(rs.add_parser("run", help="fetch today's charts and write the digest"))
    run.add_argument("--replay", help="answer every request from a recorded fixture directory")
    run.add_argument(
        "--workdir",
        default=".pulse-radar-replay",
        help="with --replay and no --config: where the warehouse and digest go",
    )
    run.add_argument("--date", help="snapshot date (default: today or the config's as_of)")
    run.add_argument("--no-llm", action="store_true", help="keyword tags only, no LLM call")
    run.add_argument("--max-requests", type=int, help="cap on network requests for this run")
    run.set_defaults(func=cmd_radar_run)
    from .estimate.cli import add_parsers as add_estimate_parsers  # watch, plan

    add_estimate_parsers(rs, with_config)
    show = with_config(rs.add_parser("show", help="print the newest digest"))
    show.add_argument("--lang", choices=["tr", "en"], default="tr")
    show.add_argument("--date")
    show.set_defaults(func=cmd_radar_show)
