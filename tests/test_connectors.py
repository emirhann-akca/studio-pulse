"""Connectors with mocked network clients and small recorded/synthetic fixtures."""

from __future__ import annotations

import datetime as dt
import json

import pyarrow as pa
import pytest
from conftest import D0, FIXTURES, GA4Builder, run_models

from studio_pulse.connectors import admob, crashlytics, ga4, revenuecat, stores, unity_jsonl
from studio_pulse.connectors.dialect import BigQueryDialect, DuckDBDialect
from studio_pulse.connectors.executors import (
    BigQueryExecutor,
    BytesCapExceeded,
    DuckDBExecutor,
    ReadOnlyViolation,
    assert_read_only,
)
from studio_pulse.demo.export_files import write_crashlytics_parquet

# -- BigQuery safety -------------------------------------------------------------


class FakeJob:
    def __init__(self, nbytes: int, table: pa.Table | None):
        self.total_bytes_processed = nbytes
        self.total_bytes_billed = nbytes
        self._table = table

    def result(self):
        return self

    def to_arrow(self, create_bqstorage_client=True):
        assert create_bqstorage_client is False
        return self._table


class FakeBQ:
    def __init__(self, nbytes: int = 5_000_000, table: pa.Table | None = None):
        self.nbytes, self.table, self.calls = nbytes, table, []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.nbytes, self.table)


def test_read_only_guard():
    assert_read_only("SELECT 1")
    assert_read_only("WITH a AS (SELECT 1) SELECT * FROM a")
    assert_read_only("SELECT 'drop table x; delete' AS s")  # keywords inside strings are fine
    assert_read_only("SELECT COUNT(*) FROM t WHERE event_name NOT IN ('app_update', 'os_update')")
    for bad in (
        "DELETE FROM t WHERE 1=1",
        "CREATE TABLE x AS SELECT 1",
        "SELECT 1; DROP TABLE t",
        "INSERT INTO t SELECT 1",
        "WITH a AS (SELECT 1) MERGE t USING a ON TRUE WHEN MATCHED THEN DELETE",
        "EXPORT DATA OPTIONS(uri='gs://x/*') AS SELECT 1",
    ):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(bad)


def test_bigquery_dry_run_then_capped_run():
    table = pa.table({"x": [1]})
    client = FakeBQ(nbytes=1_000, table=table)
    ex = BigQueryExecutor(
        "proj", max_bytes_billed=2_000_000_000, client=client, log=lambda *_: None
    )
    out = ex.run("q", "SELECT 1 AS x")
    assert out.num_rows == 1
    (_, dry_cfg), (_, run_cfg) = client.calls
    assert dry_cfg.dry_run is True and dry_cfg.use_query_cache is False
    assert run_cfg.maximum_bytes_billed == 2_000_000_000
    assert not run_cfg.dry_run
    assert run_cfg.destination is None
    assert ex.stats[0].estimated_bytes == 1_000 and ex.stats[0].billed_bytes == 1_000


def test_bigquery_refuses_query_over_cap():
    client = FakeBQ(nbytes=3_000_000_000)
    ex = BigQueryExecutor(
        "proj", max_bytes_billed=2_000_000_000, client=client, log=lambda *_: None
    )
    with pytest.raises(BytesCapExceeded):
        ex.run("big", "SELECT 1")
    assert len(client.calls) == 1  # only the dry run was sent


def test_bigquery_dry_run_only_mode_loads_nothing(con):
    client = FakeBQ()
    ex = BigQueryExecutor("proj", client=client, dry_run_only=True, log=lambda *_: None)
    ga4.ingest(
        con,
        app_id="g",
        source="ga4_bigquery",
        dialect=BigQueryDialect("proj", "analytics_1"),
        executor=ex,
        start=D0,
        end=D0 + dt.timedelta(days=9),
        opts=ga4.GA4Options(),
        log=lambda *_: None,
    )
    assert all(cfg.dry_run for _, cfg in client.calls)
    assert len(client.calls) == len(ga4.GA4Options().queries)
    assert con.execute("SELECT COUNT(*) FROM raw_ga4_active_users").fetchone()[0] == 0


