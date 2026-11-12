"""`pulse simulate`: virtual players posting v2 batches to a real collector over HTTP.

Two modes, usually run one after the other:

- backfill: N days of history in seconds. Each session's events go out in one
  batch when the session ends (an SDK flushing its offline queue). Against a
  wall-clock collector the batches are sent "now" with their old event times,
  and the collector's skew correction puts every event back on its day. With a
  `SimClock` (the demo) the collector's clock follows the simulation instead,
  which makes the demo reproducible to the byte.
- live: players in real time with the SDK's flush rules (20 events / 30 s /
  background). A game the collector has never seen starts sending part-way
  through and must register itself. At the end, `verify_live` compares the
  collector's /live with what the simulator itself sent.

Realism knobs: 0.4% of batches are lost (seq gaps), 1% are sent twice (a
retry after a timeout: dedupe), 0.1% carry one invalid event (quarantine), 3%
of devices have a wrong clock (skew correction), install stores, platforms,
countries (CF-IPCountry header) and device tiers are mixed.
"""

from __future__ import annotations

import datetime as dt
import gzip
import heapq
import json
import math
import random
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from .model import COUNTRIES, DAY, GAMES, MIN, Device, Game, Player, Sent, _pick, strip_private

ANDROID_STORES = [("google_play", 0.88), ("samsung", 0.05), ("huawei", 0.03), ("sideload", 0.04)]
IOS_STORES = [("app_store", 0.95), ("testflight", 0.05)]


class SimClock:
    """A settable clock in ms, shared by the simulator and an in-process collector."""

    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


class Poster:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        clock: SimClock | None = None,
        country_header: str = "CF-IPCountry",
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.clock = clock
        self.country_header = country_header
        self.session = requests.Session()
        self.session.headers.update({"X-Pulse-Key": key, "Content-Type": "application/json"})
        self.timeout = timeout
        self.responses: dict[int, int] = defaultdict(int)
        self.accepted = self.duplicates = self.quarantined = 0

    def post(self, batch: dict[str, Any], country: str, send_true_ms: int) -> dict[str, Any]:
        body = json.dumps(strip_private(batch), separators=(",", ":")).encode("utf-8")
        headers = {self.country_header: country}
        if len(body) > 1024:  # the SDK gzips bodies over 1 KB
            body = gzip.compress(body, mtime=0)
            headers["Content-Encoding"] = "gzip"
        if self.clock is not None:
            self.clock.ms = send_true_ms + 40  # transit time
        for attempt in range(5):
            r = self.session.post(
                self.url + "/v2/batch", data=body, headers=headers, timeout=self.timeout
            )
            self.responses[r.status_code] += 1
            if r.status_code == 200:
                out = r.json()
                self.accepted += out["accepted"]
                self.duplicates += out["duplicates"]
                self.quarantined += out["quarantined"]
                return out
            if r.status_code in (429, 503):
                time.sleep(0.2 * (attempt + 1))
                continue
            raise RuntimeError(f"collector answered {r.status_code}: {r.text[:300]}")
        raise RuntimeError("collector kept answering 429/503")


@dataclass
class Report:
    sent: Sent = field(default_factory=Sent)
    installs: dict[str, int] = field(default_factory=dict)
    by_app_events: dict[str, int] = field(default_factory=dict)
    last_seen: dict[str, tuple[str, int, tuple[str, str, str, str]]] = field(default_factory=dict)
    first_post: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": vars(self.sent),
            "installs": self.installs,
            "events_by_app": self.by_app_events,
        }


def _install_profile(game: Game, rng: random.Random, dev_phase: bool) -> dict[str, Any]:
    platform = _pick(rng, list(game.platforms.items()))[0]
    if dev_phase:
        store, env = "unknown", "debug"
    else:
        store = _pick(rng, ANDROID_STORES if platform == "android" else IOS_STORES)[0]
        env = "debug" if rng.random() < 0.01 else "release"
    return {
        "platform": platform,
        "store": store,
        "build_env": env,
        "country": _pick(rng, COUNTRIES),
    }


def _mangle(batch: dict[str, Any], rng: random.Random) -> None:
    """One invalid event: a level_fail without its required reason, or a custom event with an e-mail."""
    ev = rng.choice(batch["events"])
    if ev["name"] == "level_fail":
        ev["p"].pop("reason", None)
    else:
        ev["p"] = dict(ev["p"], note="contact me@example.com")


class _Sender:
    """Applies loss, duplicates and invalid events, posts, and records what was sent."""

    def __init__(self, poster: Poster, report: Report, rng: random.Random) -> None:
        self.poster, self.report, self.rng = poster, report, rng

    def send(self, dev: Device, batch: dict[str, Any], send_true_ms: int) -> None:
        r, s = self.rng, self.report.sent
        n = len(batch["events"])
        if r.random() < 0.004:
            s.lost_batches += 1
            s.lost_events += n
            return
        invalid = r.random() < 0.001
        if invalid:
            _mangle(batch, r)
            s.invalid += 1
        self.poster.post(batch, dev.country, send_true_ms)
        s.batches += 1
        s.events += n
        app = dev.game.app_id
        self.report.first_post.setdefault(app, send_true_ms)
        self.report.by_app_events[app] = self.report.by_app_events.get(app, 0) + n - int(invalid)
        last = max(e["_true_ts"] for e in batch["events"])
        prev = self.report.last_seen.get(dev.install_id)
        if prev is None or prev[1] < last:
            key = (dev.platform, dev.store, dev.country, dev.build_env)
            self.report.last_seen[dev.install_id] = (app, last, key)
        if r.random() < 0.01:
            self.poster.post(batch, dev.country, send_true_ms + 5000)
            s.resent_batches += 1
            s.resent_events += n


