"""Batch processing of the collector, independent of the web framework.

    decode (gzip, size caps) -> envelope -> consent / allowlist / deletions ->
    per event: contract validation, child-mode stripping, skew correction,
    time sanity -> dedupe (in batch, then against the store) -> durable write ->
    registry (first sight of an app) -> live window -> answer.

Nothing an event carries is dropped silently: an invalid event is written to
the quarantine with its reason, a duplicate is counted, a consent-denied or
deleted install is counted as ignored. A 2xx means every event of the batch was
durably stored, quarantined, or is a duplicate of a stored one.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import secrets
import threading
import time
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..contract import spec
from ..contract.validate import EnvelopeError, check_envelope, check_event
from .geo import CountryResolver
from .live import LiveWindow
from .settings import CollectorSettings

CTX_COPY = (
    "app_id", "platform", "app_version", "build", "build_env", "install_id", "install_store",
    "installer_raw", "sdk", "engine", "os", "device_model", "device_tier", "ram_mb", "locale",
    "tz_offset_min", "consent", "child",
)  # fmt: skip
RAW_MAX = 8192


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class HTTPError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class TokenBuckets:
    """Per-key token buckets kept in memory; keys are salted hashes, never raw IPs."""

    def __init__(self, rate: float, burst: int, max_keys: int = 100_000) -> None:
        self.rate, self.burst, self.max_keys = rate, burst, max_keys
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now_s: float) -> bool:
        if self.rate <= 0:
            return True
        with self._lock:
            tokens, last = self._b.get(key, (float(self.burst), now_s))
            tokens = min(self.burst, tokens + (now_s - last) * self.rate)
            ok = tokens >= 1
            self._b[key] = (tokens - 1 if ok else tokens, now_s)
            if len(self._b) > self.max_keys:
                self._b.clear()
            return ok


def decode_body(raw: bytes, encoding: str | None, limit: int) -> bytes:
    enc = (encoding or "identity").strip().lower()
    if enc in ("", "identity"):
        if len(raw) > limit:
            raise HTTPError(413, "body_too_large")
        return raw
    if enc != "gzip":
        raise HTTPError(415, "unsupported_content_encoding")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(raw, limit + 1)
    except zlib.error as exc:
        raise HTTPError(400, "bad_gzip") from exc
    if len(out) > limit or d.unconsumed_tail:
        raise HTTPError(413, "inflated_too_large")  # gzip bomb guard
    if not d.eof:
        raise HTTPError(400, "bad_gzip")
    return out


def _genre(ctx: dict[str, Any], names: set[str]) -> str:
    if str(ctx.get("engine") or "").lower().startswith("unity") or any(
        n.startswith("level_") for n in names
    ):
        return "game"
    return "app"


@dataclass
class Counters:
    accepted: int = 0
    duplicates: int = 0
    quarantined: int = 0
    ignored: int = 0
    batches: int = 0
    rejected_batches: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)


class Collector:
    """One collector process. `store` is a LocalStore or a BigQuery store."""

    def __init__(
        self,
        settings: CollectorSettings,
        store: Any,
        *,
        clock: Callable[[], int] = now_ms,
        geo: CountryResolver | None = None,
        live: LiveWindow | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock
        self.geo = geo or CountryResolver(settings.geoip, settings.country_header)
        self.live = live or LiveWindow(settings.live_window_s * 1000, settings.spark_minutes)
        self.keys = settings.key_ids()
        self.limits = spec()["limits"]
        self.prod_stores = frozenset(spec()["production_stores"])
        self._salt = secrets.token_bytes(16)
        self._ip_buckets = TokenBuckets(settings.rate_per_ip, settings.burst_per_ip)
        self._install_buckets = TokenBuckets(settings.rate_per_install, settings.burst_per_install)
        self._write_lock = threading.Lock()
        self._apps: dict[tuple[str, str], dict[str, Any]] = {}
        self._apps_lock = threading.Lock()
        self._registry_cache: tuple[int, list[dict[str, Any]]] = (0, [])
        self.deleted: set[str] = set(store.deleted_installs())
        self.counters = Counters()
        self._counter_lock = threading.Lock()

    # -- request-level helpers ---------------------------------------------------------

    def check_key(self, headers: dict[str, str]) -> str:
        key = headers.get("x-pulse-key") or ""
        if not key:
            auth = headers.get("authorization") or ""
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
        kid = self.keys.get(key)
        if not kid:
            raise HTTPError(401, "unknown_write_key")
        return kid

    def rate_limit(self, ip: str | None) -> None:
        if not ip:
            return
        if not self.settings.limit_loopback and ip in ("127.0.0.1", "::1", "localhost"):
            return
        h = hashlib.blake2b(ip.encode(), key=self._salt, digest_size=12).hexdigest()
        if not self._ip_buckets.allow(h, time.monotonic()):
            raise HTTPError(429, "rate_limited")

    def _count(self, **kw: int) -> None:
        with self._counter_lock:
            for k, v in kw.items():
                setattr(self.counters, k, getattr(self.counters, k) + v)

    def _reason(self, reason: str, n: int = 1) -> None:
        with self._counter_lock:
            base = reason.split(":", 1)[0]
            self.counters.by_reason[base] = self.counters.by_reason.get(base, 0) + n

    def _quarantine_row(
        self, received: int, reason: str, kid: str, ctx: dict[str, Any] | None, ev: Any
    ) -> dict[str, Any]:
        raw = json.dumps(ev, separators=(",", ":"), ensure_ascii=False, default=str)
        ctx = ctx or {}
        return {
            "received_at": received,
            "reason": reason,
            "app_id": ctx.get("app_id") if isinstance(ctx.get("app_id"), str) else None,
            "platform": ctx.get("platform") if isinstance(ctx.get("platform"), str) else None,
            "install_id": ctx.get("install_id") if isinstance(ctx.get("install_id"), str) else None,
            "event_id": ev.get("id") if isinstance(ev, dict) and isinstance(ev.get("id"), str) else None,
            "name": ev.get("name") if isinstance(ev, dict) and isinstance(ev.get("name"), str) else None,
            "raw": raw[:RAW_MAX],
            "key_id": kid,
        }  # fmt: skip

    def _reject_batch(self, received: int, reason: str, kid: str, head: Any) -> None:
        """A request-level failure is recorded in the quarantine too (never silent)."""
        try:
            self.store.write(
                [], [self._quarantine_row(received, reason, kid, None, head)], received
            )
        except Exception:  # the request fails anyway; do not mask the original error
            pass
        self._count(rejected_batches=1)
        self._reason(reason)

    # -- v2 -------------------------------------------------------------------------------

    def process_v2(
        self, raw: bytes, headers: dict[str, str], ip: str | None
    ) -> tuple[int, dict[str, Any]]:
        received = self.clock()
        kid = self.check_key(headers)
        self.rate_limit(ip)
        limit = self.limits["batch_max_bytes"]
        try:
            data = decode_body(raw, headers.get("content-encoding"), limit)
        except HTTPError as exc:
            self._reject_batch(received, f"batch:{exc.reason}", kid, {"bytes": len(raw)})
            raise
        try:
            body = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            self._reject_batch(
                received, "batch:bad_json", kid, data[:RAW_MAX].decode("utf-8", "replace")
            )
            raise HTTPError(400, "bad_json") from exc
        try:
            sent_at, schema, ctx, events = check_envelope(body)
        except EnvelopeError as exc:
            head = (
                {k: body.get(k) for k in ("v", "schema", "sent_at", "ctx")}
                if isinstance(body, dict)
                else body
            )
            self._reject_batch(received, f"batch:{exc.reason}", kid, head)
            raise HTTPError(exc.status, exc.reason) from exc
        country = self.geo.country(headers, ip)
        return 200, self._process(received, kid, sent_at, schema, ctx, events, country)

    def _process(
        self,
        received: int,
        kid: str,
        sent_at: int,
        schema: str,
        ctx: dict[str, Any],
        events: list[Any],
        country: str,
    ) -> dict[str, Any]:
        n = len(events)
        self._count(batches=1)
        install = ctx["install_id"]
        if not self._install_buckets.allow(install, time.monotonic()):
            raise HTTPError(429, "rate_limited")
        if ctx.get("consent") == "denied":
            self._count(ignored=n)
            return {"ok": True, "accepted": 0, "duplicates": 0, "quarantined": 0, "ignored": n,
                    "note": "consent denied: nothing stored"}  # fmt: skip
        if install in self.deleted:
            self._count(ignored=n)
            return {"ok": True, "accepted": 0, "duplicates": 0, "quarantined": 0, "ignored": n,
                    "note": "install deleted: nothing stored"}  # fmt: skip
        quarantine: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        prefixes = self.settings.bundle_prefixes
        if prefixes and not ctx["app_id"].startswith(tuple(prefixes)):
            for ev in events:
                quarantine.append(self._quarantine_row(received, "app_not_allowed", kid, ctx, ev))
            with self._write_lock:
                self.store.write([], quarantine, received)
            self._count(quarantined=n)
            self._reason("app_not_allowed", n)
            return {"ok": True, "accepted": 0, "duplicates": 0, "quarantined": n, "ignored": 0,
                    "rejected": [{"id": q["event_id"], "reason": q["reason"]} for q in quarantine]}  # fmt: skip
        skew = received - sent_at
        lim = self.limits
        child = bool(ctx.get("child"))
        base = {k: ctx.get(k) for k in CTX_COPY}
        if child:
            base["child"] = True
        base.update(
            country=country, key_id=kid, schema=schema, sent_at=sent_at, received_at=received
        )
        rows: list[dict[str, Any]] = []
        keys: list[tuple[str, str, int | None]] = []
        seen_ids: set[str] = set()
        seen_seq: set[int] = set()
        dup = 0
        for ev in events:
            clean, reason = check_event(ev, child=child)
            if clean is not None:
                ts = clean["ts"] + skew
                if ts > received + lim["future_tolerance_ms"]:
                    clean, reason = None, "ts_future"
                elif ts < lim["min_ts_ms"]:
                    clean, reason = None, "ts_too_old"
            if clean is None:
                q = self._quarantine_row(received, reason or "invalid", kid, ctx, ev)
                quarantine.append(q)
                rejected.append({"id": q["event_id"], "reason": q["reason"]})
                self._reason(reason or "invalid")
                continue
            eid, seq = clean["id"].lower(), clean["seq"]
            if eid in seen_ids or seq in seen_seq:
                dup += 1
                continue
            seen_ids.add(eid)
            seen_seq.add(seq)
            rows.append(
                {
                    "event_id": eid,
                    "seq": seq,
                    "name": clean["name"],
                    "ts": ts,
                    "ts_client": clean["ts"],
                    "skew_ms": skew,
                    "late": received - ts > lim["late_after_ms"],
                    "sid": clean["sid"].lower(),
                    "snum": clean.get("snum"),
                    "fg_ms": clean.get("fg_ms"),
                    "uid": clean.get("uid"),
                    "params": json.dumps(clean["p"], separators=(",", ":"), ensure_ascii=False),
                    **base,
                }
            )
            keys.append((eid, install, seq))
        with self._write_lock:
            already = self.store.seen(keys)
            if already:
                dup += len(already)
                rows = [r for i, r in enumerate(rows) if i not in already]
                keys = [k for i, k in enumerate(keys) if i not in already]
            self.store.write(rows, quarantine, received)
            self.store.mark_seen(keys, received)
        self._count(accepted=len(rows), duplicates=dup, quarantined=len(quarantine))
        self._after_write(ctx, rows, received)
        return {
            "ok": True,
            "accepted": len(rows),
            "duplicates": dup,
            "quarantined": len(quarantine),
            "ignored": 0,
            "rejected": rejected,
            "skew_ms": skew,
        }

    def _after_write(self, ctx: dict[str, Any], rows: list[dict[str, Any]], received: int) -> None:
        if not rows:
            return
        app_id, platform = ctx["app_id"], ctx["platform"]
        names = {r["name"] for r in rows}
        store_ = ctx.get("install_store") or "unknown"
        env = ctx.get("build_env") or "unknown"
        release = env == "release" and store_ not in ("editor", "simulator")
        test_store = store_ in ("testflight", "sideload")
        self._registry(ctx, names, received, release, test_store, store_)
        if release and store_ in self.prod_stores:
            self.store.add_prod_installs([(app_id, platform, ctx["install_id"])], received)
        self.live.observe(
            app_id,
            ctx["install_id"],
            platform=platform,
            store=store_,
            country=rows[0]["country"],
            build_env=env,
            ts_list=[r["ts"] for r in rows],
            now_ms=received,
        )

    def _registry(
        self,
        ctx: dict[str, Any],
        names: set[str],
        now: int,
        release: bool,
        test_store: bool,
        store_: str,
    ) -> None:
        key = (ctx["app_id"], ctx["platform"])
        version = ctx.get("app_version")
        genre = _genre(ctx, names)
        with self._apps_lock:
            known = self._apps.get(key)
            if known is None:
                self.store.register_app(
                    key[0], key[1], now, name=ctx.get("app_name"), genre=genre, version=version
                )
                known = {"touched": 0, "version": None, "release": False, "test": False,
                         "genre": None, "stores": set()}  # fmt: skip
                self._apps[key] = known
            changed = (
                version != known["version"]
                or (release and not known["release"])
                or (test_store and not known["test"])
                or (genre == "game" and known["genre"] != "game")
                or store_ not in known["stores"]
                or now - known["touched"] > 60_000
            )
            if changed:
                self.store.touch_app(
                    key[0], key[1], now, version=version, release=release, test_store=test_store,
                    store=store_, genre=genre, name=ctx.get("app_name"),
                )  # fmt: skip
                known.update(touched=now, version=version, genre=genre)
                known["release"] |= release
                known["test"] |= test_store
                known["stores"].add(store_)

    # -- v1 JSON lines --------------------------------------------------------------------

    def process_v1(
        self, raw: bytes, headers: dict[str, str], ip: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Contract v1 JSON lines (docs/EVENTS.md) upgraded to v2 rows.

        v1 has no event id, sequence or install id: the id is a UUID derived from
        the line (so a re-sent line is a duplicate), the session id stands in for
        the install id, and the schema column says "1".
        """
        received = self.clock()
        kid = self.check_key(headers)
        self.rate_limit(ip)
        data = decode_body(raw, headers.get("content-encoding"), self.limits["batch_max_bytes"])
        from datetime import datetime

        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        bad: list[tuple[str, Any]] = []
        for line in data.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if ev.get("v") != 1:
                    raise ValueError("v")
                ts = int(datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")).timestamp() * 1000)
                session = str(ev["session"])
                sid = (
                    str(uuid.UUID(hex=session))
                    if len(session) == 32
                    else str(uuid.uuid5(uuid.NAMESPACE_URL, session))
                )
                platform = str(ev.get("platform") or "unknown").lower()
                platform = {"iphoneplayer": "ios", "androidplayer": "android", "web": "webgl"}.get(
                    platform, platform
                )
                key = (str(ev["app"]), platform, str(ev.get("app_version") or "0"), sid)
                eid = uuid.uuid5(uuid.NAMESPACE_URL, "pulse-v1:" + line)
                groups.setdefault(key, []).append(
                    {
                        "id": str(eid),
                        # v1 has no sequence: a number derived from the line keeps
                        # (install_id, seq) unique; loss metrics skip schema "1".
                        "seq": int(eid.hex[:13], 16),
                        "name": ev["event"],
                        "ts": ts,
                        "sid": sid,
                        "p": ev.get("params") or {},
                    }
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                bad.append(("v1:bad_line", line[:RAW_MAX]))
        accepted = dup = quarantined = 0
        if bad:
            rows = [self._quarantine_row(received, r, kid, None, raw_line) for r, raw_line in bad]
            with self._write_lock:
                self.store.write([], rows, received)
            quarantined += len(rows)
            self._reason("v1:bad_line", len(rows))
        for (app, platform, version, sid), evs in groups.items():
            ctx = {"app_id": app, "platform": platform if platform in spec()["enums"]["platform"] else "editor",
                   "app_version": version, "install_id": sid, "sdk": "v1", "build_env": None}  # fmt: skip
            res = self._process(
                received, kid, received, "1", ctx, evs, self.geo.country(headers, ip)
            )
            accepted += res["accepted"]
            dup += res["duplicates"]
            quarantined += res["quarantined"]
        return 200, {
            "ok": True,
            "accepted": accepted,
            "duplicates": dup,
            "quarantined": quarantined,
        }

    # -- deletion and live ---------------------------------------------------------------

    def delete(self, body: Any, headers: dict[str, str]) -> dict[str, Any]:
        self.check_key(headers)
        if not isinstance(body, dict) or not isinstance(body.get("install_id"), str):
            raise HTTPError(400, "missing:install_id")
        install = body["install_id"].strip().lower()
        try:
            uuid.UUID(install)
        except ValueError as exc:
            raise HTTPError(400, "uuid:install_id") from exc
        app_id = body.get("app_id") if isinstance(body.get("app_id"), str) else None
        reason = str(body.get("reason") or "user_request")[:40]
        now = self.clock()
        self.store.add_deletion(install, app_id, reason, now)
        self.deleted.add(install)
        self.live.forget(install)
        return {"ok": True, "install_id": install, "status": "pending",
                "note": "events of this install are no longer accepted; stored rows are removed by the next run"}  # fmt: skip

    def registry_rows(self) -> list[dict[str, Any]]:
        t, rows = self._registry_cache
        now = time.monotonic_ns() // 1_000_000
        if now - t > 2_000:
            rows = self.store.registry()
            self._registry_cache = (now, rows)
        return rows

    def live_snapshot(self) -> dict[str, Any]:
        now = self.clock()
        snap = self.live.snapshot(now)
        reg = self.registry_rows()
        by_app: dict[str, list[dict[str, Any]]] = {}
        for r in reg:
            by_app.setdefault(r["app_id"], []).append(r)
        apps = []
        order = {"live": 0, "testing": 1, "new": 2}
        for app_id in sorted(set(snap) | set(by_app)):
            rows = by_app.get(app_id, [])
            status = min((r["status"] for r in rows), key=lambda s: order.get(s, 9), default="new")
            s = snap.get(app_id) or {"active": 0, "platforms": {}, "by": [],
                                     "events_per_min": [0] * self.live.spark_minutes,
                                     "events_last_min": 0}  # fmt: skip
            apps.append(
                {
                    "app_id": app_id,
                    "name": next(
                        (r["display_name"] for r in rows if r.get("display_name")), app_id
                    ),
                    "status": status,
                    "platform_status": {r["platform"]: r["status"] for r in rows},
                    "first_seen_ms": min((r["first_seen_ms"] for r in rows), default=None),
                    "last_seen_ms": max((r["last_seen_ms"] or 0 for r in rows), default=None),
                    "genre": next((r["genre"] for r in rows if r.get("genre")), None),
                    **s,
                }
            )
        apps.sort(key=lambda a: (-a["active"], a["app_id"]))
        banner_from = now - self.settings.new_app_banner_s * 1000
        new_apps = [
            {"app_id": r["app_id"], "platform": r["platform"], "name": r["display_name"],
             "first_seen_ms": r["first_seen_ms"]}
            for r in reg if r["first_seen_ms"] >= banner_from
        ]  # fmt: skip
        c = self.counters
        return {
            "now_ms": now,
            "window_s": self.settings.live_window_s,
            "spark_minutes": self.live.spark_minutes,
            "total_active": sum(a["active"] for a in apps),
            "apps": apps,
            "new_apps": sorted(new_apps, key=lambda r: -r["first_seen_ms"]),
            "collector": {
                "mode": self.store.kind,
                "batches": c.batches,
                "accepted": c.accepted,
                "duplicates": c.duplicates,
                "quarantined": c.quarantined,
                "ignored": c.ignored,
                "rejected_batches": c.rejected_batches,
                "quarantine_reasons": dict(sorted(c.by_reason.items())),
            },
        }


def gzip_json(obj: Any) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