def test_bigquery_sql_rendering():
    q = ga4.GA4Queries(
        BigQueryDialect("my-proj", "analytics_42"),
        dt.date(2026, 9, 1),
        dt.date(2026, 10, 30),
        ga4.GA4Options(app_info_ids=["com.example.game"]),
    )
    sql = q.active_users()
    assert "`my-proj.analytics_42.events_*`" in sql
    assert "_TABLE_SUFFIX BETWEEN '20260803' AND '20261030'" in sql  # 29 days of history for MAU
    assert "UNNEST(event_params)" in sql and "app_info.id IN ('com.example.game')" in sql
    assert "GENERATE_DATE_ARRAY(DATE '2026-09-01', DATE '2026-10-30')" in sql
    for name in ga4.GA4Options().queries:
        assert_read_only(getattr(q, name)())
    with pytest.raises(ValueError):
        BigQueryDialect("proj`; DROP", "ds")


def test_bigquery_ga4_ingest_records_bytes(con):
    canned = pa.table(
        {
            "date": [D0],
            "platform": ["ANDROID"],
            "dau": [10],
            "wau": [12],
            "mau": [20],
            "new_users": [3],
            "sessions": [14],
            "engagement_sec": [600.0],
            "crashed_users": [0],
            "crashed_sessions": [0],
            "iap_revenue_usd": [0.0],
            "ad_revenue_usd": [0.5],
            "payers": [0],
            "history_days": [30],
        }
    )
    client = FakeBQ(nbytes=7_340_032, table=canned)
    ex = BigQueryExecutor("proj", client=client, log=lambda *_: None)
    ga4.ingest(
        con,
        app_id="g",
        source="ga4_bigquery",
        dialect=BigQueryDialect("proj", "analytics_1"),
        executor=ex,
        start=D0,
        end=D0,
        opts=ga4.GA4Options(queries=["active_users"]),
        log=lambda *_: None,
    )
    assert con.execute("SELECT dau, mau FROM raw_ga4_active_users").fetchall() == [(10, 20)]
    assert con.execute("SELECT bytes_billed FROM meta_loads").fetchone()[0] == 7_340_032


def test_ga4_outputs_carry_no_user_ids(tmp_path):
    b = GA4Builder()
    for u in ("user-aaa", "user-bbb"):
        paid = ("google", "cpc", "UAC_1")
        b.add(0, "first_open", u, traffic=paid).add(
            0, "session_start", u, ga_session_id=1, traffic=paid
        ).add(1, "level_start", u, level=1, traffic=paid)
        b.add(0, "pulse_perf", u, tier="low", frames=100, ft_p50_ms=20.0)
    ex = DuckDBExecutor({"ga4_events": b.write(tmp_path / "e.parquet")})
    q = ga4.GA4Queries(DuckDBDialect(), D0, D0 + dt.timedelta(days=2), ga4.GA4Options())
    for name in [*ga4.GA4Options().queries, "campaign_cohorts"]:
        t = ex.run(name, getattr(q, name)())
        assert t.num_rows > 0 or name in ("levels", "level_max", "loads"), name
        assert not {"user_pseudo_id", "u", "user_id"} & set(t.column_names), name
        for col in t.columns:
            assert "user-aaa" not in [str(v) for v in col.to_pylist()], name


# -- Crashlytics -----------------------------------------------------------------------


def test_crashlytics_daily_aggregates(con, tmp_path):
    ts = dt.datetime(2026, 10, 1, 12)
    path = write_crashlytics_parquet(
        {
            "event_timestamp": [ts, ts, ts, ts + dt.timedelta(days=1), ts],
            "platform": ["ANDROID"] * 5,
            "is_fatal": [True, True, True, True, False],
            "installation_uuid": ["i1", "i1", "i2", "i1", "i3"],
            "issue_id": ["x", "y", "x", "x", "z"],
            "app_version": ["1.0"] * 5,
        },
        tmp_path / "c.parquet",
    )
    ex = DuckDBExecutor({"crashlytics_events": path})
    crashlytics.ingest(
        con,
        app_id="g",
        source="crash",
        dialect=DuckDBDialect(),
        executor=ex,
        tables=["crashlytics_events"],
        start=D0,
        end=D0 + dt.timedelta(days=1),
        log=lambda *_: None,
    )
    rows = con.execute(
        "SELECT date, fatal_events, nonfatal_events, crashed_installs, fatal_issues FROM raw_crashlytics_daily ORDER BY date"
    ).fetchall()
    assert rows == [(D0, 3, 1, 2, 2), (D0 + dt.timedelta(days=1), 1, 0, 1, 1)]
    con.execute(
        "INSERT INTO raw_ga4_active_users (app_id, platform, date, dau, sessions, crashed_users, history_days, _source) VALUES ('g', 'ANDROID', ?, 200, 250, 0, 1, 'ga4')",
        [D0],
    )
    run_models(con)
    r = con.execute(
        "SELECT crash_free_users, crash_free_sessions, crash_basis FROM fct_app_daily WHERE date = ?",
        [D0],
    ).fetchone()
    assert r == (pytest.approx(1 - 2 / 200), pytest.approx(1 - 3 / 250), "crashlytics")


