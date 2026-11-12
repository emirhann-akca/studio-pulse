"""Fictional games and virtual players that behave like the Studio Pulse SDK.

Everything here is invented. A `Device` is one install of one game: it keeps
the SDK's state (random install_id, a seq counter, session ids and numbers,
foreground time), stamps events with UUIDv7 ids and its own clock (some
devices run minutes to hours off), and turns its queue into v2 batches. A
`Player` decides what happens in a session: tutorial, levels with difficulty
spikes and death positions, settings, currency, ads, purchases, errors.

All randomness comes from seeded `random.Random` objects, so a seed and a
start time reproduce the same batches byte for byte.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..contract import generated as C

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000

COUNTRIES = [  # code, weight, currency, locale
    ("TR", 0.22, "TRY", "tr-TR"), ("US", 0.18, "USD", "en-US"), ("BR", 0.12, "BRL", "pt-BR"),
    ("DE", 0.09, "EUR", "de-DE"), ("IN", 0.09, "INR", "en-IN"), ("GB", 0.07, "GBP", "en-GB"),
    ("ID", 0.08, "IDR", "id-ID"), ("MX", 0.07, "MXN", "es-MX"), ("FR", 0.04, "EUR", "fr-FR"),
    ("JP", 0.04, "JPY", "ja-JP"),
]  # fmt: skip
# Local prices of a "small pack" (about $1.99) in each currency, in micros.
PRICE_SMALL = {
    "TRY": 69_990_000, "USD": 1_990_000, "BRL": 9_990_000, "EUR": 1_990_000, "INR": 169_000_000,
    "GBP": 1_790_000, "IDR": 29_000_000_000, "MXN": 39_000_000, "JPY": 300_000_000,
}  # fmt: skip
TIERS = [("low", 0.35, 3072), ("mid", 0.45, 6144), ("high", 0.20, 8192)]
MODELS = {
    "android": {"low": "SM-A145F", "mid": "Redmi Note 13", "high": "Pixel 9"},
    "ios": {"low": "iPhone11,8", "mid": "iPhone14,5", "high": "iPhone17,1"},
}


def uuid7(ms: int, rng: random.Random) -> str:
    val = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rng.getrandbits(12) << 64
    val |= 0b10 << 62 | rng.getrandbits(62)
    return str(uuid.UUID(int=val))


def uuid4(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _pick(
    rng: random.Random, items: list[tuple[Any, ...]], weight_index: int = 1
) -> tuple[Any, ...]:
    total = sum(it[weight_index] for it in items)
    x = rng.random() * total
    for it in items:
        x -= it[weight_index]
        if x <= 0:
            return it
    return items[-1]


@dataclass
class Game:
    app_id: str
    name: str
    kind: str  # puzzle | runner | app
    engine: str
    sdk: str
    platforms: dict[str, float]  # platform -> share of installs
    installs_per_day: float
    d1: float
    decay: float
    sessions_per_day: float
    session_min: float  # median session length in minutes
    payer_rate: float
    ads_per_session: float
    ecpm_usd: float
    levels: int = 0
    spikes: dict[int, float] = field(default_factory=dict)  # level -> win probability
    base_win: float = 0.82
    version: str = "1.0.0"
    versions: list[tuple[int, str]] = field(default_factory=list)  # (day offset, version)
    start_day: int = 0  # first day (offset in the run) the game sends anything
    dev_days: int = 0  # days of debug builds from developer devices before release
    seed: int = 1

    def version_on(self, day: int) -> str:
        v = self.version
        for d, ver in self.versions:
            if day >= d:
                v = ver
        return v

    def win_prob(self, level: int) -> float:
        if level in self.spikes:
            return self.spikes[level]
        return max(0.35, self.base_win - 0.004 * level)


GAMES = [
    Game(
        app_id="demo.pebblepop", name="Pebble Pop", kind="puzzle", engine="unity 6000.3",
        sdk="unity/2.0.0", platforms={"android": 0.62, "ios": 0.38}, installs_per_day=18,
        d1=0.36, decay=0.66, sessions_per_day=1.7, session_min=7.0, payer_rate=0.035,
        ads_per_session=1.3, ecpm_usd=9.0, levels=60, spikes={12: 0.30, 25: 0.26, 40: 0.22},
        version="3.1.0", versions=[(30, "3.2.0")], seed=101,
    ),
    Game(
        app_id="demo.tidyhabit", name="Tidy Habit", kind="app", engine="flutter 3.32",
        sdk="flutter/2.0.0", platforms={"android": 0.5, "ios": 0.5}, installs_per_day=8,
        d1=0.27, decay=0.45, sessions_per_day=1.3, session_min=3.0, payer_rate=0.06,
        ads_per_session=0.0, ecpm_usd=0.0, version="2.4.0", seed=202,
    ),
    Game(
        app_id="demo.lanternleap", name="Lantern Leap", kind="runner", engine="unity 6000.3",
        sdk="unity/2.0.0", platforms={"android": 1.0}, installs_per_day=14, d1=0.33, decay=0.72,
        sessions_per_day=1.9, session_min=6.0, payer_rate=0.02, ads_per_session=2.0,
        ecpm_usd=7.0, levels=30, spikes={8: 0.35, 17: 0.28}, base_win=0.78, version="0.9.0",
        start_day=-12, dev_days=2, seed=303,
    ),
]  # fmt: skip


@dataclass
class Sent:
    """What the simulator sent, for checks against the collector and the warehouse."""

    events: int = 0
    invalid: int = 0
    resent_batches: int = 0
    resent_events: int = 0
    lost_batches: int = 0
    lost_events: int = 0
    batches: int = 0


class Device:
    """One install of one game: the SDK's state and its event queue."""

    def __init__(
        self,
        game: Game,
        rng: random.Random,
        install_ms: int,
        *,
        platform: str,
        store: str,
        build_env: str,
        country: tuple[str, float, str, str],
    ) -> None:
        self.game, self.rng = game, rng
        self.install_id = uuid4(rng)
        self.platform, self.store, self.build_env = platform, store, build_env
        self.country, _, self.currency, self.locale = country
        tier, _, ram = _pick(rng, TIERS)
        self.tier, self.ram = tier, ram
        # 2% of devices run their clock off by 10 min .. 3 h (either way).
        self.clock_off = 0
        if rng.random() < 0.02:
            self.clock_off = int(rng.choice((-1, 1)) * rng.uniform(10 * MIN, 3 * HOUR))
        self.install_ms = install_ms
        self.seq = 0
        self.snum = 0
        self.sid = ""
        self.session_start = 0
        self.queue: list[dict[str, Any]] = []
        self.version = game.version
        self.last_version: str | None = None

    def ctx(self) -> dict[str, Any]:
        store_raw = {"google_play": "com.android.vending", "samsung": "com.sec.android.app.samsungapps",
                     "huawei": "com.huawei.appmarket", "app_store": None, "testflight": None,
                     "sideload": None, "unknown": None}  # fmt: skip
        os_name = "android 14" if self.platform == "android" else "ios 18.6"
        return {
            "app_id": self.game.app_id,
            "platform": self.platform,
            "app_version": self.version,
            "build": self.version.replace(".", ""),
            "build_env": self.build_env,
            "install_id": self.install_id,
            "install_store": self.store,
            "installer_raw": store_raw.get(self.store),
            "app_name": self.game.name,
            "sdk": self.game.sdk,
            "engine": self.game.engine,
            "os": os_name,
            "device_model": MODELS[self.platform][self.tier],
            "device_tier": self.tier,
            "ram_mb": self.ram,
            "locale": self.locale,
            "tz_offset_min": 180 if self.country == "TR" else 0,
            "consent": "granted",
            "child": False,
        }

    def emit(self, name: str, p: dict[str, Any], t_ms: int) -> dict[str, Any]:
        fg = max(0, t_ms - self.session_start)
        ev = {
            "id": uuid7(t_ms + self.clock_off, self.rng),
            "seq": self.seq,
            "name": name,
            "ts": t_ms + self.clock_off,
            "sid": self.sid,
            "snum": self.snum,
            "fg_ms": fg,
            "uid": None,
            "p": p,
            "_true_ts": t_ms,
        }
        self.seq += 1
        self.queue.append(ev)
        return ev

    def start_session(self, t_ms: int, version: str) -> None:
        self.snum += 1
        self.sid = uuid7(t_ms + self.clock_off, self.rng)
        self.session_start = t_ms
        self.version = version

    def take_batch(self, send_true_ms: int, max_events: int = 500) -> dict[str, Any] | None:
        if not self.queue:
            return None
        events, self.queue = self.queue[:max_events], self.queue[max_events:]
        return {
            "v": C.BATCH_VERSION,
            "schema": C.CONTRACT_VERSION,
            "sent_at": send_true_ms + self.clock_off,
            "ctx": self.ctx(),
            "events": events,
        }


