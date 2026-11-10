"""Client performance percentiles and load times (hand-computed)."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import D0, run_models


def add_window(con, tier, p50, p95, frames, slow, mem, day=D0, version="1.0", reserved=None):
    con.execute(
        "INSERT INTO raw_perf_windows VALUES ('g', ?, 'ANDROID', ?, ?, 'Level', 30, ?, ?, ?, NULL, ?, ?, ?, 'unity', now())",
        [day, version, tier, frames, p50, p95, slow, mem, reserved],
    )


@pytest.fixture()
def perf(con):
    for p50, p95, slow, mem in ((18, 30, 10, 200), (20, 34, 20, 300), (22, 36, 30, 400), (30, 50, 40, 500)):
        add_window(con, "low", p50, p95, 1000, slow, mem)
    add_window(con, "high", 16.0, 18.0, 2000, 0, 450)
    for kind, ms in (("cold_start", 3000), ("cold_start", 5000), ("cold_start", 4000), ("scene_load", 800)):
        con.execute("INSERT INTO raw_load_times VALUES ('g', ?, 'ANDROID', '1.0', 'low', ?, 'Boot', ?, 'unity', now())", [D0, kind, ms])
    run_models(con)
    return con


def test_perf_by_tier(perf):
    r = {row[0]: row[1:] for row in perf.execute(
        "SELECT device_tier, windows, frames, ft_p50_ms, ft_p95_ms, slow_frame_share, mem_alloc_p95_mb FROM fct_perf_by_tier").fetchall()}
    low = r["low"]
    assert low[:2] == (4, 4000)
    assert low[2] == pytest.approx(21.0)  # median of 18, 20, 22, 30
    assert low[3] == pytest.approx(35.0)  # median of 30, 34, 36, 50
    assert low[4] == pytest.approx(100 / 4000)
    assert low[5] == pytest.approx(485.0)  # p95 (linear): 400 + 0.85 * (500 - 400)
    assert r["high"][2] == pytest.approx(16.0)
    allr = r["all"]
    assert allr[0] == 5 and allr[1] == 6000
    assert allr[2] == pytest.approx(20.0)  # median of 16, 18, 20, 22, 30
    assert allr[4] == pytest.approx(100 / 6000)


def test_perf_daily_and_by_version(perf):
    add_window(perf, "low", 26.0, 40.0, 1000, 50, 300, day=D0 + dt.timedelta(days=1), version="1.1")
    run_models(perf)
    d = perf.execute("SELECT date, ft_p50_ms FROM fct_perf_daily WHERE device_tier = 'low' ORDER BY date").fetchall()
    assert d == [(D0, pytest.approx(21.0)), (D0 + dt.timedelta(days=1), pytest.approx(26.0))]
    v = dict(perf.execute("SELECT app_version, ft_p50_ms FROM fct_perf_by_version WHERE device_tier = 'low'").fetchall())
    assert v == {"1.0": pytest.approx(21.0), "1.1": pytest.approx(26.0)}


def test_load_times(perf):
    r = {(k, s): (n, p50) for k, s, n, p50 in perf.execute("SELECT kind, scene, samples, p50_ms FROM fct_load_times").fetchall()}
    assert r[("cold_start", "Boot")] == (3, pytest.approx(4000.0))
    assert r[("scene_load", "Boot")] == (1, pytest.approx(800.0))


def test_implausible_samples_are_dropped(con):
    add_window(con, "low", 0.0, 1.0, 1000, 0, 100)       # zero frame time
    add_window(con, "low", 20.0, 30.0, 0, 0, 100)        # zero frames
    con.execute("INSERT INTO raw_load_times VALUES ('g', ?, 'ANDROID', '1', 'low', 'cold_start', 'Boot', 900000, 'u', now())", [D0])
    run_models(con)
    assert con.execute("SELECT COUNT(*) FROM stg_perf_windows").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM stg_load_times").fetchone()[0] == 0