def test_crash_loop_cannot_push_crash_free_below_zero(con):
    """23 fatal crashes from one device on a day GA4 saw 2 sessions."""
    con.execute(
        "INSERT INTO raw_crashlytics_daily VALUES ('g', ?, 'ANDROID', 23, 0, 1, 1, 'crash', now())",
        [D0],
    )
    con.execute(
        "INSERT INTO raw_ga4_active_users (app_id, platform, date, dau, sessions, history_days, _source) VALUES ('g', 'ANDROID', ?, 1, 2, 1, 'ga4')",
        [D0],
    )
    con.execute(
        "INSERT INTO meta_apps (app_id, name, genre, platforms, is_demo) VALUES ('g', 'G', 'app', 'ANDROID', FALSE)"
    )
    run_models(con)
    assert con.execute(
        "SELECT crash_free_users, crash_free_sessions FROM fct_app_daily"
    ).fetchone() == (0.0, 0.0)
    assert con.execute("SELECT crash_free_sessions_7d FROM fct_app_summary").fetchone()[0] == 0.0


def test_platform_without_crashlytics_export_falls_back_to_ga4(con):
    """Android exports Crashlytics, iOS does not: iOS crashes must still count."""
    con.execute(
        "INSERT INTO raw_crashlytics_daily VALUES ('g', ?, 'ANDROID', 1, 0, 1, 1, 'crash', now())",
        [D0],
    )
    con.execute(
        "INSERT INTO raw_ga4_active_users (app_id, platform, date, dau, sessions, crashed_users, crashed_sessions, history_days, _source) "
        "VALUES ('g', 'ANDROID', ?, 10, 12, 0, 0, 1, 'ga4'), ('g', 'IOS', ?, 10, 10, 2, 2, 1, 'ga4')",
        [D0, D0],
    )
    run_models(con)
    r = con.execute(
        "SELECT crashed_users, crash_free_users, crash_basis FROM fct_app_daily"
    ).fetchone()
    assert r == (3, pytest.approx(1 - 3 / 20), "crashlytics+ga4_app_exception")


def test_missing_crash_counts_stay_unknown(con):
    """DuckDB's LEAST skips NULLs; an unknown crash count must not read as 'everyone crashed'."""
    con.execute(
        "INSERT INTO raw_ga4_active_users (app_id, platform, date, dau, sessions, history_days, _source) VALUES ('g', 'IOS', ?, 5, 5, 1, 'ga4')",
        [D0],
    )
    con.execute("INSERT INTO meta_apps (app_id, name) VALUES ('g', 'G')")
    run_models(con)
    assert con.execute(
        "SELECT crash_free_users, crash_free_sessions FROM fct_app_daily"
    ).fetchone() == (None, None)
    assert con.execute("SELECT crash_free_users_7d FROM fct_app_summary").fetchone()[0] is None