def strip_private(batch: dict[str, Any]) -> dict[str, Any]:
    """The batch as it goes on the wire (without the simulator's bookkeeping keys)."""
    return {
        **batch,
        "events": [{k: v for k, v in e.items() if not k.startswith("_")} for e in batch["events"]],
    }


class Player:
    """What one person does in one game; drives a Device."""

    def __init__(self, game: Game, rng: random.Random, device: Device) -> None:
        self.game, self.rng, self.dev = game, rng, device
        self.engagement = math.exp(rng.gauss(0, 0.55))
        self.level = 1
        self.attempts: dict[int, int] = {}
        self.payer = rng.random() < game.payer_rate
        self.coins = 100
        self.settings = {"music": "on", "sfx": "on", "haptics": "on", "notifications": "on"}
        self.wants_off = {
            "music": rng.random() < 0.32,
            "sfx": rng.random() < 0.16,
            "haptics": rng.random() < 0.11,
            "notifications": rng.random() < 0.20,
        }
        self.tutorial_done = False
        self.churned = False
        self.stuck_fails = 0
        self.crash_next = False
        self.prev_sid = ""
        self._step = 1.0

    # -- per day ------------------------------------------------------------------------

    def active_on(self, day_n: int) -> bool:
        if self.churned:
            return False
        if day_n == 0:
            return True
        p = min(0.97, self.game.d1 * self.engagement * day_n ** (-self.game.decay))
        if self.stuck_fails >= 3:
            p *= 0.55  # stuck behind a hard level
        return self.rng.random() < p

    def sessions_today(self, day_n: int) -> int:
        lam = self.game.sessions_per_day * (1.3 if day_n == 0 else 1.0) * min(self.engagement, 2.0)
        n = 0
        # Poisson by inversion, at least one session on an active day
        x, p = self.rng.random(), math.exp(-lam)
        s = p
        while x > s and n < 8:
            n += 1
            p *= lam / n
            s += p
        return max(1, n)

    # -- one session ------------------------------------------------------------------

    def session(self, t0: int, version: str, step_ms: float = 1.0) -> int:
        """Emit one session's events starting at true time t0; returns the end time.

        step_ms scales the gaps between events (1.0 = real pacing).
        """
        g, rng, dev = self.game, self.rng, self.dev
        self._step = step_ms
        first = dev.snum == 0
        dev.start_session(t0, version)
        t = t0

        def gap(lo_s: float, hi_s: float) -> int:
            return int(rng.uniform(lo_s, hi_s) * 1000 * step_ms)

        if first:
            dev.emit(C.EVENT_PULSE_INSTALL, {"install_store": dev.store,
                     "installer_raw": dev.ctx()["installer_raw"], "restored": False}, t)  # fmt: skip
        dev.emit(
            C.EVENT_PULSE_SESSION_START, {"cold": True, "since_last_s": 0 if first else 3600}, t
        )
        if self.crash_next:
            dev.emit(C.EVENT_PULSE_EXIT, {"reason": rng.choice(["crash", "crash_native", "anr"]),
                     "prev_sid": self.prev_sid, "prev_version": version}, t + 5)  # fmt: skip
            self.crash_next = False
        if dev.last_version and dev.last_version != version:
            dev.emit(C.EVENT_PULSE_APP_UPDATE, {"from_version": dev.last_version}, t + 8)
        dev.last_version = version
        load = {"low": 4200, "mid": 2600, "high": 1700}[dev.tier] * rng.uniform(0.8, 1.3)
        t += int(load)
        dev.emit(C.EVENT_PULSE_LOAD, {"pulse_v": 1, "tier": dev.tier, "kind": "cold_start",
                 "scene": "Menu", "duration_ms": round(load, 1)}, t)  # fmt: skip
        target = t0 + int(g.session_min * MIN * rng.lognormvariate(0, 0.5) * step_ms)
        if g.kind == "app":
            t = self._app_session(t, target, gap)
        else:
            if not self.tutorial_done:
                t = self._tutorial(t, gap)
            t = self._levels(t, target, gap)
        if rng.random() < 0.02:
            fps = [("NullReferenceException", "Board.Swap", "9f86d081884c7d65"),
                   ("IndexOutOfRangeException", "Grid.Neighbours", "3c59dc048e885024"),
                   ("TimeoutException", "Shop.LoadOffers", "b6589fc6ab0dc82c")]  # fmt: skip
            typ, frame, fp = rng.choice(fps)
            dev.emit(C.EVENT_PULSE_ERROR, {"type": typ, "msg": "error in " + frame, "frame": frame,
                     "fp": fp, "fatal": False}, t)  # fmt: skip
        ft = {"low": 18.2, "mid": 16.9, "high": 16.7}[dev.tier] * rng.uniform(0.95, 1.12)
        dev.emit(C.EVENT_PULSE_PERF, {"pulse_v": 1, "tier": dev.tier, "scene": "Level",
                 "window_s": 30.0, "frames": 1700, "ft_src": "ftm", "ft_p50_ms": round(ft, 2),
                 "ft_p95_ms": round(ft * 1.45, 2), "ft_max_ms": round(ft * 3.1, 2),
                 "slow_frames": int(rng.uniform(0, 40) if dev.tier == "low" else rng.uniform(0, 8)),
                 "mem_src": "recorder", "mem_reserved_mb": 420, "sys_mem_mb": dev.ram}, t)  # fmt: skip
        t += gap(1, 3)
        dev.emit(C.EVENT_PULSE_BACKGROUND, {"fg_ms": t - t0}, t)
        self.prev_sid = dev.sid
        if rng.random() < 0.004:
            self.crash_next = True
        return t

    def _tutorial(self, t: int, gap: Any) -> int:
        dev = self.dev
        dev.emit(C.EVENT_TUTORIAL_BEGIN, {}, t)
        for step, name in enumerate(["tap a pebble", "make a match", "use a booster"], 1):
            t += gap(3, 9)
            if self.rng.random() < 0.04:  # leaves during the tutorial
                return t
            dev.emit(C.EVENT_TUTORIAL_STEP, {"step": step, "step_name": name}, t)
        t += gap(2, 5)
        dev.emit(C.EVENT_TUTORIAL_COMPLETE, {}, t)
        self.tutorial_done = True
        return t

    def _maybe_setting(self, t: int) -> None:
        dev, rng = self.dev, self.rng
        for s, off in self.wants_off.items():
            if (
                off
                and self.settings[s] == "on"
                and (s != "notifications" or self.game.kind == "app")
            ):
                # most people who mute do it in their first sessions
                p = 0.45 if dev.snum <= 2 else 0.08
                if rng.random() < p:
                    dev.emit(C.EVENT_SETTING_CHANGE, {"setting": s, "from": "on", "to": "off"}, t)
                    self.settings[s] = "off"
                    return
        if dev.snum == 1 and rng.random() < 0.04:
            lang = dev.locale.split("-")[0]
            dev.emit(C.EVENT_SETTING_CHANGE, {"setting": "language", "from": "en", "to": lang}, t)

    def _ad(self, t: int, placement: str, fmt: str) -> None:
        dev, rng = self.dev, self.rng
        dev.emit(
            C.EVENT_AD_EVENT, {"action": "request", "ad_format": fmt, "placement": placement}, t
        )
        if rng.random() < 0.9:
            dev.emit(
                C.EVENT_AD_EVENT,
                {"action": "show", "ad_format": fmt, "placement": placement},
                t + 300,
            )
            value = (
                self.game.ecpm_usd
                / 1000
                * rng.lognormvariate(0, 0.4)
                * (1.6 if fmt == "rewarded" else 1)
            )
            dev.emit(C.EVENT_AD_IMPRESSION, {"ad_platform": "applovin_max", "ad_format": fmt,
                     "value": round(value, 6), "currency": "USD", "ad_source": rng.choice(["admob", "unity", "mintegral"]),
                     "placement": placement, "precision": "estimated"}, t + 400)  # fmt: skip

    def _purchase(self, t: int, product: str, mult: float) -> None:
        dev = self.dev
        price = int(PRICE_SMALL[dev.currency] * mult)
        dev.emit(C.EVENT_PURCHASE, {"transaction_id": f"T{dev.install_id[:8]}{dev.seq}",
                 "product_id": product, "price_micros": price, "currency": dev.currency,
                 "is_restore": False, "is_sandbox": dev.store == "testflight"}, t)  # fmt: skip

    def _levels(self, t: int, target: int, gap: Any) -> int:
        g, rng, dev = self.game, self.rng, self.dev
        world = g.kind == "runner"
        self._maybe_setting(t)
        while t < target and self.level <= g.levels:
            lvl = self.level
            att = self.attempts.get(lvl, 0) + 1
            self.attempts[lvl] = att
            dev.emit(C.EVENT_LEVEL_START, {"level": lvl, "attempt": att}, t)
            dur = int(rng.uniform(35, 110) * 1000 * self._step)
            t += dur
            if rng.random() < 0.015:  # the app was closed mid-level: no outcome is ever sent
                return t
            # hard levels get a little easier with every attempt
            p_win = min(0.95, g.win_prob(lvl) + 0.05 * (att - 1))
            x = rng.random()
            if x < 0.03:
                dev.emit(C.EVENT_LEVEL_QUIT, {"level": lvl, "attempt": att, "duration_ms": dur,
                         "reason": rng.choice(["menu", "restart"]), "progress": round(rng.random(), 3)}, t)  # fmt: skip
                if rng.random() < 0.5:
                    break
                continue
            if x < 0.03 + p_win:
                dev.emit(C.EVENT_LEVEL_COMPLETE, {"level": lvl, "attempt": att, "duration_ms": dur,
                         "score": int(rng.uniform(500, 3000)), "stars": rng.choice([1, 2, 3])}, t)  # fmt: skip
                earned = int(rng.choice([10, 20, 30]))
                dev.emit(C.EVENT_EARN_VIRTUAL_CURRENCY, {"virtual_currency_name": "coins",
                         "value": earned, "level": lvl}, t + 200)  # fmt: skip
                self.coins += earned
                self.level += 1
                self.stuck_fails = 0
            else:
                pos = self._death_pos(lvl, world)
                p = {"level": lvl, "attempt": att, "duration_ms": dur,
                     "reason": rng.choice(["out_of_moves", "out_of_moves", "out_of_time"]) if not world
                     else rng.choice(["fell", "hit_spike", "hit_spike"]),
                     "progress": pos["progress"], "pos_x": pos["x"], "pos_y": pos["y"],
                     "pos_space": "world" if world else "norm"}  # fmt: skip
                dev.emit(C.EVENT_LEVEL_FAIL, p, t)
                self.stuck_fails += 1 if lvl in g.spikes else 0
                if g.ads_per_session and rng.random() < 0.45:
                    self._ad(
                        t + 500, "after_fail", "interstitial" if rng.random() < 0.6 else "rewarded"
                    )
                if self.coins >= 50 and rng.random() < 0.3:
                    dev.emit(C.EVENT_SPEND_VIRTUAL_CURRENCY, {"virtual_currency_name": "coins",
                             "value": 50, "item_name": "extra_moves", "level": lvl}, t + 900)  # fmt: skip
                    self.coins -= 50
                if self.payer and rng.random() < 0.25:
                    self._purchase(t + 1500, "coins_small", 1.0)
            t += gap(4, 15)
        if g.ads_per_session and rng.random() < 0.3:
            self._ad(t, "session_end", "interstitial")
        return t

    def _death_pos(self, level: int, world: bool) -> dict[str, float]:
        """Deaths cluster around two hot spots per level (fixed by the level number)."""
        lr = random.Random(level * 7919 + self.game.seed)
        spots = [(lr.uniform(0.15, 0.85), lr.uniform(0.2, 0.9)) for _ in range(2)]
        sx, sy = spots[0] if self.rng.random() < 0.65 else spots[1]
        x = min(1.0, max(0.0, self.rng.gauss(sx, 0.06)))
        y = min(1.0, max(0.0, self.rng.gauss(sy, 0.06)))
        if world:  # a runner level is 200 world units long and 12 high
            return {"x": round(x * 200, 2), "y": round(y * 12, 2), "progress": round(x, 3)}
        return {"x": round(x, 4), "y": round(y, 4), "progress": round(min(0.99, x * 0.9 + 0.05), 3)}

    def _app_session(self, t: int, target: int, gap: Any) -> int:
        dev, rng = self.dev, self.rng
        self._maybe_setting(t)
        screens = ["home", "habits", "stats", "settings", "paywall"]
        weights = [0.35, 0.35, 0.15, 0.08, 0.07]
        while t < target:
            s = rng.choices(screens, weights)[0]
            dev.emit(
                C.EVENT_SCREEN_VIEW, {"screen_name": s, "screen_class": s.title() + "Screen"}, t
            )
            if s == "habits":
                for _ in range(rng.randint(1, 3)):
                    t += gap(2, 8)
                    dev.emit(
                        "habit_checked",
                        {"habit_type": rng.choice(["water", "walk", "read", "sleep"])},
                        t,
                    )
            if s == "paywall" and self.payer and rng.random() < 0.35:
                self._purchase(t + 2000, "premium_monthly", 2.5)
                self.payer = False  # one subscription start
            t += gap(5, 20)
        return t
