"""Zero-setup app registry: status new -> testing -> live, store checks, config merge.

The collector creates one registry row per (app_id, platform) on the first
event of an app it has not seen, and promotes it to `testing` as soon as a
release build or a TestFlight / sideloaded install sends data. `pulse apps
refresh` promotes `testing` to `live` when both hold:

1. release builds installed from a production store (Google Play, App Store,
   Amazon ...) reached `live_min_installs` distinct installs in the last 24 h
   (internal/closed Play tracks probably also report the Play installer, so
   this alone is not proof), and
2. the public store listing exists: iTunes Lookup `?bundleId=` returns one
   result (undocumented parameter: a signal, cached) / the Play details page
   answers 200 (404 when there is no public listing; details pages are allowed
   by Play's robots.txt, search is not).

Checks are polite: at most one request every 3 s (20/min), a result is reused
for 24 h, and a status never goes down automatically.

`registry_apps(cfg)` turns registry rows into AppConfig entries for apps that
pulse.yaml does not list, so the runner, dashboard and checks show a new game
without a config edit.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contract import spec
from .store import connect_db

ORDER = {"new": 0, "testing": 1, "live": 2}
CHECK_TTL_MS = 24 * 3_600_000
MIN_INTERVAL_S = 3.0
ITUNES = "https://itunes.apple.com/lookup"
PLAY = "https://play.google.com/store/apps/details"


def decide(
    status: str,
    *,
    seen_release: bool,
    seen_test_store: bool,
    prod_installs_24h: int,
    min_installs: int,
    store_listed: bool | None,
) -> str:
    """Next status; never lower than the current one."""
    want = "new"
    if seen_release or seen_test_store:
        want = "testing"
    if prod_installs_24h >= min_installs and store_listed:
        want = "live"
    return want if ORDER[want] > ORDER.get(status, 0) else status


@dataclass
class Listing:
    listed: bool | None  # None: could not tell (network error, unexpected status)
    version: str | None = None
    url: str | None = None
    name: str | None = None
    note: str = ""


class StoreChecker:
    """Public store listing lookups through a requests-like session (replayable)."""

    def __init__(
        self,
        session: Any = None,
        *,
        country: str = "us",
        min_interval_s: float = MIN_INTERVAL_S,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if session is None:
            import requests

            from .. import __version__

            session = requests.Session()
            session.headers["User-Agent"] = f"studio-pulse/{__version__} (store listing check)"
        self.session = session
        self.country = country
        self.min_interval_s = min_interval_s
        self.sleep, self.clock = sleep, clock
        self._last = -1e9
        self.requests = 0

    def _get(self, url: str, params: dict[str, str]) -> Any:
        wait = self._last + self.min_interval_s - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last = self.clock()
        self.requests += 1
        return self.session.get(url, params=params, timeout=20)

    def check(self, app_id: str, platform: str) -> Listing:
        try:
            if platform == "ios":
                r = self._get(ITUNES, {"bundleId": app_id, "country": self.country})
                if r.status_code != 200:
                    return Listing(None, note=f"itunes http {r.status_code}")
                data = json.loads(r.content)
                n = int(data.get("resultCount") or 0)
                if n < 1:
                    return Listing(False, note="itunes lookup: 0 results")
                res = data["results"][0]
                return Listing(
                    True, res.get("version"), res.get("trackViewUrl"), res.get("trackName"),
                    "itunes lookup: 1 result",
                )  # fmt: skip
            if platform == "android":
                r = self._get(PLAY, {"id": app_id, "hl": "en"})
                if r.status_code == 404:
                    return Listing(False, note="play details: 404")
                if r.status_code != 200:
                    return Listing(None, note=f"play details http {r.status_code}")
                m = re.search(r"<title[^>]*>([^<]*)</title>", r.text or "")
                name = m.group(1).replace(" - Apps on Google Play", "").strip() if m else None
                return Listing(True, None, f"{PLAY}?id={app_id}", name, "play details: 200")
        except Exception as exc:  # network trouble is "unknown", never "not listed"
            return Listing(None, note=f"{type(exc).__name__}")
        return Listing(None, note=f"no public store for platform {platform}")


def refresh(
    data_dir: Path,
    *,
    min_installs: int = 20,
    checker: StoreChecker | None = None,
    store_check: bool = True,
    now_ms: int | None = None,
    log: Callable[[str], Any] = print,
) -> list[dict[str, Any]]:
    """Apply the status machine to every registry row; returns the rows after the update."""
    db = Path(data_dir) / "collector.sqlite3"
    if not db.exists():
        log(f"no registry at {db} (has the collector received anything?)")
        return []
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    con = connect_db(db)
    try:
        rows = [
            dict(r) for r in con.execute("SELECT * FROM app_registry ORDER BY app_id, platform")
        ]
        for r in rows:
            n = con.execute(
                "SELECT COUNT(*) FROM registry_prod_installs WHERE app_id = ? AND platform = ? "
                "AND last_seen_ms >= ?",
                (r["app_id"], r["platform"], now - 24 * 3_600_000),
            ).fetchone()[0]
            r["prod_installs_24h"] = n
            listed = None if r["store_listed"] is None else bool(r["store_listed"])
            fresh = r["store_checked_ms"] and now - r["store_checked_ms"] < CHECK_TTL_MS
            if (
                store_check
                and r["status"] != "live"
                and n >= min_installs
                and not fresh
                and r["platform"] in ("ios", "android")
            ):
                checker = checker or StoreChecker()
                res = checker.check(r["app_id"], r["platform"])
                listed = res.listed
                con.execute(
                    "UPDATE app_registry SET store_listed = ?, store_version = ?, store_url = ?, "
                    "store_name = ?, store_checked_ms = ?, store_note = ?, "
                    "display_name = CASE WHEN display_name = app_id AND ? IS NOT NULL THEN ? "
                    "ELSE display_name END WHERE app_id = ? AND platform = ?",
                    (
                        None if res.listed is None else int(res.listed),
                        res.version,
                        res.url,
                        res.name,
                        now,
                        res.note,
                        res.name,
                        res.name,
                        r["app_id"],
                        r["platform"],
                    ),  # fmt: skip
                )
                log(f"  store check {r['app_id']} ({r['platform']}): {res.note}")
            new = decide(
                r["status"],
                seen_release=bool(r["seen_release"]),
                seen_test_store=bool(r["seen_test_store"]),
                prod_installs_24h=n,
                min_installs=min_installs,
                store_listed=listed,
            )
            if new != r["status"]:
                con.execute(
                    "UPDATE app_registry SET status = ?, status_changed_ms = ? "
                    "WHERE app_id = ? AND platform = ?",
                    (new, now, r["app_id"], r["platform"]),
                )
                log(f"  {r['app_id']} ({r['platform']}): {r['status']} -> {new}")
        if any(x["status"] == "live" for x in rows):  # live apps no longer need install rows
            con.execute(
                "DELETE FROM registry_prod_installs WHERE (app_id, platform) IN "
                "(SELECT app_id, platform FROM app_registry WHERE status = 'live')"
            )
        out = [dict(r) for r in con.execute("SELECT * FROM app_registry ORDER BY app_id, platform")]
        counts = {(r["app_id"], r["platform"]): r["prod_installs_24h"] for r in rows}
        for r in out:
            r["prod_installs_24h"] = counts.get((r["app_id"], r["platform"]), 0)
        return out
    finally:
        con.close()


# -- config merge ---------------------------------------------------------------------


def slug(app_id: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", app_id.lower()).strip("_")
    return s or "app"


def read_registry(data_dir: Path) -> list[dict[str, Any]]:
    db = Path(data_dir) / "collector.sqlite3"
    if not db.exists():
        return []
    try:
        con = connect_db(db)
    except sqlite3.Error:
        return []
    try:
        return [
            dict(r) for r in con.execute("SELECT * FROM app_registry ORDER BY app_id, platform")
        ]
    finally:
        con.close()


def registry_apps(cfg: Any) -> list[Any]:
    """AppConfig entries for registry apps that no config app claims (pulse_sdk.bundle_ids)."""
    from ..config import AppConfig
    from .settings import from_config

    block = (cfg.raw or {}).get("collector") or {}
    if block.get("auto_apps", True) is False:
        return []
    settings = from_config(cfg)
    if settings.mode != "local":
        return []
    min_status = str(block.get("dashboard_min_status", "new"))
    claimed: set[str] = set()
    for a in cfg.apps:
        for key, src in a.sources.items():
            if key.split("@", 1)[0] == "pulse_sdk":
                claimed |= set((src or {}).get("bundle_ids") or [])
    taken = {a.id for a in cfg.apps}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in read_registry(settings.data_dir):
        if r["app_id"] in claimed or ORDER.get(r["status"], 0) < ORDER.get(min_status, 0):
            continue
        groups.setdefault(r["app_id"], []).append(r)
    known = set(spec()["enums"]["platform"])
    out = []
    for bundle, rows in sorted(groups.items()):
        aid = slug(bundle)
        while aid in taken:
            aid += "_sdk"
        taken.add(aid)
        name = next((r["display_name"] for r in rows if r["display_name"] != bundle), bundle)
        genre = "game" if any(r["genre"] == "game" for r in rows) else "app"
        out.append(
            AppConfig(
                id=aid,
                name=name,
                genre=genre,
                platforms=sorted({r["platform"].upper() for r in rows if r["platform"] in known}),
                sources={"pulse_sdk": {"bundle_ids": [bundle], "auto": True}},
                timezone="UTC",
            )
        )
    return out