def test_crashlytics_days_follow_the_app_timezone(con, tmp_path):
    """22:30 UTC on Oct 1 is already Oct 2 in Istanbul, where GA4's event_date lives."""
    late = dt.datetime(2026, 10, 1, 22, 30)
    path = write_crashlytics_parquet(
        {
            "event_timestamp": [late, dt.datetime(2026, 10, 1, 12)],
            "platform": ["ANDROID"] * 2,
            "is_fatal": [True, True],
            "installation_uuid": ["i1", "i2"],
            "issue_id": ["x", "x"],
            "app_version": ["1.0"] * 2,
        },
        tmp_path / "c.parquet",
    )
    for tz, expected in (
        ("UTC", [(D0, 2)]),
        ("Europe/Istanbul", [(D0, 1), (D0 + dt.timedelta(days=1), 1)]),
    ):
        ex = DuckDBExecutor({"crashlytics_events": path})
        crashlytics.ingest(
            con,
            app_id="g",
            source="crash",
            dialect=DuckDBDialect(),
            executor=ex,
            tables=["crashlytics_events"],
            start=D0,
            end=D0 + dt.timedelta(days=1),
            timezone=tz,
            log=lambda *_: None,
        )
        assert (
            con.execute(
                "SELECT date, fatal_events FROM raw_crashlytics_daily ORDER BY date"
            ).fetchall()
            == expected
        ), tz
    sql = crashlytics.daily_query(
        BigQueryDialect("p", "firebase_crashlytics"), "t", D0, D0, "Europe/Istanbul"
    )
    assert "DATE(event_timestamp, 'Europe/Istanbul') BETWEEN" in sql
    with pytest.raises(ValueError):
        crashlytics.daily_query(
            BigQueryDialect("p", "firebase_crashlytics"), "t", D0, D0, "UTC') --"
        )


def test_crashlytics_bigquery_sql_prunes_partitions():
    sql = crashlytics.daily_query(
        BigQueryDialect("p", "firebase_crashlytics"), "com_example_game_ANDROID", D0, D0
    )
    assert "`p.firebase_crashlytics.com_example_game_ANDROID`" in sql
    # +-1 UTC day for the partition filter, the exact cut on the local date
    assert "event_timestamp >= TIMESTAMP '2026-09-30 00:00:00'" in sql
    assert "event_timestamp < TIMESTAMP '2026-10-03 00:00:00'" in sql
    assert "DATE(event_timestamp, 'UTC') BETWEEN DATE '2026-10-01' AND DATE '2026-10-01'" in sql
    assert_read_only(sql)


# -- AdMob ----------------------------------------------------------------------------------


class FakeHTTP:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, body, []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append((url, headers, json.loads(data)))
        outer = self

        class R:
            status_code = outer.status
            text = "error body"

            def json(self):
                return outer.body

        return R()


def test_admob_parse_recorded_report():
    rows, currency = admob.parse_report(
        json.loads((FIXTURES / "admob_network_report.json").read_text(encoding="utf-8"))
    )
    assert currency == "USD" and len(rows) == 3
    assert rows[0] == {
        "date": D0,
        "admob_app_id": "ca-app-pub-0000000000000000~1111111111",
        "earnings": 12.5,
        "currency": "USD",
        "impressions": 2500,
        "clicks": 31,
        "ad_requests": 2900,
        "matched_requests": 2700,
    }
    assert rows[2]["clicks"] is None


def test_admob_fetch_builds_request():
    body = json.loads((FIXTURES / "admob_network_report.json").read_text(encoding="utf-8"))
    http = FakeHTTP(body=body)
    out = admob.fetch_report(
        "pub-0000000000000000",
        D0,
        D0 + dt.timedelta(days=1),
        ["ca-app-pub-0000000000000000~1111111111"],
        session=http,
        token="t0k",
    )
    assert out == body
    url, headers, payload = http.calls[0]
    assert (
        url
        == "https://admob.googleapis.com/v1/accounts/pub-0000000000000000/networkReport:generate"
    )
    assert headers["Authorization"] == "Bearer t0k"
    spec = payload["reportSpec"]
    assert spec["dateRange"]["startDate"] == {"year": 2026, "month": 10, "day": 1}
    assert spec["dimensions"] == ["DATE", "APP"] and "ESTIMATED_EARNINGS" in spec["metrics"]
    assert spec["dimensionFilters"][0]["matchesAny"]["values"] == [
        "ca-app-pub-0000000000000000~1111111111"
    ]


def test_admob_http_error_and_bad_account():
    with pytest.raises(RuntimeError, match="403"):
        admob.fetch_report("pub-1", D0, D0, session=FakeHTTP(status=403), token="t")
    with pytest.raises(ValueError):
        admob.fetch_report("1234", D0, D0, session=FakeHTTP(), token="t")


