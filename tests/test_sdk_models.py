"""SDK events -> collector -> pulse_sdk source -> models, with exact expected numbers.

A handful of hand-made sessions for a puzzle game go through the real
collector code (TestClient, fake clock), then `pulse run` loads them and builds
every model. Each assertion is a number computed by hand from the script below.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from studio_pulse import cli
from studio_pulse.collector.app import create_app
from studio_pulse.collector.core import Collector
from studio_pulse.collector.ingest import purge
from studio_pulse.collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings
from studio_pulse.collector.store import LocalStore, connect_db

D0 = dt.date(2026, 10, 1)
KEY = {"X-Pulse-Key": DEFAULT_LOCAL_KEY}


def ms(day: int, hh: int, mm: int = 0, ss: int = 0) -> int:
    t = dt.datetime.combine(D0 + dt.timedelta(days=day), dt.time(hh, mm, ss), dt.UTC)
    return int(t.timestamp() * 1000)


class Device:
    def __init__(self, n: int, platform: str, store: str, env: str = "release") -> None:
        self.install = str(uuid.UUID(int=n, version=4))
        self.platform, self.store, self.env = platform, store, env
        self.seq = 0
        self.n = n
        self.sid = ""
        self.snum = 0
        self.t0 = 0
        self.events: list[dict] = []

    def session(self, t: int) -> str:
        self.snum += 1
        self.sid = str(uuid.UUID(int=(self.n << 32) + self.snum, version=4))
        self.t0 = t
        return self.sid

    def e(self, name: str, t: int, skip_seq: bool = False, **p) -> dict:
        if skip_seq:
            self.seq += 1  # this sequence number is lost on the way
        ev = {"id": str(uuid.UUID(int=(self.n << 64) + self.seq, version=4)), "seq": self.seq,
              "name": name, "ts": t, "sid": self.sid, "snum": self.snum, "fg_ms": t - self.t0, "p": p}  # fmt: skip
        self.seq += 1
        self.events.append(ev)
        return ev

    def batch(self) -> dict:
        evs, self.events = self.events, []
        return {"v": 2, "schema": "2.0", "sent_at": max(e["ts"] for e in evs) + 1000,
                "ctx": {"app_id": "com.test.puzzle", "platform": self.platform, "app_version": "1.0.0",
                        "install_id": self.install, "sdk": "unity/2.0.0", "build_env": self.env,
                        "install_store": self.store, "app_name": "Test Puzzle"},
                "events": evs}  # fmt: skip


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("sdk")
    data = root / "collector"
    clock = [0]
    settings = CollectorSettings(data_dir=data, country_header="CF-IPCountry", rate_per_ip=0)
    store = LocalStore(data)
    client = TestClient(create_app(settings, Collector(settings, store, clock=lambda: clock[0])))

    def send(dev: Device, country: str, *, twice: bool = False) -> dict:
        b = dev.batch()
        clock[0] = b["sent_at"]  # no clock skew
        r = client.post("/v2/batch", json=b, headers={**KEY, "CF-IPCountry": country}).json()
        if twice:
            client.post("/v2/batch", json=b, headers={**KEY, "CF-IPCountry": country})
        return r

    a = Device(1, "android", "google_play")
    b = Device(2, "ios", "app_store")
    c = Device(3, "android", "unknown", env="debug")
    d = Device(4, "android", "google_play")
    # -- A, day 0: install; session 1 (2 minutes) with a fail and a win on level 1
    a.session(ms(0, 10))
    a.e(
        "pulse_install",
        ms(0, 10),
        install_store="google_play",
        installer_raw="com.android.vending",
        restored=False,
    )
    a.e("pulse_session_start", ms(0, 10), cold=True)
    a.e("level_start", ms(0, 10, 0, 1), level=1, attempt=1)
    a.e("level_fail", ms(0, 10, 0, 30), level=1, attempt=1, duration_ms=29000, reason="out_of_moves",
        progress=0.4, pos_x=0.15, pos_y=0.85, pos_space="norm")  # fmt: skip
    a.e("level_start", ms(0, 10, 0, 31), level=1, attempt=2)
    a.e("level_complete", ms(0, 10, 1, 30), level=1, attempt=2, duration_ms=59000, score=900)
    a.e("setting_change", ms(0, 10, 1, 35), setting="music", to="off", **{"from": "on"})
    a.e("purchase", ms(0, 10, 1, 40), transaction_id="t1", product_id="coins_s", price_micros=49_990_000,
        currency="TRY", is_restore=False, is_sandbox=False)  # fmt: skip
    a.e(
        "ad_impression",
        ms(0, 10, 1, 45),
        ad_platform="max",
        ad_format="rewarded",
        value=0.01,
        currency="USD",
    )
    a.e("pulse_perf", ms(0, 10, 1, 50), pulse_v=1, tier="low", scene="Level", window_s=30.0, frames=1700,
        ft_src="ftm", ft_p50_ms=19.5, ft_p95_ms=28.0, ft_max_ms=60.0, slow_frames=12)  # fmt: skip
    a.e("pulse_background", ms(0, 10, 2), fg_ms=120000)
    send(a, "TR", twice=True)  # a retry: the collector drops the repeat
    # session 2: starts level 2 and the app dies (no outcome) -> an inferred quit
    a.session(ms(0, 11))
    a.e("pulse_session_start", ms(0, 11), cold=True)
    a.e("level_start", ms(0, 11, 0, 5), level=2, attempt=1, skip_seq=True)
    send(a, "TR")
    # -- A, day 1: one sid, but 40 minutes between events: two sessions
    a.session(ms(1, 9))
    a.e("pulse_session_start", ms(1, 9), cold=True)
    a.e("level_start", ms(1, 9, 0, 10), level=2, attempt=2)
    a.e("level_fail", ms(1, 9, 1), level=2, attempt=2, duration_ms=50000, reason="out_of_time",
        pos_x=0.55, pos_y=0.45)  # fmt: skip
    a.e("pulse_heartbeat", ms(1, 9, 41), fg_ms=60000)
    send(a, "TR")
    # -- B, day 0: wins level 1 first try, one non-fatal error; crashes (reported on day 2)
    sb = b.session(ms(0, 20))
    b.e("pulse_install", ms(0, 20), install_store="app_store", installer_raw=None, restored=False)
    b.e("pulse_session_start", ms(0, 20), cold=True)
    b.e("level_start", ms(0, 20, 0, 5), level=1, attempt=1)
    b.e("level_complete", ms(0, 20, 0, 50), level=1, attempt=1, duration_ms=45000)
    b.e("pulse_error", ms(0, 20, 1), type="NullReferenceException", fp="abc", fatal=False)
    send(b, "US")
    b.session(ms(2, 8))
    b.e("pulse_session_start", ms(2, 8), cold=True)
    b.e("pulse_exit", ms(2, 8, 0, 1), reason="crash", prev_sid=sb)
    b.e("level_start", ms(2, 8, 0, 9), level=2, attempt=1)
    b.e("bad_event", ms(2, 8, 0, 10), note="mail x@y.co")  # quarantined (e-mail)
    send(b, "US")
    # -- C: a developer build, not in any product metric
    c.session(ms(0, 12))
    c.e("pulse_install", ms(0, 12), install_store="unknown", installer_raw=None, restored=False)
    c.e("level_start", ms(0, 12, 0, 1), level=1, attempt=1)
    send(c, "DE")
    # -- D: plays, then asks for deletion
    d.session(ms(0, 13))
    d.e("pulse_install", ms(0, 13), install_store="google_play", installer_raw=None, restored=False)
    d.e("level_start", ms(0, 13, 0, 1), level=1, attempt=1)
    send(d, "TR")
    r = client.post(
        "/v2/delete", json={"install_id": d.install, "reason": "user_request"}, headers=KEY
    )
    assert r.status_code == 202
    store.close()
    # a crash between the segment write and the dedupe index: a raw repeat of one A row
    seg = sorted((data / "events").glob("dt=*/*.ndjson"))[0]
    first = seg.read_text(encoding="utf-8").splitlines()[0]
    with open(seg, "a", encoding="utf-8") as f:
        f.write(first + "\n")
    cfg = {
        "title": "SDK model test",
        "warehouse": "w.duckdb",
        "site_dir": "site",
        "lookback_days": 5,
        "end_date": (D0 + dt.timedelta(days=2)).isoformat(),
        "as_of": (D0 + dt.timedelta(days=3)).isoformat(),
        "fx_rates": {"TRY": 0.029},
        "collector": {"data_dir": "collector"},
        "apps": [
            {
                "id": "puzzle",
                "name": "Test Puzzle",
                "genre": "puzzle",
                "platforms": ["ANDROID", "IOS"],
                "sources": {"pulse_sdk": {"bundle_ids": ["com.test.puzzle"]}},
            }
        ],  # fmt: skip
    }
    path = root / "pulse.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert cli.main(["run", "-c", str(path)]) == 0
    con = duckdb.connect(str(root / "w.duckdb"), read_only=True)
    yield {"con": con, "root": root, "data": data, "a": a, "b": b, "d": d}
    con.close()


def one(con, sql, *params):
    return con.execute(sql, list(params)).fetchall()


def test_raw_load_dedupes_and_leaves_out_deleted_installs(world):
    con, d = world["con"], world["d"]
    raw = one(con, "SELECT COUNT(*) FROM raw_pulse_events")[0][0]
    stg = one(con, "SELECT COUNT(*) FROM stg_pulse_events")[0][0]
    assert raw == stg + 1  # the planted repeat
    assert (
        one(con, "SELECT COUNT(*) FROM raw_pulse_events WHERE install_id = ?", d.install)[0][0] == 0
    )
    assert one(con, "SELECT COUNT(*) FROM stg_pulse_events WHERE NOT is_prod")[0][0] == 2  # C
    assert one(con, "SELECT DISTINCT country FROM stg_pulse_events WHERE platform = 'IOS'") == [
        ("US",)
    ]


def test_daily_users_sessions_and_revenue(world):
    con = world["con"]
    rows = one(con, "SELECT date, dau, new_users, sessions, crashed_users, iap_revenue_usd, ad_revenue_usd, payers "
                    "FROM fct_app_daily WHERE app_id = 'puzzle' ORDER BY date")  # fmt: skip
    d0, d1, d2 = rows
    assert d0[:5] == (D0, 2, 2, 3, 1)  # A twice + B once; B's session crashed
    assert d0[5] == pytest.approx(49.99 * 0.029) and d0[6] == pytest.approx(0.01) and d0[7] == 1
    assert d1[:4] == (D0 + dt.timedelta(days=1), 1, 0, 2)  # the 40-minute gap split the sid
    assert d2[:4] == (D0 + dt.timedelta(days=2), 1, 0, 1)
    plat = one(
        con,
        "SELECT platform, dau FROM stg_active_users WHERE app_id = 'puzzle' AND date = ? ORDER BY 1",
        D0,
    )
    assert plat == [("ANDROID", 1), ("IOS", 1)]
    lengths = one(
        con,
        "SELECT length_ms FROM stg_sdk_sessions WHERE install_id = ? ORDER BY started_at",
        world["a"].install,
    )
    assert lengths[0][0] == 120000 and lengths[1][0] == 5000
    assert sorted(x[0] for x in lengths[2:]) == [0, 60000]  # day 1: the split session from fg_ms


def test_retention_cohorts(world):
    con = world["con"]
    ret = dict(
        one(
            con,
            "SELECT day_n, retention FROM fct_retention WHERE app_id = 'puzzle' AND cohort_date = ? AND day_n <= 2",
            D0,
        )
    )
    assert ret == {0: 1.0, 1: 0.5, 2: 0.5}


def test_level_funnel_with_attempts_and_inferred_quits(world):
    con = world["con"]
    f = {r[0]: r[1:] for r in one(con, "SELECT level, starts, completes, fails, fail_rate, avg_attempts_to_win, quit_inferred "
                                       "FROM fct_sdk_level_funnel WHERE app_id = 'puzzle'")}  # fmt: skip
    assert (
        f[1][:3] == (3, 2, 1)
        and f[1][3] == pytest.approx(1 / 3)
        and f[1][4] == 1.5
        and f[1][5] == 0
    )
    assert f[2][:3] == (3, 0, 1) and f[2][5] == 2  # A's dead session 2 and B's crash-day session
    # the existing funnel model shows the SDK app too
    old = dict(one(con, "SELECT level, starts FROM fct_level_funnel WHERE app_id = 'puzzle'"))
    assert old == {1: 3, 2: 3}


def test_death_heatmap_bins(world):
    con = world["con"]
    h = one(
        con,
        "SELECT level, bin_x, bin_y, fails FROM fct_sdk_fail_heatmap WHERE app_id = 'puzzle' ORDER BY level",
    )
    assert h == [(1, 1, 8, 1), (2, 5, 4, 1)]


def test_settings_share_and_when(world):
    con = world["con"]
    s = one(
        con,
        "SELECT setting, players, turned_off_players, off_share, off_in_first_session FROM fct_sdk_settings",
    )
    assert s == [("music", 2, 1, 0.5, 1.0)]
    assert one(con, "SELECT session_bucket, players FROM fct_sdk_settings_when") == [("1", 1)]


def test_errors_perf_and_purchases(world):
    con = world["con"]
    e = one(
        con,
        "SELECT sessions, errors, errors_per_session, crashed_sessions FROM fct_sdk_errors_daily WHERE date = ?",
        D0,
    )
    assert e[0][:2] == (3, 1) and e[0][2] == pytest.approx(1 / 3) and e[0][3] == 1
    assert one(con, "SELECT fingerprint, events, installs FROM fct_sdk_errors") == [("abc", 1, 1)]
    perf = one(
        con, "SELECT device_tier, ft_p50_ms, frames FROM stg_perf_windows WHERE app_id = 'puzzle'"
    )
    assert perf == [("low", 19.5, 1700)]
    p = one(con, "SELECT product_id, purchases, payers, revenue_usd FROM fct_sdk_purchases")
    assert p[0][:3] == ("coins_s", 1, 1) and p[0][3] == pytest.approx(1.44971)


def test_data_quality_numbers(world):
    con = world["con"]
    dq = one(
        con,
        "SELECT duplicate_rows, seq_expected, seq_received, quarantined, skew_p95_ms FROM fct_sdk_data_quality",
    )
    dup, expected, received, quarantined, skew = dq[0]
    # A's lost seq; B's quarantined event is its install's last seq, so not a gap
    assert dup == 1 and expected - received == 1
    assert quarantined == 1 and skew == 0
    reasons = one(con, "SELECT reason, events FROM raw_pulse_quarantine")
    assert reasons == [("pii_email", 1)]
    checks = one(
        con,
        "SELECT name, status, severity FROM meta_check_results WHERE name LIKE 'sdk_%' ORDER BY name",
    )
    # 1 repeat and 1 quarantined event out of 28 are above 1%: the warn-level checks fire;
    # the loss check is only judged from 2,000 expected events up
    failing = {(n, sev) for n, st, sev in checks if st != "pass"}
    assert failing == {("sdk_duplicates_below_1pct", "warn"), ("sdk_quarantine_below_1pct", "warn")}


def test_registry_reaches_the_warehouse(world):
    con = world["con"]
    rows = one(
        con,
        "SELECT platform, status, display_name FROM fct_sdk_apps WHERE app_id = 'puzzle' ORDER BY 1",
    )
    assert rows == [("android", "testing", "Test Puzzle"), ("ios", "testing", "Test Puzzle")]


def test_purge_removes_deleted_installs_from_segments_after_two_hours(world, tmp_path):
    import shutil

    data = tmp_path / "copy"
    shutil.copytree(world["data"], data)
    inst = world["d"].install
    blob = lambda: b"".join(p.read_bytes() for p in (data / "events").rglob("*.ndjson"))  # noqa: E731
    assert inst.encode() in blob()
    stats = purge(data, log=lambda m: None)  # just written: still "active", left alone
    assert stats["removed_rows"] == 0 and inst.encode() in blob()
    stats = purge(data, now=time.time() + 3 * 3600, log=lambda m: None)
    assert stats["removed_rows"] == 2 and stats["requests_done"] == 1
    assert inst.encode() not in blob()
    con = connect_db(data / "collector.sqlite3")
    assert con.execute("SELECT status FROM deletion_requests").fetchone()[0] == "done"
    con.close()
    # 13 months later everything is gone
    stats = purge(data, now=time.time() + 400 * 86400, log=lambda m: None)
    assert stats["expired_days"] >= 3 and not list((data / "events").rglob("*.ndjson"))


def test_dates_follow_the_app_time_zone(world, tmp_path):
    root = world["root"]
    data = yaml.safe_load((root / "pulse.yaml").read_text(encoding="utf-8"))
    data["apps"][0]["timezone"] = "America/Los_Angeles"
    data["warehouse"] = str(tmp_path / "w.duckdb")
    data["collector"]["data_dir"] = str(world["data"])
    data["site_dir"] = str(tmp_path / "site")
    data["state_dir"] = str(tmp_path / "state")
    p = tmp_path / "pulse.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert cli.main(["ingest", "-c", str(p)]) == 0
    con = duckdb.connect(str(tmp_path / "w.duckdb"), read_only=True)
    # B's day-0 session at 20:00 UTC is 13:00 the same day in Los Angeles; A's 09:00 UTC on day 1 is 02:00
    rows = dict(con.execute("SELECT name, date FROM raw_pulse_events WHERE install_id = ? AND name = 'pulse_install'",
                            [world["b"].install]).fetchall())  # fmt: skip
    assert rows["pulse_install"] == D0
    early = con.execute(
        "SELECT MIN(date) FROM raw_pulse_events WHERE install_id = ? AND ts >= ?",
        [world["a"].install, dt.datetime(2026, 10, 2, 9)],
    ).fetchone()[0]
    assert early == D0 + dt.timedelta(days=1)
    con.close()


def test_ingest_config_errors(tmp_path):
    from studio_pulse.config import ConfigError

    cfg = {"warehouse": "w.duckdb", "apps": [{"id": "x", "sources": {"pulse_sdk": {}}}]}
    p = tmp_path / "pulse.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from studio_pulse import pipeline, warehouse
    from studio_pulse.config import load_config

    c = load_config(p)
    con = warehouse.connect(":memory:")
    rep = pipeline.ingest(con, c, log=lambda m: None)
    assert ConfigError.__name__ in rep.errors["x.pulse_sdk"]
    con.close()
