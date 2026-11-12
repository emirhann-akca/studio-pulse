"""`pulse demo` part 2: three SDK games through a real collector, with the demo's clock.

The collector runs in this process on a free 127.0.0.1 port; the simulator
posts every batch over HTTP. Its clock follows the simulation (each request is
"received" 40 ms after the batch was sent), so the demo stays reproducible on
every OS. Pebble Pop is declared in pulse.yaml with its bundle id; Tidy Habit
and Lantern Leap are not: they register themselves on their first event and
reach the dashboard through the registry. Lantern Leap starts 12 days before
the end, on developer builds for its first two days.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

SDK_DAYS = 42


def add_sdk_games(
    cfg: dict[str, Any],
    out_dir: Path,
    *,
    end: dt.date,
    scale: float = 1.0,
    days: int = SDK_DAYS,
    log: Any = print,
) -> dict[str, Any]:
    from ..collector.inprocess import running_collector
    from ..collector.registry import refresh
    from ..collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings
    from .model import GAMES
    from .run import Poster, SimClock, backfill

    data_dir = out_dir / "collector"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    start = end - dt.timedelta(days=days - 1)
    t0 = int(dt.datetime.combine(start, dt.time(), dt.UTC).timestamp() * 1000)
    clock = SimClock(t0)
    settings = CollectorSettings(data_dir=data_dir, country_header="CF-IPCountry")
    with running_collector(settings, clock) as (url, col):
        poster = Poster(url, DEFAULT_LOCAL_KEY, clock=clock)
        rep = backfill(poster, start=start, days=days, games=GAMES, scale=scale, seed=7, log=log)
        counters = col.counters
    end_ms = int(
        dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), dt.UTC).timestamp() * 1000
    )
    refresh(data_dir, min_installs=20, store_check=False, now_ms=end_ms, log=lambda _m: None)
    cfg["collector"] = {"data_dir": "collector", "dashboard_min_status": "new"}
    cfg["apps"].append(
        {
            "id": "pebble_pop",
            "name": "Pebble Pop",
            "genre": "puzzle",
            "platforms": ["ANDROID", "IOS"],
            "sources": {"pulse_sdk": {"bundle_ids": ["demo.pebblepop"]}},
        }
    )
    log(
        f"  generated 3 SDK games through the collector: {rep.sent.batches:,} batches, "
        f"{counters.accepted:,} events accepted, {counters.duplicates} duplicates, "
        f"{counters.quarantined} quarantined"
    )
    return {"report": rep, "accepted": counters.accepted}
