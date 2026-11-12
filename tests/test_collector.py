"""`pulse collect`: HTTP paths, validation, dedupe, skew, durability, privacy, live window.

Most tests use FastAPI's TestClient with a fake clock; the concurrency and IP
tests run a real uvicorn server on 127.0.0.1 (collector/inprocess.py).
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from studio_pulse.collector import core
from studio_pulse.collector.app import create_app
from studio_pulse.collector.core import Collector
from studio_pulse.collector.geo import CountryResolver
from studio_pulse.collector.inprocess import running_collector
from studio_pulse.collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings, from_dict, key_id
from studio_pulse.collector.store import LocalStore

T0 = 1_793_000_000_000  # 2026-10-26 (ms)
KEY = {"X-Pulse-Key": DEFAULT_LOCAL_KEY}


class Clock:
    def __init__(self, ms: int = T0) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


def uid(n: int, prefix: str = "0190f0e2-0000-7000-8000") -> str:
    return f"{prefix}-{n:012x}"


def install(n: int = 1) -> str:
    return str(uuid.UUID(int=n, version=4))


def ev(seq: int, name: str = "level_start", p: dict | None = None, ts: int = T0, **kw) -> dict:
    out = {
        "id": kw.pop("id", uid(seq)),
        "seq": seq,
        "name": name,
        "ts": ts,
        "sid": "0190f0e0-0000-7000-8000-000000000001",
        "snum": 1,
        "fg_ms": 1000 * seq,
        "p": {"level": 1} if p is None and name == "level_start" else (p or {}),
    }
    out.update(kw)
    return out


def batch(
    events: list[dict], *, sent_at: int = T0, app: str = "com.example.game", inst: int = 1, **ctx
) -> dict:
    c = {
        "app_id": app,
        "platform": "android",
        "app_version": "1.0.0",
        "install_id": install(inst),
        "sdk": "unity/2.0.0",
        "build_env": "release",
        "install_store": "google_play",
    }
    c.update(ctx)
    return {"v": 2, "schema": "2.0", "sent_at": sent_at, "ctx": c, "events": events}


def rows(data_dir: Path, sub: str = "events") -> list[dict]:
    out = []
    for f in sorted((data_dir / sub).glob("dt=*/*.ndjson")):
        out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line]
    return out


@pytest.fixture()
def col(tmp_path):
    clock = Clock()
    settings = CollectorSettings(data_dir=tmp_path / "c", rate_per_ip=0)
    store = LocalStore(settings.data_dir)
    c = Collector(settings, store, clock=clock)
    client = TestClient(create_app(settings, c))
    yield client, c, clock, settings.data_dir
    store.close()


def post(client, body, headers=None, **kw):
    return client.post("/v2/batch", json=body, headers={**KEY, **(headers or {})}, **kw)


def test_accepts_a_batch_and_writes_ndjson_rows(col):
    client, c, clock, d = col
    r = post(client, batch([ev(0, "pulse_session_start", {"cold": True}), ev(1)]))
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 2
    stored = rows(d)
    assert [x["name"] for x in stored] == ["pulse_session_start", "level_start"]
    row = stored[1]
    assert row["app_id"] == "com.example.game" and row["install_id"] == install(1)
    assert row["key_id"] == key_id(DEFAULT_LOCAL_KEY) and row["country"] == "unknown"
    assert json.loads(row["params"]) == {"level": 1}
    assert row["received_at"] == T0 and row["schema"] == "2.0"


def test_gzip_body_and_encoding_errors(col):
    client, *_ = col
    body = gzip.compress(json.dumps(batch([ev(0)])).encode())
    hdr = {**KEY, "Content-Encoding": "gzip", "Content-Type": "application/json"}
    assert client.post("/v2/batch", content=body, headers=hdr).json()["accepted"] == 1
    assert client.post("/v2/batch", content=b"not gzip", headers=hdr).status_code == 400
    hdr["Content-Encoding"] = "br"
    assert client.post("/v2/batch", content=body, headers=hdr).status_code == 415


def test_401_400_413_paths(col):
    client, c, _, d = col
    assert client.post("/v2/batch", json=batch([ev(0)])).status_code == 401
    assert post(client, batch([ev(0)]), {"X-Pulse-Key": "sp_pub_wrong"}).status_code == 401
    bearer = {"Authorization": f"Bearer {DEFAULT_LOCAL_KEY}"}
    assert client.post("/v2/batch", json=batch([ev(0)]), headers=bearer).status_code == 200
    r = client.post(
        "/v2/batch", content=b"{not json", headers={**KEY, "Content-Type": "application/json"}
    )
    assert r.status_code == 400 and r.json()["error"] == "bad_json"
    r = post(client, {"v": 2, "schema": "2.0", "sent_at": T0, "ctx": {}, "events": [ev(0)]})
    assert r.status_code == 400 and r.json()["error"] == "ctx.missing:app_id"
    r = post(client, batch([ev(i, id=uid(i)) for i in range(501)]))
    assert r.status_code == 413 and r.json()["error"] == "too_many_events"
    big = b"x" * (1024 * 1024 + 1)
    r = client.post("/v2/batch", content=big, headers={**KEY, "Content-Type": "application/json"})
    assert r.status_code == 413
    # gzip bomb: 4 MB of zeros compress to a few KB
    bomb = gzip.compress(b"0" * (4 * 1024 * 1024))
    r = client.post("/v2/batch", content=bomb, headers={**KEY, "Content-Encoding": "gzip"})
    assert r.status_code == 413 and r.json()["error"] == "inflated_too_large"
    # nothing disappears silently: every refused request left a quarantine row with its reason
    reasons = {q["reason"] for q in rows(d, "quarantine")}
    assert {"batch:bad_json", "batch:ctx.missing:app_id", "batch:too_many_events"} <= reasons
    assert "batch:inflated_too_large" in reasons


def test_same_batch_twice_is_stored_once(col):
    client, c, clock, d = col
    b = batch([ev(i) for i in range(5)])
    assert post(client, b).json()["accepted"] == 5
    clock.ms += 5000
    again = post(client, b).json()
    assert again["accepted"] == 0 and again["duplicates"] == 5
    assert len(rows(d)) == 5
    # a new id for an already stored (install_id, seq) is a repeat too
    r = post(client, batch([ev(3, id=uid(99))])).json()
    assert r["duplicates"] == 1 and len(rows(d)) == 5
    # in-batch repeats
    r = post(client, batch([ev(10), ev(10), ev(11, id=uid(10))])).json()
    assert (r["accepted"], r["duplicates"]) == (1, 2)


def test_dedupe_survives_a_restart(tmp_path):
    settings = CollectorSettings(data_dir=tmp_path / "c")
    for _ in range(2):
        store = LocalStore(settings.data_dir)
        client = TestClient(create_app(settings, Collector(settings, store, clock=Clock())))
        post(client, batch([ev(0), ev(1)]))
        store.close()
    assert len(rows(settings.data_dir)) == 2


def test_skew_correction_and_time_sanity(col):
    client, c, clock, d = col
    # device clock 2 h fast: events 10 s and 60 s before sending
    off = 2 * 3_600_000
    clock.ms = T0 + 300
    b = batch([ev(0, ts=T0 - 10_000 + off), ev(1, ts=T0 - 60_000 + off)], sent_at=T0 + off)
    r = post(client, b).json()
    assert r["accepted"] == 2 and r["skew_ms"] == 300 - off
    got = {x["seq"]: x for x in rows(d)}
    # Segment: timestamp = receivedAt - (sentAt - originalTimestamp)
    assert got[0]["ts"] == clock.ms - (T0 + off - (T0 - 10_000 + off)) == T0 - 9_700
    assert got[1]["ts"] == T0 - 59_700 and got[1]["ts_client"] == T0 - 60_000 + off
    # an event 2 h in the future on a correct clock is quarantined; the rest is kept
    r = post(
        client, batch([ev(5, ts=clock.ms + 2 * 3_600_000), ev(6, ts=clock.ms)], sent_at=clock.ms)
    ).json()
    assert r["accepted"] == 1 and r["rejected"] == [{"id": uid(5), "reason": "ts_future"}]
    assert any(
        q["reason"] == "ts_future" and q["event_id"] == uid(5) for q in rows(d, "quarantine")
    )
    # 40 days old: kept and flagged late; before 2020: quarantined
    old = clock.ms - 40 * 86_400_000
    r = post(client, batch([ev(7, ts=old), ev(8, ts=1_500_000_000_000)], sent_at=clock.ms)).json()
    assert r["accepted"] == 1 and r["rejected"][0]["reason"] == "ts_too_old"
    assert {x["seq"]: x["late"] for x in rows(d)}[7] is True


def test_invalid_events_are_quarantined_with_reasons(col):
    client, c, _, d = col
    bad = [
        ev(1, "level_fail", {"level": 3}),  # missing params
        ev(2, "session_start", {}),  # Firebase-reserved
        ev(3, "booster_used", {"note": "write to a@b.co"}),  # e-mail
    ]
    r = post(client, batch([ev(0), *bad])).json()
    assert r["accepted"] == 1 and r["quarantined"] == 3
    reasons = sorted(x["reason"] for x in r["rejected"])
    assert reasons == ["missing_param:attempt", "pii_email:note", "reserved_name"]
    q = rows(d, "quarantine")
    assert len(q) == 3 and all(x["raw"] and x["app_id"] == "com.example.game" for x in q)
    assert c.counters.by_reason["reserved_name"] == 1


def test_consent_denied_stores_nothing(col):
    client, c, _, d = col
    r = post(client, batch([ev(0), ev(1)], consent="denied")).json()
    assert r["accepted"] == 0 and r["ignored"] == 2
    assert rows(d) == [] and rows(d, "quarantine") == []


def test_child_mode_strips_uid_and_free_text(col):
    client, _, _, d = col
    e = ev(0, "tutorial_step", {"step": 1, "step_name": "drag"}, uid="acct-7")
    post(client, batch([e], child=True))
    (row,) = rows(d)
    assert row["uid"] is None and json.loads(row["params"]) == {"step": 1} and row["child"] is True


def test_deletion_request_blocks_the_install(col):
    client, c, clock, d = col
    post(client, batch([ev(0)]))
    assert c.live_snapshot()["total_active"] == 1
    r = client.post("/v2/delete", json={"install_id": install(1), "reason": "opt_out"}, headers=KEY)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert c.live_snapshot()["total_active"] == 0
    r = post(client, batch([ev(1)])).json()
    assert r["accepted"] == 0 and r["ignored"] == 1
    assert client.post("/v2/delete", json={"install_id": "x"}, headers=KEY).status_code == 400
    assert client.post("/v2/delete", json={"install_id": install(1)}).status_code == 401
    # a restarted collector still refuses it
    assert install(1) in LocalStore(d).deleted_installs()


def test_bundle_allowlist_quarantines_unknown_apps(tmp_path):
    s = CollectorSettings(data_dir=tmp_path / "c", bundle_prefixes=["com.example."])
    store = LocalStore(s.data_dir)
    client = TestClient(create_app(s, Collector(s, store, clock=Clock())))
    assert post(client, batch([ev(0)])).json()["accepted"] == 1
    r = post(client, batch([ev(0)], app="org.spam.app")).json()
    assert r["accepted"] == 0 and r["quarantined"] == 1
    assert [x["app_id"] for x in store.registry()] == ["com.example.game"]
    assert rows(s.data_dir, "quarantine")[0]["reason"] == "app_not_allowed"
    store.close()


def test_registry_row_and_promotion_to_testing(col):
    client, c, clock, d = col
    post(
        client, batch([ev(0)], build_env="debug", install_store="unknown", app_name="Example Game")
    )
    (reg,) = c.store.registry()
    assert (reg["app_id"], reg["platform"], reg["status"], reg["display_name"]) == (
        "com.example.game", "android", "new", "Example Game")  # fmt: skip
    assert reg["genre"] == "game"  # level events
    clock.ms += 1000
    post(client, batch([ev(1)], inst=2))  # a release build from Google Play
    (reg,) = c.store.registry()
    assert reg["status"] == "testing" and reg["stores_seen"] == "google_play,unknown"


def test_fifty_parallel_first_batches_make_one_registry_row(tmp_path):
    s = CollectorSettings(data_dir=tmp_path / "c")
    with running_collector(s) as (url, c):
        sess = requests.Session()

        def first(i: int) -> int:
            b = batch([ev(0, id=uid(i))], app="com.example.brandnew", inst=100 + i)
            return sess.post(url + "/v2/batch", json=b, headers=KEY, timeout=30).status_code

        with ThreadPoolExecutor(50) as pool:
            codes = list(pool.map(first, range(50)))
        assert codes == [200] * 50
        reg = c.store.registry()
    assert [(r["app_id"], r["platform"]) for r in reg] == [("com.example.brandnew", "android")]


def test_registry_insert_is_safe_across_connections(tmp_path):
    """The SQL itself (not only the collector's lock) creates one row for 50 racing writers."""
    stores = [LocalStore(tmp_path / "c") for _ in range(5)]
    barrier = threading.Barrier(50)
    created = []

    def go(i: int) -> None:
        barrier.wait()
        created.append(
            stores[i % 5].register_app("com.x", "ios", T0 + i, name=None, genre="app", version="1")
        )

    threads = [threading.Thread(target=go, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert created.count(True) == 1 and len(stores[0].registry()) == 1
    for s in stores:
        s.close()


def test_no_ip_address_is_ever_stored(tmp_path):
    class Reader:
        def get(self, ip):
            return {"country": {"iso_code": "US"}} if ip == "8.8.4.4" else None

    s = CollectorSettings(data_dir=tmp_path / "c", trusted_proxy_hops=1)
    store = LocalStore(s.data_dir)
    c = Collector(s, store, geo=CountryResolver(reader=Reader()))
    app = create_app(s, c)
    client = TestClient(app)
    hdr = {**KEY, "X-Forwarded-For": "10.1.1.1, 8.8.4.4"}
    assert client.post("/v2/batch", json=batch([ev(0)]), headers=hdr).json()["accepted"] == 1
    bad = client.post(
        "/v2/batch", content=b"{", headers={**hdr, "Content-Type": "application/json"}
    )
    assert bad.status_code == 400
    store.close()
    assert rows(s.data_dir)[0]["country"] == "US"
    blob = b"".join(p.read_bytes() for p in s.data_dir.rglob("*") if p.is_file())
    assert b"8.8.4.4" not in blob and b"10.1.1.1" not in blob


def test_live_window_counts_distinct_installs_for_five_minutes(col):
    client, c, clock, _ = col
    for i in range(3):
        post(
            client,
            batch(
                [ev(0, ts=clock.ms - 1000)], inst=i + 1, install_store="app_store", platform="ios"
            ),
        )
    post(client, batch([ev(0, ts=clock.ms)], inst=9, app="com.example.other"))
    # an old event flushed from an offline queue does not make an install active
    post(client, batch([ev(0, ts=clock.ms - 3_600_000)], inst=10))
    snap = client.get("/live").json()
    by = {a["app_id"]: a for a in snap["apps"]}
    assert by["com.example.game"]["active"] == 3 and by["com.example.other"]["active"] == 1
    assert snap["total_active"] == 4
    assert by["com.example.game"]["by"] == [
        {
            "platform": "ios",
            "store": "app_store",
            "country": "unknown",
            "build_env": "release",
            "active": 3,
        }
    ]
    assert sum(by["com.example.game"]["events_per_min"]) == 3
    assert {n["app_id"] for n in snap["new_apps"]} == {"com.example.game", "com.example.other"}
    clock.ms += 4 * 60_000
    assert client.get("/live").json()["total_active"] == 4
    clock.ms += 2 * 60_000  # 6 minutes after the last event
    snap = client.get("/live").json()
    assert snap["total_active"] == 0 and {a["app_id"] for a in snap["apps"]} == {
        "com.example.game", "com.example.other"}  # fmt: skip


def test_live_needs_the_read_key_when_configured(tmp_path):
    s = CollectorSettings(data_dir=tmp_path / "c", read_key="r3ad")
    store = LocalStore(s.data_dir)
    client = TestClient(create_app(s, Collector(s, store, clock=Clock())))
    assert client.get("/live").status_code == 401
    assert client.get("/live", headers={"X-Pulse-Read-Key": "r3ad"}).status_code == 200
    h = client.get("/healthz").json()
    assert h["ok"] and "r3ad" not in json.dumps(h) and DEFAULT_LOCAL_KEY not in json.dumps(h)
    store.close()


def test_durable_write_happens_before_the_answer(col, monkeypatch):
    client, *_ = col
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    assert post(client, batch([ev(0)])).status_code == 200
    assert calls, "the segment was not fsync'ed before the 2xx"


def test_storage_failure_answers_503(col, monkeypatch):
    client, c, *_ = col

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(c.store, "write", boom)
    r = post(client, batch([ev(0)]))
    assert r.status_code == 503
    monkeypatch.undo()
    assert post(client, batch([ev(0)])).json()["accepted"] == 1  # the retry is not a duplicate


def test_rate_limit_answers_429(tmp_path):
    s = CollectorSettings(data_dir=tmp_path / "c", rate_per_ip=0.001, burst_per_ip=2)
    store = LocalStore(s.data_dir)
    client = TestClient(create_app(s, Collector(s, store, clock=Clock())))
    codes = [post(client, batch([ev(i)])).status_code for i in range(4)]
    assert codes[:2] == [200, 200] and codes[2:] == [429, 429]
    store.close()


def test_v1_json_lines_are_upgraded(col):
    client, c, _, d = col
    line = json.dumps({"v": 1, "event": "pulse_load", "ts": "2026-10-26T08:00:00.000Z",
                       "app": "com.example.game", "app_version": "1.4.0", "platform": "ANDROID",
                       "session": "0123456789abcdef0123456789abcdef",
                       "params": {"pulse_v": 1, "tier": "low", "kind": "cold_start", "scene": "Menu",
                                  "duration_ms": 3912.25}})  # fmt: skip
    body = (line + "\n" + "{broken\n").encode()
    r = client.post(
        "/v1/ndjson", content=body, headers={**KEY, "Content-Type": "application/x-ndjson"}
    )
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["quarantined"] == 1
    (row,) = rows(d)
    assert row["schema"] == "1" and row["name"] == "pulse_load" and row["platform"] == "android"
    assert row["ts"] == 1793001600000 and row["sid"] == "01234567-89ab-cdef-0123-456789abcdef"
    r = client.post("/v1/ndjson", content=line.encode(), headers=KEY)
    assert r.json()["duplicates"] == 1


def test_settings_from_config_block_and_environment(tmp_path):
    s = from_dict(
        {"data_dir": "x", "write_keys": ["k1", "k2"], "bundle_prefixes": "com.a.,com.b."},
        tmp_path,
        env={},
    )
    assert s.data_dir == tmp_path / "x" and s.write_keys == ["k1", "k2"]
    assert s.bundle_prefixes == ["com.a.", "com.b."] and not s.uses_default_key
    env = {"PULSE_WRITE_KEYS": "e1", "PULSE_COLLECTOR_MODE": "bigquery", "PULSE_BQ_PROJECT": "p",
           "PULSE_BQ_DATASET": "d", "PULSE_TRUSTED_PROXY_HOPS": "1"}  # fmt: skip
    s = from_dict({"data_dir": "x"}, tmp_path, env=env)
    assert (
        s.write_keys == ["e1"]
        and s.mode == "bigquery"
        and s.bigquery == {"project": "p", "dataset": "d"}
    )
    assert s.trusted_proxy_hops == 1
    with pytest.raises(ValueError):
        CollectorSettings(data_dir=tmp_path, mode="kafka")


def test_decode_body_limits():
    assert core.decode_body(b"abc", None, 10) == b"abc"
    with pytest.raises(core.HTTPError) as e:
        core.decode_body(gzip.compress(b"a" * 100), "gzip", 50)
    assert e.value.status == 413