def test_admob_ingest_filters_app_and_window(con):
    body = json.loads((FIXTURES / "admob_network_report.json").read_text(encoding="utf-8"))
    n = admob.ingest(
        con,
        app_id="g",
        source="admob",
        start=D0,
        end=D0,
        publisher_id="pub-0000000000000000",
        app_ids=["ca-app-pub-0000000000000000~1111111111"],
        session=FakeHTTP(body=body),
        token="t",
        log=lambda *_: None,
    )
    assert n == 1
    assert con.execute("SELECT earnings, impressions FROM raw_admob_daily").fetchall() == [
        (12.5, 2500)
    ]


# -- Store reports ---------------------------------------------------------------------------


def test_play_console_utf16_reports(con):
    stores.ingest_play(
        con,
        app_id="g",
        source="play",
        path=FIXTURES / "play",
        package="com.example.game",
        log=lambda *_: None,
    )
    assert con.execute(
        "SELECT date, installs, uninstalls FROM raw_store_installs ORDER BY date"
    ).fetchall() == [(D0, 120, 35), (D0 + dt.timedelta(days=1), 100, 30)]
    assert con.execute(
        "SELECT daily_avg_rating, total_avg_rating FROM raw_store_ratings ORDER BY date"
    ).fetchall() == [(4.5, 4.31), (None, 4.31)]
    stores.ingest_play(
        con,
        app_id="g",
        source="play",
        path=FIXTURES / "play",
        package="com.example.game",
        log=lambda *_: None,
    )
    assert (
        con.execute("SELECT COUNT(*) FROM raw_store_installs").fetchone()[0] == 2
    )  # idempotent per file


def test_app_store_sales_report():
    text = stores.read_text(FIXTURES / "appstore" / "S_D_80000000_20261001.txt")
    rows = stores.parse_appstore_sales(text, apple_id="1234567890", sku="com.example.game")
    by_cur = {r["proceeds_currency"]: r for r in rows}
    usd, eur = by_cur["USD"], by_cur["EUR"]
    assert (usd["installs"], usd["redownloads"], usd["updates"], usd["iap_units"]) == (
        50,
        7,
        200,
        4,
    )
    assert usd["proceeds"] == pytest.approx(4 * 0.70)  # Developer Proceeds is per unit
    assert eur["iap_units"] == 2 and eur["proceeds"] == pytest.approx(2 * 0.62)
    assert all(r["date"] == D0 for r in rows)


# -- RevenueCat from GCS (mocked) --------------------------------------------------------------


class FakeBlob:
    def __init__(self, name, data):
        self.name, self.data, self.size = name, data, len(data)
        self.downloads = 0

    def download_to_filename(self, path):
        self.downloads += 1
        with open(path, "wb") as f:
            f.write(self.data)


class FakeGCS:
    def __init__(self, blobs):
        self.blobs = blobs

    def list_blobs(self, bucket, prefix=None):
        assert bucket == "rc-bucket" and prefix == "exports"
        return list(self.blobs)


def test_revenuecat_gcs_sync_downloads_only_new_files(con, tmp_path):
    csv = b"store_transaction_id,renewal_number,price_in_usd,start_time\nT1,1,2.99,2026-10-01 10:00:00\n"
    blobs = [
        FakeBlob("exports/2026-10-01/transactions_1790820000.csv", csv),
        FakeBlob("exports/README.txt", b"x"),
    ]
    gcs = FakeGCS(blobs)
    cache = tmp_path / "cache"
    revenuecat.ingest(
        con,
        app_id="g",
        source="rc",
        path="gs://rc-bucket/exports",
        cache_dir=cache,
        gcs_client=gcs,
        log=lambda *_: None,
    )
    revenuecat.ingest(
        con,
        app_id="g",
        source="rc",
        path="gs://rc-bucket/exports",
        cache_dir=cache,
        gcs_client=gcs,
        log=lambda *_: None,
    )
    assert blobs[0].downloads == 1 and blobs[1].downloads == 0
    assert con.execute(
        "SELECT store_transaction_id, price_in_usd FROM raw_revenuecat_transactions"
    ).fetchall() == [("T1", 2.99)]


