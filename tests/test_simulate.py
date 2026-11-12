"""`pulse simulate`: reproducible SDK traffic through a real collector over HTTP."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from studio_pulse import cli
from studio_pulse.collector.inprocess import running_collector
from studio_pulse.collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings
from studio_pulse.collector.store import LocalStore
from studio_pulse.simulate.model import GAMES
from studio_pulse.simulate.run import (
    Poster,
    Report,
    SimClock,
    backfill,
    expected_live,
    live,
    verify_live,
)

START = dt.date(2026, 10, 1)


def _lines(data: Path) -> list[str]:
    out: list[str] = []
    for f in (data / "events").glob("dt=*/*.ndjson"):
        out += f.read_text(encoding="utf-8").splitlines()
    return sorted(out)


def _backfill(data: Path, days: int = 6) -> tuple[Report, int]:
    t0 = int(dt.datetime.combine(START, dt.time(), dt.UTC).timestamp() * 1000)
    clock = SimClock(t0)
    s = CollectorSettings(data_dir=data, country_header="CF-IPCountry")
    with running_collector(s, clock) as (url, col):
        rep = backfill(Poster(url, DEFAULT_LOCAL_KEY, clock=clock), start=START, days=days,
                       games=GAMES, scale=0.3, seed=5, log=lambda m: None)  # fmt: skip
        accepted = col.counters.accepted
        assert col.counters.duplicates + col.counters.quarantined >= rep.sent.resent_events
        assert col.counters.quarantined >= rep.sent.invalid
    return rep, accepted


def test_backfill_is_reproducible_and_fully_accounted(tmp_path):
    rep, accepted = _backfill(tmp_path / "a")
    rep2, accepted2 = _backfill(tmp_path / "b")
    assert accepted == accepted2 > 1000
    assert _lines(tmp_path / "a") == _lines(tmp_path / "b")  # byte for byte, any OS
    assert accepted == rep.sent.events - rep.sent.invalid
    # a run shorter than 12 days has the late game from its first day
    assert set(rep.by_app_events) == {g.app_id for g in GAMES}
    store = LocalStore(tmp_path / "a")
    assert {r["app_id"] for r in store.registry()} == {g.app_id for g in GAMES}
    store.close()
    rows = [json.loads(x) for x in _lines(tmp_path / "a")[:200]]
    assert {r["country"] for r in rows} - {"unknown"} and all(
        r["skew_ms"] is not None for r in rows
    )


def test_a_new_game_registers_mid_run(tmp_path):
    t0 = int(dt.datetime.combine(START, dt.time(), dt.UTC).timestamp() * 1000)
    clock = SimClock(t0)
    s = CollectorSettings(data_dir=tmp_path / "c", country_header="CF-IPCountry")
    with running_collector(s, clock) as (url, col):
        backfill(Poster(url, DEFAULT_LOCAL_KEY, clock=clock), start=START, days=16, games=GAMES,
                 scale=0.15, seed=9, log=lambda m: None)  # fmt: skip
        reg = {(r["app_id"], r["platform"]): r for r in col.store.registry()}
    lantern = reg[("demo.lanternleap", "android")]
    first = dt.datetime.fromtimestamp(lantern["first_seen_ms"] / 1000, dt.UTC).date()
    assert first == START + dt.timedelta(days=16 - 12)  # starts 12 days before the end
    # developer builds first (status new), then a Google Play release (testing)
    assert (
        lantern["status"] == "testing" and lantern["status_changed_ms"] > lantern["first_seen_ms"]
    )
    assert lantern["display_name"] == "Lantern Leap" and lantern["genre"] == "game"


def test_live_mode_matches_the_collector_live_view(tmp_path):
    s = CollectorSettings(data_dir=tmp_path / "c", country_header="CF-IPCountry")
    with running_collector(s) as (url, col):
        poster = Poster(url, DEFAULT_LOCAL_KEY)
        rep = live(poster, duration_s=4, players=12, games=GAMES, new_game_after_s=1.5,
                   pace=0.02, first_within_s=1.0, log=lambda m: None)  # fmt: skip
        v = verify_live(url, rep)
    assert v["ok"], v
    apps = {a["app_id"]: a for a in v["apps"]}
    assert set(apps) == {"demo.pebblepop", "demo.tidyhabit", "demo.lanternleap"}
    assert sum(a["live"] for a in apps.values()) == sum(rep.installs.values())
    assert "demo.lanternleap" in {n["app_id"] for n in v["new_apps"]}
    assert v["collector"]["accepted"] == rep.sent.events - rep.sent.invalid


def test_expected_live_marks_installs_on_the_window_edge_as_ambiguous():
    rep = Report()
    key = ("android", "google_play", "TR", "release")
    now = 1_000_000_000
    rep.last_seen = {
        "a": ("x", now - 10_000, key),
        "b": ("x", now - 300_500, key),
        "c": ("x", now - 400_000, key),
    }
    exp = expected_live(rep, now)
    assert exp["sure"] == {"x": 1} and exp["maybe"] == {"x": 1} and exp["by"] == {"x": {key: 1}}


def test_simulate_cli_backfill_live_and_verify(tmp_path, capsys):
    s = CollectorSettings(data_dir=tmp_path / "c", country_header="CF-IPCountry")
    report = tmp_path / "sim.json"
    with running_collector(s) as (url, _):
        rc = cli.main(["simulate", "--url", url, "--backfill-days", "2", "--scale", "0.2",
                       "--duration", "3", "--players", "9", "--pace", "0.02", "--ramp", "1", "--new-game-after", "1",
                       "--verify", "--report", str(report)])  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 0, out
    r = json.loads(report.read_text(encoding="utf-8"))
    assert r["accounted"] and r["accepted_matches"] and r["verify"]["ok"]
    assert r["modes"] == ["backfill", "live"] and "MISMATCH" not in out
    live_apps = {a["app_id"]: a["live"] for a in r["verify"]["apps"]}
    assert live_apps["demo.lanternleap"] > 0  # the new game appeared during the live run
