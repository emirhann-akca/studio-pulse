"""In-memory sliding window for "who is playing right now".

An install is active when one of its accepted events has a (skew-corrected)
time in the last `window_ms` (5 minutes). Old events a device flushes from
its offline queue do not make it active. Counts are kept per app with a
breakdown by platform x store x country x build_env, and events per minute for
a sparkline. Nothing here is stored: a restarted collector refills within one
window. Cloud Run runs one instance (`--max-instances=1`) so there is one view.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

FUTURE_SLACK_MS = 120_000  # a device clock slightly ahead still counts as "now"


class LiveWindow:
    def __init__(self, window_ms: int = 300_000, spark_minutes: int = 30) -> None:
        self.window_ms = window_ms
        self.spark_minutes = spark_minutes
        self._lock = threading.Lock()
        # app_id -> install_id -> (last_ms, platform, store, country, build_env)
        self._installs: dict[str, dict[str, tuple[int, str, str, str, str]]] = defaultdict(dict)
        # app_id -> minute (epoch // 60000) -> events
        self._minutes: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    def observe(
        self,
        app_id: str,
        install_id: str,
        *,
        platform: str,
        store: str,
        country: str,
        build_env: str,
        ts_list: list[int],
        now_ms: int,
    ) -> None:
        recent = [t for t in ts_list if now_ms - self.window_ms <= t <= now_ms + FUTURE_SLACK_MS]
        spark_from = now_ms - self.spark_minutes * 60_000
        with self._lock:
            mins = self._minutes[app_id]
            for t in ts_list:
                if spark_from <= t <= now_ms + FUTURE_SLACK_MS:
                    mins[min(t, now_ms) // 60_000] += 1
            if recent:
                last = min(max(recent), now_ms)
                prev = self._installs[app_id].get(install_id)
                if prev is None or prev[0] <= last:
                    self._installs[app_id][install_id] = (last, platform, store, country, build_env)

    def forget(self, install_id: str) -> None:
        with self._lock:
            for inst in self._installs.values():
                inst.pop(install_id, None)

    def _prune(self, now_ms: int) -> None:
        cut = now_ms - self.window_ms
        first_min = (now_ms - self.spark_minutes * 60_000) // 60_000
        for app_id in list(self._installs):
            inst = self._installs[app_id]
            for k in [k for k, v in inst.items() if v[0] < cut]:
                del inst[k]
        for app_id in list(self._minutes):
            mins = self._minutes[app_id]
            for m in [m for m in mins if m < first_min]:
                del mins[m]

    def snapshot(self, now_ms: int) -> dict[str, dict[str, Any]]:
        """{app_id: {active, by: [...], events_per_min: [...]}} for apps with recent data."""
        with self._lock:
            self._prune(now_ms)
            out: dict[str, dict[str, Any]] = {}
            now_min = now_ms // 60_000
            for app_id in set(self._installs) | set(self._minutes):
                inst = self._installs.get(app_id, {})
                by: dict[tuple[str, str, str, str], int] = defaultdict(int)
                platforms: dict[str, int] = defaultdict(int)
                for _, platform, store, country, env in inst.values():
                    by[(platform, store, country, env)] += 1
                    platforms[platform] += 1
                mins = self._minutes.get(app_id, {})
                spark = [mins.get(now_min - i, 0) for i in range(self.spark_minutes - 1, -1, -1)]
                out[app_id] = {
                    "active": len(inst),
                    "platforms": dict(sorted(platforms.items())),
                    "by": [
                        {"platform": p, "store": s, "country": c, "build_env": e, "active": n}
                        for (p, s, c, e), n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
                    ],
                    "events_per_min": spark,
                    "events_last_min": spark[-1] if spark else 0,
                }
            return out