# -- backfill ---------------------------------------------------------------------------


def backfill(
    poster: Poster,
    *,
    start: dt.date,
    days: int,
    games: list[Game] | None = None,
    scale: float = 1.0,
    seed: int = 7,
    stop_ms: int | None = None,
    send_now: Callable[[], int] | None = None,
    log: Callable[[str], Any] = print,
) -> Report:
    """Generate `days` days of play from `start` (UTC) and post every session's batch.

    send_now: with a wall-clock collector, the time batches are really sent
    (device clocks are applied to it); None = send at the simulated time.
    """
    games = games or GAMES
    report = Report()
    sender = _Sender(poster, report, random.Random(seed + 1))
    t_start = int(dt.datetime.combine(start, dt.time(), dt.UTC).timestamp() * 1000)
    stop_ms = stop_ms if stop_ms is not None else t_start + days * DAY
    queue: list[tuple[int, int, int, Device, dict[str, Any]]] = []
    n = 0
    for gi, game in enumerate(games):
        grng = random.Random(seed * 1000 + game.seed)
        first_day = game.start_day if game.start_day >= 0 else days + game.start_day
        players: list[tuple[int, Player]] = []
        for d in range(max(0, first_day), days):
            day0 = t_start + d * DAY
            dev_phase = d < first_day + game.dev_days
            weekend = dt.date.fromordinal(start.toordinal() + d).weekday() >= 5
            lam = (2 if dev_phase else game.installs_per_day * scale) * (1.15 if weekend else 1.0)
            count = max(0, int(round(grng.gauss(lam, math.sqrt(max(lam, 1)) * 0.6))))
            for _ in range(count):
                prof = _install_profile(game, grng, dev_phase)
                dev = Device(game, random.Random(grng.getrandbits(64)), day0, **prof)
                players.append((d, Player(game, random.Random(grng.getrandbits(64)), dev)))
            for install_day, pl in players:
                day_n = d - install_day
                if not pl.active_on(day_n):
                    if day_n > 0 and pl.rng.random() < 0.02:
                        pl.churned = True
                    continue
                k = pl.sessions_today(day_n)
                starts = sorted(pl.rng.uniform(0.25, 0.97) for _ in range(k))
                version = game.version_on(d - max(0, first_day))
                last_end = 0
                for frac in starts:
                    t0 = max(day0 + int(frac * DAY), last_end + 5 * MIN)
                    if t0 >= stop_ms - 30 * MIN or t0 >= day0 + DAY:
                        break
                    if install_day == d and pl.dev.snum == 0:
                        pl.dev.install_ms = t0
                    end = pl.session(t0, version)
                    last_end = end
                    delay = int(pl.rng.uniform(1, 30) * 1000)
                    if pl.rng.random() < 0.05:  # offline: the batch leaves hours later
                        delay = int(pl.rng.uniform(2, 20) * 3_600_000)
                    send = min(end + delay, stop_ms - 1000)
                    batch = pl.dev.take_batch(send)
                    if batch:
                        n += 1
                        heapq.heappush(queue, (send, gi, n, pl.dev, batch))
        report.installs[game.app_id] = len(players)
    log(f"  simulate backfill: {len(queue)} batch(es) for {len(games)} game(s), {days} day(s)")
    while queue:
        send, _, _, dev, batch = heapq.heappop(queue)
        if send_now is not None:
            now = send_now()
            batch["sent_at"] = now + dev.clock_off
            send = now
        sender.send(dev, batch, send)
    return report


# -- live -------------------------------------------------------------------------------


@dataclass
class _LivePlayer:
    player: Player
    next_session_ms: int
    outbox: list[dict[str, Any]] = field(default_factory=list)
    last_flush_ms: int = 0
    gone: bool = False


