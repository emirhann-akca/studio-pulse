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


def cmd_radar_news(args: argparse.Namespace) -> int:
    """Read the news feeds (unless --no-fetch), tag new items and list the recent ones."""
    from .. import warehouse
    from ..llm import client_from_settings
    from ..llm import settings as llm_settings
    from . import news
    from .settings import from_config
    from .store import utcnow

    cfg = load_config(args.config)
    s = from_config(cfg)
    day = s.as_of or cfg.as_of or dt.date.today()
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731 - stdout carries the list
    con = warehouse.connect(cfg.warehouse)
    try:
        client = None
        if not args.no_llm:
            client = client_from_settings(
                llm_settings(cfg.raw.get("llm")), state_dir=Path(cfg.state_dir)
            )
        res = news.run_news(
            con,
            day=day,
            out_dir=s.out_dir,
            feeds=news.selected_feeds(s.news_feeds),
            client=client,
            days=args.days,
            fetch=not args.no_fetch,
            max_llm_items=s.news_max_llm_items,
            log=log,
        )
        items = news.recent(con, args.days, utcnow())
    finally:
        con.close()
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2, default=str))
        return 0
    for it in items:
        when = (it["published_at"] or it["first_seen"] or "")[:10]
        topics = ", ".join(it["topics"]) or "-"
        print(f"[{it['relevance'] or '-'}] {when} {it['source_name']}: {it['title']}")
        print(f"    {it['link']}")
        print(
            f"    {topics} ({it['tag_source'] or 'untagged'})"
            + (f": {it['why']}" if it["why"] else "")
        )
    errors = [f for f in res.feeds if f.error]
    print(
        f"{len(items)} item(s) from the last {args.days} day(s); {res.new_items} new this run"
        + (f"; {len(errors)} feed(s) failed (see above)" if errors else ""),
        file=sys.stderr,
    )
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
    nw = with_config(
        rs.add_parser("news", help="read the mobile-games industry news feeds and list them")
    )
    nw.add_argument("--days", type=int, default=7, help="list items of the last N days (7)")
    nw.add_argument("--json", action="store_true", help="print the items as JSON")
    nw.add_argument("--no-llm", action="store_true", help="keyword tags only, no LLM call")
    nw.add_argument("--no-fetch", action="store_true", help="list stored items, send nothing")
    nw.set_defaults(func=cmd_radar_news)
    show = with_config(rs.add_parser("show", help="print the newest digest"))
    show.add_argument("--lang", choices=["tr", "en"], default="tr")
    show.add_argument("--date")
    show.set_defaults(func=cmd_radar_show)
    from .dossier.cli import add_parser as add_dossier_parser  # pulse radar dossier <id>

    add_dossier_parser(rs, with_config)
    from .adintel.cli import add_parser as add_adintel_parser  # pulse radar adintel refresh

    add_adintel_parser(rs, with_config)
