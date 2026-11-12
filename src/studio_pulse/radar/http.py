"""Polite fetching for the radar: robots.txt, spacing, daily budgets, a day cache and a ledger.

Every store request goes through `Fetcher.get`:
1. Hosts in `robots_hosts` (Google Play) have their robots.txt read first (once per run) and
   a URL it disallows is refused before anything is sent (RobotsDisallowed). Paths that Play's
   robots.txt forbids today (`/_/`, `/store/search`) are refused even if a robots.txt could not
   be read.
2. A response cached for the same snapshot day is returned without a request.
3. A host's daily budget (requests already recorded in raw_mr_request for that day plus this
   run) and the run's total cap are checked: over budget -> BudgetExhausted, nothing sent.
4. Requests to one host are spaced by at least `spacing_s` seconds (Play: 3 s).
5. 429/503: wait Retry-After (capped) and retry; repeated 429/403 stops that host for the rest
   of the run (HostStopped). No proxies, no rotating identities, no browser impersonation.
Every attempt is written to raw_mr_request.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
import requests

from .. import __version__
from .robots import Robots

USER_AGENT = f"studio-pulse-radar/{__version__} (low-volume daily market snapshot)"
ROBOTS_TOKEN = "studio-pulse-radar"
ALWAYS_REFUSED = {"play.google.com": ("/_/", "/store/search", "/store/getreviews")}


class BudgetExhausted(RuntimeError):
    pass


class HostStopped(RuntimeError):
    pass


class RobotsDisallowed(RuntimeError):
    pass


class NotFound(RuntimeError):
    pass


@dataclass
class Fetched:
    url: str
    status: int
    content: bytes
    cached: bool

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class Fetcher:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        snapshot_date: dt.date,
        *,
        session: requests.Session | None = None,
        run_id: str = "",
        budgets: dict[str, int] | None = None,
        spacing: dict[str, float] | None = None,
        default_spacing_s: float = 3.0,
        max_total: int | None = None,
        cache_dir: Path | None = None,
        robots_hosts: tuple[str, ...] = ("play.google.com",),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 30.0,
        retries: int = 2,
        log: Callable[[str], None] = print,
    ) -> None:
        self.con = con
        self.day = snapshot_date
        self.session = session or requests.Session()
        self.run_id = run_id
        self.budgets = budgets or {}
        self.spacing = spacing or {}
        self.default_spacing_s = default_spacing_s
        self.max_total = max_total
        self.cache_dir = Path(cache_dir) / snapshot_date.isoformat() if cache_dir else None
        self.robots_hosts = robots_hosts
        self.clock = clock
        self.sleep = sleep
        self.timeout_s = timeout_s
        self.retries = retries
        self.log = log
        self.sent = 0
        self.sent_by_host: dict[str, int] = {}
        self.cache_hits = 0
        self._last: dict[str, float] = {}
        self._robots: dict[str, Robots] = {}
        self._stopped: dict[str, str] = {}

    # -- bookkeeping -----------------------------------------------------------------
    def used_today(self, host: str) -> int:
        row = self.con.execute(
            "SELECT COUNT(*) FROM raw_mr_request WHERE snapshot_date = ? AND host = ? "
            "AND NOT cached AND run_id <> ?",
            [self.day, host, self.run_id],
        ).fetchone()
        return int(row[0]) + self.sent_by_host.get(host, 0)

    def _record(
        self,
        url: str,
        purpose: str,
        status: int | None,
        size: int,
        cached: bool,
        ms: float,
        error: str | None = None,
    ) -> None:
        parts = urlsplit(url)
        self.con.execute(
            "INSERT INTO raw_mr_request VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                self.run_id,
                dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0),
                self.day,
                parts.hostname,
                parts.path + (f"?{parts.query}" if parts.query else ""),
                purpose,
                status,
                size,
                cached,
                round(ms, 1),
                error,
            ],
        )

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ".bin")

    def _space(self, host: str) -> None:
        gap = self.spacing.get(host, self.default_spacing_s)
        last = self._last.get(host)
        if last is not None:
            wait = gap - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.clock()

    # -- robots ------------------------------------------------------------------------
    def robots(self, host: str) -> Robots:
        if host not in self._robots:
            url = f"https://{host}/robots.txt"
            res = self._send(url, "robots", cacheable=True)
            if res.status != 200:
                raise RobotsDisallowed(f"robots.txt of {host} answered {res.status}: not crawling")
            self._robots[host] = Robots.parse(res.text)
        return self._robots[host]

    def check_allowed(self, url: str) -> None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        for prefix in ALWAYS_REFUSED.get(host, ()):
            if path.startswith(prefix):
                raise RobotsDisallowed(f"{host}{prefix} is disallowed by robots.txt; refused")
        if host in self.robots_hosts and parts.path != "/robots.txt":
            if not self.robots(host).allowed(path, ROBOTS_TOKEN):
                raise RobotsDisallowed(f"robots.txt of {host} disallows {parts.path}; refused")

    # -- requests ------------------------------------------------------------------------
    def get(self, url: str, purpose: str, *, cacheable: bool = True) -> Fetched:
        self.check_allowed(url)
        return self._send(url, purpose, cacheable=cacheable)

    def _send(self, url: str, purpose: str, *, cacheable: bool) -> Fetched:
        host = urlsplit(url).hostname or ""
        cp = self._cache_path(url) if cacheable else None
        if cp is not None and cp.exists():
            self.cache_hits += 1
            data = cp.read_bytes()
            self._record(url, purpose, 200, len(data), True, 0.0)
            return Fetched(url, 200, data, True)
        if host in self._stopped:
            raise HostStopped(self._stopped[host])
        attempt = 0
        while True:
            budget = self.budgets.get(host)
            if budget is not None and self.used_today(host) >= budget:
                raise BudgetExhausted(f"daily budget of {budget} requests to {host} is used up")
            if self.max_total is not None and self.sent >= self.max_total:
                raise BudgetExhausted(f"run cap of {self.max_total} requests reached")
            self._space(host)
            attempt += 1
            t0 = time.perf_counter()
            self.sent += 1
            self.sent_by_host[host] = self.sent_by_host.get(host, 0) + 1
            try:
                r = self.session.get(
                    url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout_s
                )
            except requests.RequestException as exc:
                ms = (time.perf_counter() - t0) * 1000
                self._record(url, purpose, None, 0, False, ms, f"{type(exc).__name__}")
                if attempt > self.retries:
                    raise
                self.sleep(min(5.0 * attempt, 30.0))
                continue
            ms = (time.perf_counter() - t0) * 1000
            self._record(url, purpose, r.status_code, len(r.content), False, ms)
            if r.status_code in (429, 503) and attempt <= self.retries:
                ra = r.headers.get("Retry-After", "")
                wait = float(ra) if ra.isdigit() else 10.0 * attempt
                self.sleep(min(wait, 60.0))
                continue
            if r.status_code in (403, 429, 503):
                why = f"{host} answered {r.status_code}; no more requests to it in this run"
                self._stopped[host] = why
                raise HostStopped(why)
            if r.status_code == 200 and cp is not None:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_bytes(r.content)
            return Fetched(url, r.status_code, r.content, False)

    def summary(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "by_host": dict(sorted(self.sent_by_host.items())),
            "cache_hits": self.cache_hits,
            "stopped": dict(self._stopped),
        }


def prune_cache(cache_dir: Path, keep_days: int, today: dt.date) -> int:
    """Delete day folders older than keep_days (store payloads are not kept long)."""
    import shutil

    n = 0
    if not cache_dir.is_dir():
        return 0
    for d in cache_dir.iterdir():
        try:
            day = dt.date.fromisoformat(d.name)
        except ValueError:
            continue
        if (today - day).days > keep_days:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