def test_revenuecat_gcs_sync_refuses_paths_outside_the_cache(con, tmp_path):
    csv = b"store_transaction_id,renewal_number,price_in_usd,start_time\nT1,1,2.99,2026-10-01 10:00:00\n"
    evil = FakeBlob("exports/../../escaped/transactions_1790820000.csv", csv)
    cache = tmp_path / "a" / "b" / "cache"
    revenuecat.sync_gcs(
        "gs://rc-bucket/exports", cache, client=FakeGCS([evil]), log=lambda *_: None
    )
    assert evil.downloads == 0
    assert not (tmp_path / "a" / "escaped").exists()


# -- Unity JSON lines --------------------------------------------------------------------------


def _line(event="pulse_perf", app="com.example.game", **params):
    base = {
        "pulse_v": 1,
        "tier": "low",
        "scene": "Level",
        "window_s": 30.0,
        "frames": 1700,
        "ft_p50_ms": 17.2,
        "ft_p95_ms": 25.0,
        "ft_max_ms": 70.0,
        "slow_frames": 12,
        "mem_alloc_mb": 410,
        "mem_reserved_mb": 520,
        "sys_mem_mb": 3072,
    }
    if event == "pulse_load":
        base = {
            "pulse_v": 1,
            "tier": "low",
            "kind": "cold_start",
            "scene": "Boot",
            "duration_ms": 3900.5,
        }
    return json.dumps(
        {
            "v": 1,
            "event": event,
            "ts": "2026-10-01T23:59:59.500Z",
            "app": app,
            "app_version": "1.2.0",
            "platform": "IPhonePlayer",
            "session": "abc",
            "params": {**base, **params},
        }
    )


def test_unity_jsonl_parse():
    lines = [
        _line(),
        _line("pulse_load"),
        _line(app="com.other"),
        "{not json",
        json.dumps({"v": 2, "event": "pulse_perf"}),
        _line("level_start"),
        "",
    ]
    perf, load, st = unity_jsonl.parse_lines(lines, app="com.example.game")
    assert (st.lines, st.perf, st.load, st.skipped_other_app, st.malformed, st.unknown_event) == (
        6,
        1,
        1,
        1,
        2,
        1,
    )
    assert perf[0]["date"] == D0 and perf[0]["platform"] == "IOS" and perf[0]["frames"] == 1700
    assert perf[0]["ft_p50_ms"] == 17.2 and perf[0]["slow_frames"] == 12
    assert load[0]["kind"] == "cold_start" and load[0]["duration_ms"] == 3900.5


def test_unity_jsonl_ingest_is_idempotent(con, tmp_path):
    f = tmp_path / "pulse-20261001.jsonl"
    f.write_text(
        "\n".join([_line(), _line(ft_p50_ms=19.0), _line("pulse_load")]) + "\n", encoding="utf-8"
    )
    for _ in range(2):
        unity_jsonl.ingest(con, app_id="g", source="unity", path=tmp_path, log=lambda *_: None)
    assert con.execute("SELECT COUNT(*) FROM raw_perf_windows").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM raw_load_times").fetchone()[0] == 1


def test_unity_jsonl_files_with_the_same_name_from_two_devices(con, tmp_path):
    """JsonLinesSink always writes pulse/events.jsonl, so collected files share a name."""
    for device, ft in (("device_a", 17.0), ("device_b", 25.0)):
        (tmp_path / device).mkdir()
        (tmp_path / device / "events.jsonl").write_text(
            _line(ft_p50_ms=ft) + "\n", encoding="utf-8"
        )
    for _ in range(2):
        unity_jsonl.ingest(con, app_id="g", source="unity", path=tmp_path, log=lambda *_: None)
    assert sorted(
        r[0] for r in con.execute("SELECT ft_p50_ms FROM raw_perf_windows").fetchall()
    ) == [17.0, 25.0]


def test_unity_jsonl_dates_follow_the_app_timezone():
    perf, _, _ = unity_jsonl.parse_lines([_line()], timezone="Europe/Istanbul")
    assert perf[0]["date"] == D0 + dt.timedelta(days=1)  # 23:59:59 UTC = 02:59 next day in Istanbul
