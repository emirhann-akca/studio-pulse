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
import json
import shutil
from pathlib import Path
from typing import Any

SDK_DAYS = 42
LIVE_SNAPSHOT = "live_snapshot.json"


class _SnapshotClock:
    """SimClock that calls take() once, just before the simulation passes `at`."""

    def __init__(self, ms: int, at: int) -> None:
        self._ms = ms
        self.at = at
        self.take: Any = None
        self.snapshot: dict[str, Any] | None = None

    def __call__(self) -> int:
        return self._ms

    @property
    def ms(self) -> int:
        return self._ms

    @ms.setter
    def ms(self, value: int) -> None:
        if self.snapshot is None and self.take is not None and self._ms < self.at <= value:
            self._ms = self.at
            self.snapshot = self.take()
        self._ms = value


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
    from .run import Poster, backfill

    data_dir = out_dir / "collector"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    start = end - dt.timedelta(days=days - 1)
    t0 = int(dt.datetime.combine(start, dt.time(), dt.UTC).timestamp() * 1000)
    # the static Studio's "playing now": the collector's live view at 20:00 UTC on the
    # last day, taken as the simulation passes it (at midnight nobody is playing)
    snap_at = int(dt.datetime.combine(end, dt.time(20), dt.UTC).timestamp() * 1000)
    clock = _SnapshotClock(t0, snap_at)
    settings = CollectorSettings(data_dir=data_dir, country_header="CF-IPCountry")
    with running_collector(settings, clock) as (url, col):
        clock.take = col.live_snapshot
        poster = Poster(url, DEFAULT_LOCAL_KEY, clock=clock)
        rep = backfill(poster, start=start, days=days, games=GAMES, scale=scale, seed=7, log=log)
        counters = col.counters
    if clock.snapshot is not None:
        (out_dir / LIVE_SNAPSHOT).write_text(json.dumps(clock.snapshot), encoding="utf-8")
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
