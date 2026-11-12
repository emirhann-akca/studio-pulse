"""`pulse simulate`: virtual players for the fictional games against a running collector."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..collector.settings import DEFAULT_LOCAL_KEY, DEFAULT_PORT


def cmd_simulate(args: argparse.Namespace) -> int:
    from ..config import ConfigError, load_config
    from .model import GAMES
    from .run import Poster, backfill, live, verify_live

    url = args.url
    if not url:
        try:
            url = ((load_config(args.config).raw or {}).get("collector") or {}).get("url")
        except ConfigError:
            url = None
        url = url or f"http://127.0.0.1:{DEFAULT_PORT}"
    poster = Poster(url, args.key, country_header=args.country_header)
    games = GAMES
    t0 = time.monotonic()
    reports = []
    new_game = args.new_game_after if args.duration else None
    if args.backfill_days:
        # the late game is brand new: it must not exist before the live run
        hist = games[:-1] if new_game is not None else games
        now = time.time_ns() // 1_000_000
        today = dt.datetime.fromtimestamp(now / 1000, dt.UTC).date()
        start = today - dt.timedelta(days=args.backfill_days - 1)
        rep = backfill(
            poster,
            start=start,
            days=args.backfill_days,
            games=hist,
            scale=args.scale,
            seed=args.seed,
            stop_ms=now,
            send_now=lambda: time.time_ns() // 1_000_000,
        )
        reports.append(("backfill", rep))
        print(
            f"backfill: {rep.sent.batches} batches, {rep.sent.events} events in {time.monotonic() - t0:.1f} s"
        )
    result: dict[str, Any] = {"url": url}
    if args.duration:
        rep = live(
            poster,
            duration_s=args.duration,
            players=args.players,
            games=games,
            new_game_after_s=new_game,
            seed=args.seed + 1,
            pace=args.pace,
            first_within_s=args.ramp,
        )
        reports.append(("live", rep))
        print(
            f"live: {rep.sent.batches} batches, {rep.sent.events} events, installs {rep.installs}"
        )
        if args.verify:
            time.sleep(1.0)
            v = verify_live(url, rep, read_key=args.read_key)
            result["verify"] = v
            for row in v["apps"]:
                print(
                    f"  {row['app_id']:<22} /live {row['live']:>4}  sent {row['expected']!s:>8}  "
                    f"{'ok' if row['match'] and row['breakdown_match'] else 'MISMATCH'}"
                )
            print(f"new apps on /live: {[a['app_id'] for a in v['new_apps']]}")
    sent = {
        k: sum(getattr(r.sent, k) for _, r in reports)
        for k in ("batches", "events", "lost_batches", "lost_events", "resent_batches", "resent_events", "invalid")
    }  # fmt: skip
    result.update(
        sent=sent,
        responses=dict(poster.responses),
        collector_accepted=poster.accepted,
        collector_duplicates=poster.duplicates,
        collector_quarantined=poster.quarantined,
        modes=[m for m, _ in reports],
        installs={m: r.installs for m, r in reports},
        events_by_app={m: r.by_app_events for m, r in reports},
    )
    # every event the simulator posted is accounted for: stored, quarantined or a duplicate
    posted = sent["events"] + sent["resent_events"]
    result["accounted"] = poster.accepted + poster.duplicates + poster.quarantined == posted
    result["expected_accepted"] = sent["events"] - sent["invalid"]
    result["accepted_matches"] = poster.accepted == result["expected_accepted"]
    print(
        f"sent {posted} events ({sent['resent_events']} re-sent, {sent['invalid']} invalid, "
        f"{sent['lost_events']} lost on the way); collector accepted {poster.accepted}, "
        f"duplicates {poster.duplicates}, quarantined {poster.quarantined}"
    )
    if args.report:
        Path(args.report).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    ok = result["accounted"] and result["accepted_matches"]
    if args.verify and args.duration:
        ok = ok and result["verify"]["ok"]
    if not ok:
        print("simulation check FAILED", file=sys.stderr)
    return 0 if ok else 1


def register(sub: Any, with_config: Any) -> None:
    s = with_config(
        sub.add_parser(
            "simulate",
            help="virtual players for 3 fictional games posting SDK batches to a collector",
        )
    )
    s.add_argument("--url", help="collector URL (default: collector.url or http://127.0.0.1:8787)")
    s.add_argument("--key", default=DEFAULT_LOCAL_KEY, help="public write key")
    s.add_argument("--backfill-days", type=int, default=0, help="first send N days of history")
    s.add_argument("--duration", type=float, default=0, help="then play live for N seconds")
    s.add_argument("--players", type=int, default=30, help="live players across the games")
    s.add_argument("--scale", type=float, default=0.5, help="backfill install volume multiplier")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument(
        "--pace", type=float, default=0.25, help="live session time compression (1 = real)"
    )
    s.add_argument("--ramp", type=float, default=20.0, help="live players start within N seconds")
    s.add_argument(
        "--new-game-after", type=float, default=30.0,
        help="seconds into the live run when the third game starts sending (default 30)",
    )  # fmt: skip
    s.add_argument("--country-header", default="CF-IPCountry", help="header carrying the country")
    s.add_argument(
        "--verify", action="store_true", help="compare /live with what was sent; exit 1 on mismatch"
    )
    s.add_argument("--read-key", help="the collector's read key for /live")
    s.add_argument("--report", help="write a JSON report here")
    s.set_defaults(func=cmd_simulate)
