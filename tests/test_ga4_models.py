"""GA4 extraction (DuckDB dialect) + engagement/retention/revenue models.

The fixture is small enough to verify every number by hand; see the comments.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import D0, GA4Builder, run_models

from studio_pulse.connectors import ga4
from studio_pulse.connectors.dialect import DuckDBDialect
from studio_pulse.connectors.executors import DuckDBExecutor


def day(n: int) -> dt.date:
    return D0 + dt.timedelta(days=n)


@pytest.fixture()
def loaded(con, tmp_path):
    b = GA4Builder()
    # Cohort Oct 1: A, B, C, D.  Cohort Oct 2: E.
    b.add(0, "first_open", "A").add(0, "session_start", "A", ga_session_id=1).add(
        0, "session_start", "A", ga_session_id=2
    )
    b.add(0, "ad_impression", "A", ga_session_id=1, value_usd=0.01, value=0.01)
    b.add(0, "ad_impression", "A", ga_session_id=2, value_usd=0.02, value=0.02)
    b.add(1, "session_start", "A", ga_session_id=3)
    b.add(7, "session_start", "A", ga_session_id=4)
    b.add(0, "first_open", "B").add(0, "session_start", "B", ga_session_id=10)
    b.add(1, "session_start", "B", ga_session_id=11).add(
        1, "in_app_purchase", "B", ga_session_id=11, value_usd=4.99
    )
    b.add(0, "first_open", "C").add(0, "session_start", "C", ga_session_id=20)
    b.add(0, "app_exception", "C", ga_session_id=20, fatal=1)
    b.add(0, "first_open", "D").add(0, "session_start", "D", ga_session_id=30)
    b.add(1, "first_open", "E", first_touch_day=1).add(
        1, "session_start", "E", first_touch_day=1, ga_session_id=40
    )
    b.add(2, "session_start", "E", first_touch_day=1, ga_session_id=41)
    # F only receives a notification (passive) -> never active.
    b.add(2, "notification_receive", "F", first_touch_day=-10)
    # G is an old user (installed long before the window) -> active, but no cohort in the window.
    b.add(3, "session_start", "G", first_touch_day=-40, ga_session_id=60)
    # Another app in the same property must be filtered out by app_info.id.
    b.add(0, "first_open", "Z", app="com.example.other").add(
        0, "session_start", "Z", app="com.example.other", ga_session_id=99
    )
    path = b.write(tmp_path / "events.parquet")
    ex = DuckDBExecutor({"ga4_events": path})
    opts = ga4.GA4Options(app_info_ids=["com.example.app"])
    ga4.ingest(
        con,
        app_id="demo",
        source="ga4_files",
        dialect=DuckDBDialect(),
        executor=ex,
        start=day(0),
        end=day(8),
        opts=opts,
        log=lambda *_: None,
    )
    con.execute(
        "INSERT INTO meta_apps (app_id, name, genre, platforms, is_demo) VALUES ('demo', 'Demo', 'puzzle', 'ANDROID', TRUE)"
    )
    run_models(con)
    return con


def daily(con, d: dt.date) -> dict:
    cur = con.execute("SELECT * FROM fct_app_daily WHERE app_id = 'demo' AND date = ?", [d])
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, cur.fetchone(), strict=True))


def test_dau_new_users_sessions(loaded):
    d0 = daily(loaded, day(0))
    assert d0["dau"] == 4  # A, B, C, D (Z belongs to another app)
    assert d0["new_users"] == 4
    assert d0["sessions"] == 5  # A has two sessions
    assert d0["sessions_per_dau"] == pytest.approx(1.25)
    assert daily(loaded, day(1))["dau"] == 3  # A, B, E
    assert daily(loaded, day(2))["dau"] == 1  # E only; F's notification is passive
    assert daily(loaded, day(3))["dau"] == 1  # G


def test_wau_mau_only_with_enough_history(loaded):
    d7 = daily(loaded, day(7))
    # Oct 2..Oct 8: A, B, E (Oct 2), E (Oct 3), G (Oct 4), A (Oct 8) -> 4 distinct users.
    assert d7["wau"] == 4
    assert d7["mau"] is None  # only 8 days of history
    assert daily(loaded, day(3))["wau"] is None  # 4 days of history < 7


def test_crash_free_from_ga4_app_exception(loaded):
    d0 = daily(loaded, day(0))
    assert d0["crash_basis"] == "ga4_app_exception"
    assert d0["crash_free_users"] == pytest.approx(1 - 1 / 4)
    assert d0["crash_free_sessions"] == pytest.approx(1 - 1 / 5)


def test_revenue_arpdau_payers(loaded):
    d0, d1 = daily(loaded, day(0)), daily(loaded, day(1))
    assert d0["ad_revenue_usd"] == pytest.approx(0.03)
    assert d0["iap_basis"] == "ga4_gross"
    assert d1["iap_revenue_usd"] == pytest.approx(4.99)
    assert d1["arpdau"] == pytest.approx(4.99 / 3)
    assert d0["arpdau"] == pytest.approx(0.03 / 4)
    assert d1["payers"] == 1
    assert d1["payer_conversion"] == pytest.approx(1 / 3)


def test_retention_matrix(loaded):
    rows = {
        (r[0], r[1]): (r[2], r[3], r[4])
        for r in loaded.execute(
            "SELECT cohort_date, day_n, cohort_size, retention, is_complete FROM fct_retention WHERE app_id = 'demo'"
        ).fetchall()
    }
    assert {c for c, _ in rows} == {day(0), day(1)}  # G's cohort is outside the window
    assert rows[(day(0), 0)] == (4, 1.0, True)
    assert rows[(day(0), 1)][1] == pytest.approx(0.5)  # A, B
    assert rows[(day(0), 2)][1] == 0.0
    assert rows[(day(0), 7)][1] == pytest.approx(0.25)  # A
    assert rows[(day(0), 8)][2] is True and rows[(day(0), 9)][2] is False  # last data day is Oct 9
    assert rows[(day(1), 1)][1] == pytest.approx(1.0)  # E came back on Oct 3


def test_retention_summary_is_cohort_weighted(loaded):
    s = dict(
        loaded.execute(
            "SELECT day_n, retention FROM fct_retention_summary WHERE app_id = 'demo'"
        ).fetchall()
    )
    assert s[1] == pytest.approx((2 + 1) / (4 + 1))
    assert s[7] == pytest.approx((1 + 0) / (4 + 1))


def test_cohort_revenue_and_first_payers(loaded):
    rows = loaded.execute(
        "SELECT cohort_date, day_n, iap_revenue_usd, ad_revenue_usd, new_payers FROM stg_cohort_days "
        "WHERE app_id = 'demo' AND (iap_revenue_usd > 0 OR ad_revenue_usd > 0 OR new_payers > 0) ORDER BY 1, 2"
    ).fetchall()
    assert rows == [
        (day(0), 0, 0.0, pytest.approx(0.03), 0),
        (day(0), 1, pytest.approx(4.99), 0.0, 1),
    ]


def test_reload_is_idempotent(loaded, tmp_path):
    before = loaded.execute("SELECT COUNT(*), SUM(dau) FROM raw_ga4_active_users").fetchone()
    run_models(loaded)
    run_models(loaded)
    assert (
        loaded.execute("SELECT COUNT(*), SUM(dau) FROM raw_ga4_active_users").fetchone() == before
    )
    assert loaded.execute("SELECT COUNT(*) FROM fct_retention").fetchone()[0] == 2 * 31


def test_levels_and_churn(con, tmp_path):
    b = GA4Builder()
    b.add(0, "session_start", "A", ga_session_id=1)
    b.add(0, "level_start", "A", level=1).add(0, "level_complete", "A", level=1)
    b.add(0, "level_start", "A", level=2).add(0, "level_fail", "A", level=2)
    b.add(7, "session_start", "A", ga_session_id=2)  # A is still around on Oct 8
    b.add(0, "level_start", "B", level=1).add(0, "level_fail", "B", level=1)
    b.add(1, "level_start", "B", level=1).add(1, "level_complete", "B", level=1)
    b.add(1, "level_start", "B", level=2)  # B's last activity: Oct 2
    b.add(0, "level_start", "C", level=1)  # C's last activity: Oct 1
    b.add(0, "level_end", "D", level=1, success=1)  # GA4 recommended event, counted as a completion
    ex = DuckDBExecutor({"ga4_events": b.write(tmp_path / "lv.parquet")})
    ga4.ingest(
        con,
        app_id="p",
        source="ga4",
        dialect=DuckDBDialect(),
        executor=ex,
        start=day(0),
        end=day(8),
        opts=ga4.GA4Options(queries=["levels", "level_max"], churn_window_days=7),
        log=lambda *_: None,
    )
    run_models(con)
    f = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT level, starts, completes, fails, win_rate, attempts_per_completion, users_reached, users_churned, churn_rate "
            "FROM fct_level_funnel WHERE app_id = 'p' ORDER BY level"
        ).fetchall()
    }
    # Level 1: starts A, B, B, C = 4; completes A, B, D = 3; fails B = 1.
    assert f[1][:3] == (4, 3, 1)
    assert f[1][3] == pytest.approx(0.75)
    assert f[1][4] == pytest.approx(4 / 3)
    # Highest started level: A=2, B=2, C=1 (D never started one). Churn cutoff Oct 9 - 7 = Oct 2.
    assert f[1][5] == 3 and f[2][5] == 2
    assert f[1][6] == 1 and f[1][7] == pytest.approx(1 / 3)  # C stopped at 1
    assert f[2][6] == 1 and f[2][7] == pytest.approx(1 / 2)  # B stopped at 2, A is active


def test_perf_events_and_fps_mapping_from_ga4(con, tmp_path):
    b = GA4Builder()
    b.add(
        0,
        "pulse_perf",
        "A",
        tier="low",
        scene="Level",
        frames=1000,
        ft_p50_ms=20.0,
        ft_p95_ms=30.0,
        slow_frames=10,
        mem_alloc_mb=300,
    )
    b.add(
        0,
        "pulse_perf",
        "B",
        tier="high",
        scene="Level",
        frames=2000,
        ft_p50_ms=16.0,
        ft_p95_ms=18.0,
        slow_frames=0,
        mem_alloc_mb=400,
    )
    b.add(0, "pulse_load", "A", tier="low", kind="cold_start", scene="Boot", duration_ms=4000.0)
    b.add(0, "camera_session_completed", "C", median_fps=25.0)
    ex = DuckDBExecutor({"ga4_events": b.write(tmp_path / "perf.parquet")})
    opts = ga4.GA4Options(
        queries=["perf", "loads"],
        fps_events=[
            {"event": "camera_session_completed", "fps_param": "median_fps", "scene": "camera"}
        ],
    )
    ga4.ingest(
        con,
        app_id="p",
        source="ga4",
        dialect=DuckDBDialect(),
        executor=ex,
        start=day(0),
        end=day(0),
        opts=opts,
        log=lambda *_: None,
    )
    rows = con.execute(
        "SELECT device_tier, scene, frames, ft_p50_ms, slow_frames FROM raw_perf_windows ORDER BY scene, device_tier"
    ).fetchall()
    assert rows == [
        ("high", "Level", 2000, 16.0, 0),
        ("low", "Level", 1000, 20.0, 10),
        ("unknown", "camera", None, pytest.approx(40.0), None),  # 1000 / 25 fps
    ]
    assert con.execute("SELECT kind, scene, duration_ms FROM raw_load_times").fetchall() == [
        ("cold_start", "Boot", 4000.0)
    ]


def test_duplicate_monetary_events_count_once(con, tmp_path):
    """The export can contain the same purchase twice (client retry): count it once."""
    b = GA4Builder()
    ts = 1_790_000_000_000_000
    b.add(0, "first_open", "A").add(0, "session_start", "A", ga_session_id=1)
    for _ in range(2):  # identical user, timestamp, name and value
        b.add(0, "in_app_purchase", "A", ga_session_id=1, value_usd=4.99, ts_micros=ts)
        b.add(
            0, "ad_impression", "A", ga_session_id=1, value_usd=0.02, value=0.02, ts_micros=ts + 1
        )
    b.add(
        0, "in_app_purchase", "A", ga_session_id=1, value_usd=0.99, ts_micros=ts + 2
    )  # a second, real purchase
    ex = DuckDBExecutor({"ga4_events": b.write(tmp_path / "dup.parquet")})
    ga4.ingest(
        con,
        app_id="d",
        source="ga4",
        dialect=DuckDBDialect(),
        executor=ex,
        start=day(0),
        end=day(1),
        opts=ga4.GA4Options(queries=["active_users", "cohorts"]),
        log=lambda *_: None,
    )
    run_models(con)
    iap, ad = con.execute(
        "SELECT iap_revenue_usd, ad_revenue_usd FROM fct_app_daily WHERE date = ?", [day(0)]
    ).fetchone()
    assert iap == pytest.approx(4.99 + 0.99) and ad == pytest.approx(0.02)
    cohort = con.execute(
        "SELECT iap_revenue_usd, ad_revenue_usd FROM stg_cohort_days WHERE day_n = 0"
    ).fetchone()
    assert cohort == (pytest.approx(5.98), pytest.approx(0.02))


def test_app_info_ids_cannot_inject_sql():
    # BigQuery string literals honour backslash escapes, so stripping quotes is not enough.
    evil = "com.x\\', ') OR TRUE --"
    with pytest.raises(ValueError):
        ga4.GA4Options.from_source({"app_info_ids": [evil]})
    q = ga4.GA4Queries(DuckDBDialect(), day(0), day(1), ga4.GA4Options(app_info_ids=[evil]))
    with pytest.raises(ValueError):
        q.active_users()
    with pytest.raises(ValueError):
        ga4.GA4Queries(DuckDBDialect(), day(0), day(1), ga4.GA4Options(timezone="UTC'; --"))


def test_first_touch_fallback_uses_app_timezone():
    from studio_pulse.connectors.dialect import BigQueryDialect

    sql = ga4.GA4Queries(
        BigQueryDialect("p", "analytics_1"),
        day(0),
        day(1),
        ga4.GA4Options(timezone="Europe/Istanbul"),
    ).cohorts()
    assert "DATE(TIMESTAMP_MICROS(user_first_touch_timestamp), 'Europe/Istanbul')" in sql