def live(
    poster: Poster,
    *,
    duration_s: float,
    players: int = 30,
    games: list[Game] | None = None,
    new_game_after_s: float | None = None,
    seed: int = 11,
    pace: float = 0.25,
    first_within_s: float = 20.0,
    clock: Callable[[], int] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    log: Callable[[str], Any] = print,
) -> Report:
    """Real-time players for `duration_s` seconds; the last game starts at `new_game_after_s`."""
    games = games or GAMES
    clock = clock or (lambda: time.time_ns() // 1_000_000)
    report = Report()
    rng = random.Random(seed)
    sender = _Sender(poster, report, random.Random(seed + 1))
    t0 = clock()
    end = t0 + int(duration_s * 1000)
    late_game = games[-1] if new_game_after_s is not None and len(games) > 1 else None
    start_of = {g.app_id: t0 for g in games}
    if late_game:
        start_of[late_game.app_id] = t0 + int(new_game_after_s * 1000)
    pool: list[_LivePlayer] = []
    for g in games:
        k = max(2, round(players * g.installs_per_day / sum(x.installs_per_day for x in games)))
        for _ in range(k):
            grng = random.Random(rng.getrandbits(64))
            # the game that starts mid-run is still on developer builds: registry status "new"
            prof = _install_profile(g, grng, g is late_game)
            dev = Device(g, random.Random(grng.getrandbits(64)), start_of[g.app_id], **prof)
            first = start_of[g.app_id] + int(grng.uniform(0, first_within_s * 1000))
            pool.append(_LivePlayer(Player(g, grng, dev), first))
        report.installs[g.app_id] = k
    if late_game:
        log(
            f"  simulate live: {late_game.name} ({late_game.app_id}) starts after {new_game_after_s:.0f} s"
        )
    log(f"  simulate live: {len(pool)} players for {duration_s:.0f} s")

    def flush(lp: _LivePlayer, now: int) -> None:
        if not lp.outbox:
            return
        dev = lp.player.dev
        batch = {"v": 2, "schema": "2.0", "sent_at": now + dev.clock_off, "ctx": dev.ctx(),
                 "events": lp.outbox}  # fmt: skip
        lp.outbox = []
        lp.last_flush_ms = now
        sender.send(dev, batch, now)

    while True:
        now = clock()
        if now >= end:
            break
        for lp in pool:
            if lp.gone:
                continue
            dev = lp.player.dev
            if not dev.queue and now >= lp.next_session_ms:
                if lp.player.rng.random() < 0.12 and dev.snum > 0:
                    lp.gone = True  # this player is done for today
                    continue
                version = lp.player.game.version
                session_end = lp.player.session(now, version, step_ms=pace)
                lp.next_session_ms = session_end + int(lp.player.rng.uniform(20, 90) * 1000)
            due = [e for e in dev.queue if e["_true_ts"] <= now]
            if due:
                dev.queue = [e for e in dev.queue if e["_true_ts"] > now]
                lp.outbox.extend(due)
            background = any(e["name"] == "pulse_background" for e in lp.outbox)
            if lp.outbox and (
                len(lp.outbox) >= 20 or background or now - lp.last_flush_ms >= 30_000
            ):
                flush(lp, now)
        sleep(0.25)
    now = clock()
    for lp in pool:  # what is due goes out; the rest of a session is never played
        dev = lp.player.dev
        lp.outbox.extend(e for e in dev.queue if e["_true_ts"] <= now)
        dev.queue = []
        flush(lp, now)
    return report


# -- verification -----------------------------------------------------------------------


def expected_live(
    report: Report, now_ms: int, window_ms: int = 300_000, slack_ms: int = 2_000
) -> dict[str, Any]:
    """Active installs per app (and per platform/store/country/build_env) the collector should show.

    Installs whose last event is within `slack_ms` of the window edge are
    ambiguous (their transit time decides), so they are reported separately.
    """
    sure: dict[str, int] = defaultdict(int)
    maybe: dict[str, int] = defaultdict(int)
    by: dict[str, dict[tuple[str, str, str, str], int]] = defaultdict(lambda: defaultdict(int))
    edge = now_ms - window_ms
    for _, (app, last, key) in report.last_seen.items():
        if last >= edge + slack_ms:
            sure[app] += 1
            by[app][key] += 1
        elif last >= edge - slack_ms:
            maybe[app] += 1
    return {"sure": dict(sure), "maybe": dict(maybe), "by": {a: dict(v) for a, v in by.items()}}


def verify_live(url: str, report: Report, *, read_key: str | None = None) -> dict[str, Any]:
    """GET /live and compare it with what this simulator sent. Returns a result dict."""
    headers = {"X-Pulse-Read-Key": read_key} if read_key else {}
    snap = requests.get(url.rstrip("/") + "/live", headers=headers, timeout=30).json()
    exp = expected_live(report, snap["now_ms"], snap["window_s"] * 1000)
    rows = []
    ok = True
    for app in sorted(set(exp["sure"]) | set(exp["maybe"]) | {a["app_id"] for a in snap["apps"]}):
        got = next((a for a in snap["apps"] if a["app_id"] == app), None)
        active = got["active"] if got else 0
        lo = exp["sure"].get(app, 0)
        hi = lo + exp["maybe"].get(app, 0)
        match = lo <= active <= hi
        got_by = {
            (b["platform"], b["store"], b["country"], b["build_env"]): b["active"]
            for b in (got or {}).get("by", [])
        }
        by_match = got_by == exp["by"].get(app, {}) if not exp["maybe"].get(app) else True
        ok &= match and by_match
        rows.append({"app_id": app, "live": active, "expected": lo if lo == hi else [lo, hi],
                     "breakdown_match": by_match, "match": match})  # fmt: skip
    return {"ok": ok, "now_ms": snap["now_ms"], "apps": rows, "new_apps": snap.get("new_apps", []),
            "collector": snap.get("collector", {})}  # fmt: skip
