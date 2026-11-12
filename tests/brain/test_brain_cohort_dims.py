"""B3/B3b: the dimensional cohort pulls, on GA4-export-shaped fixtures (no BigQuery calls)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest

from studio_pulse.brain import cohort_dims as cd
from studio_pulse.connectors import ga4
from studio_pulse.connectors.dialect import BigQueryDialect, DuckDBDialect
from studio_pulse.connectors.executors import BigQueryExecutor, DuckDBExecutor, assert_read_only

D0 = dt.date(2026, 10, 1)


def build_events(path: Path, *, users_per_cell: int = 60) -> Path:
    """first_open on day 0 for users in country x version x tier; 1.8.0 installers churn more."""
    from conftest import GA4Builder  # tests/conftest.py: the shared GA4-shaped fixture writer

    b = GA4Builder()
    country_of: dict[str, str] = {}
    k = 0
    for country in ("Brazil", "Germany"):
        for version in ("1.7.2", "1.8.0"):
            for tier in ("low", "high"):
                for i in range(users_per_cell):
                    u = f"user-{k:05d}"
                    k += 1
                    country_of[u] = country
                    b.add(0, "first_open", u, version=version, ga_session_id=1)
                    b.add(0, "session_start", u, version=version, ga_session_id=1)
                    b.add(
                        0, "pulse_perf", u, version=version, tier=tier, frames=100, ft_p50_ms=18.0
                    )
                    if i < users_per_cell * (0.2 if version == "1.8.0" else 0.5):
                        b.add(1, "session_start", u, version=version, ga_session_id=2)
    b.cols["country"] = [country_of[u] for u in b.cols["user_pseudo_id"]]
    return b.write(path)


@pytest.fixture()
def events(tmp_path):
    return build_events(tmp_path / "events.parquet")


def run_duckdb(events: Path, **opts) -> list[dict]:
    o = ga4.GA4Options.from_source({"queries": ["cohort_dims"], **opts})
    q = ga4.GA4Queries(DuckDBDialect(), D0, D0 + dt.timedelta(days=3), o)
    ex = DuckDBExecutor({"ga4_events": events})
    t = ex.run("cohort_dims", q.cohort_dims())
    return t.to_pylist()


def test_grouping_sets_cover_total_singles_and_pairs():
    sets = cd.grouping_sets()
    assert len(sets) == 16 and () in sets and ("app_version", "device_tier") in sets


def test_cohort_dims_cells_respect_the_minimum_and_carry_no_ids(events):
    rows = run_duckdb(events, min_cell_users=50)
    assert rows
    assert min(r["users"] for r in rows) >= 50
    flat = {str(v) for r in rows for v in r.values()}
    assert not any(v.startswith("user-") for v in flat)
    assert not {"user_pseudo_id", "u"} & set(rows[0])
    total = [r for r in rows if all(r[d] is None for d in cd.DIMS)]
    assert total[0]["users"] == 480 and total[0]["d1_users"] == 2 * 2 * (30 + 12)
    by_version = {r["app_version"]: r for r in rows if r["app_version"] and not any(
        r[d] for d in cd.DIMS if d != "app_version")}  # fmt: skip
    assert float(by_version["1.8.0"]["d1_users"]) / by_version["1.8.0"]["users"] == pytest.approx(
        0.2
    )
    assert float(by_version["1.7.2"]["d1_users"]) / by_version["1.7.2"]["users"] == pytest.approx(
        0.5
    )
    tiers = {r["device_tier"] for r in rows if r["device_tier"]}
    assert tiers == {"low", "high"}  # from the pulse_perf tier parameter
    countries = {r["country"] for r in rows if r["country"]}
    assert countries == {"Brazil", "Germany"}


def test_cells_below_the_minimum_are_never_returned(events):
    rows = run_duckdb(events, min_cell_users=130)
    assert rows and min(r["users"] for r in rows) >= 130
    # 60-user (country, version, tier) cells and 120-user pairs are suppressed, 240-user singles kept
    assert not [r for r in rows if r["app_version"] and r["device_tier"]]
    with pytest.raises(ValueError):
        ga4.GA4Options.from_source({"min_cell_users": 0})


def test_bigquery_rendering_is_read_only_dry_run_first_and_capped():
    from test_connectors import FakeBQ

    opts = ga4.GA4Options.from_source(
        {"queries": ["cohort_dims"], "segments": ["music_enabled"],
         "tier_by_model": {"low": ["SM-A055F", "Redmi 12C"]}}
    )  # fmt: skip
    q = ga4.GA4Queries(BigQueryDialect("proj", "analytics_1"), D0, D0 + dt.timedelta(days=30), opts)
    for sql in (q.cohort_dims(), q.segment_retention()):
        assert_read_only(sql)
        assert "GROUPING SETS" in sql and "HAVING COUNT(*) >= 50" in sql
        assert (
            "user_pseudo_id AS u" in sql
            and "SELECT\n    cohort_date" in sql
            or "SELECT cohort_date" in sql
        )
    assert "ARRAY_AGG(" in q.cohort_dims() and "WHEN 'Redmi 12C' THEN 'low'" in q.cohort_dims()
    assert "UNNEST(user_properties)" in q.segment_retention()
    assert opts.queries == ["cohort_dims", "segment_retention"]
    client = FakeBQ(nbytes=1_000)
    ex = BigQueryExecutor("proj", client=client, dry_run_only=True, log=lambda *_: None)
    con = duckdb.connect()
    from studio_pulse import warehouse

    warehouse.init_schema(con)
    ga4.ingest(con, app_id="g", source="ga4_bigquery", dialect=q.d, executor=ex, start=q.start,
               end=q.end, opts=opts, log=lambda *_: None)  # fmt: skip
    assert [cfg.dry_run for _, cfg in client.calls] == [True, True]
    assert ex.stats[0].estimated_bytes == 1_000


@pytest.mark.parametrize("bad", ["x'; DROP", "a\\b", "o'brien"])
def test_tier_map_rejects_unsafe_model_names(bad):
    with pytest.raises(ValueError):
        cd.options_from_source({"tier_by_model": {"low": [bad]}})
    with pytest.raises(ValueError):
        cd.options_from_source({"tier_by_model": {"ultra": ["Pixel 8"]}})


def write_props_events(path: Path, users: int = 240) -> Path:
    """GA4-shaped Parquet with user_properties: music_enabled off/on, day-0 sessions 1 or 3."""
    con = duckdb.connect()
    rows = []
    for i in range(users):
        u = f"user-{i:05d}"
        music = "0" if i % 2 else "1"
        heavy = i % 4 < 2
        sessions = 3 if heavy else 1
        retained = (i % 10) < (6 if heavy else 2)
        for s in range(1, sessions + 1):
            rows.append(("20261001", u, "first_open" if s == 1 else "session_start", s, music))
        if retained:
            rows.append(("20261002", u, "session_start", 9, music))
    con.execute(
        "CREATE TABLE e (event_date VARCHAR, u VARCHAR, name VARCHAR, sid BIGINT, music VARCHAR)"
    )
    con.executemany("INSERT INTO e VALUES (?, ?, ?, ?, ?)", rows)
    out = str(path).replace("'", "''")
    con.execute(
        f"""COPY (SELECT event_date, 1790000000000000 + row_number() OVER () AS event_timestamp,
                name AS event_name,
                [{{'key': 'ga_session_id', 'value': {{'string_value': NULL::VARCHAR, 'int_value': sid,
                  'float_value': NULL::DOUBLE, 'double_value': NULL::DOUBLE}}}}] AS event_params,
                u AS user_pseudo_id, 1790000000000000 AS user_first_touch_timestamp, 'ANDROID' AS platform,
                {{'id': 'com.example.app', 'version': '1.0.0'}} AS app_info,
                {{'country': 'Turkey'}} AS geo,
                {{'name': NULL::VARCHAR, 'medium': NULL::VARCHAR, 'source': NULL::VARCHAR}} AS traffic_source,
                [{{'key': 'music_enabled', 'value': {{'string_value': music, 'int_value': NULL::BIGINT,
                  'float_value': NULL::DOUBLE, 'double_value': NULL::DOUBLE, 'set_timestamp_micros': NULL::BIGINT}}}}]
                  AS user_properties,
                NULL::DOUBLE AS event_value_in_usd
            FROM e ORDER BY event_date) TO '{out}' (FORMAT parquet)"""
    )
    con.close()
    return path


def test_segment_retention_by_user_property_with_engagement_strata(tmp_path):
    path = write_props_events(tmp_path / "props.parquet")
    o = ga4.GA4Options.from_source({"segments": ["music_enabled"], "min_cell_users": 50})
    q = ga4.GA4Queries(DuckDBDialect(), D0, D0 + dt.timedelta(days=3), o)
    t = DuckDBExecutor({"ga4_events": path}).run("seg", q.segment_retention()).to_pylist()
    whole = {r["value"]: r for r in t if r["day0_sessions"] is None}
    assert set(whole) == {"0", "1"} and whole["0"]["users"] == 120
    strata = [r for r in t if r["day0_sessions"] is not None]
    assert {r["day0_sessions"] for r in strata} == {"1", "3+"}
    assert all(r["users"] >= 50 for r in t)
    assert not any(str(v).startswith("user-") for r in t for v in r.values())


def test_sdk_installs_fill_the_same_cells(tmp_path):
    """stg_cohort_dims on SDK installs: classic day N, 50-install cells, dev builds ignored.

    App sdk_a: 70 Android installs in TR and 50 iOS installs in US on D0; on day
    1, 35 Android and 10 iOS players come back, on day 7 seven Android players;
    one more iOS install opens only a debug build on day 1 (not counted). The
    newest day of the app is D0 + 10, so day 30 is not observed yet. App sdk_b
    has 49 installs: no cell at all.
    """
    from studio_pulse import runner

    models = Path(__file__).resolve().parents[2] / "models"
    sql = (models / "stg_cohort_dims.sql").read_text(encoding="utf-8")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE raw_ga4_cohort_dims (app_id VARCHAR, cohort_date DATE, platform VARCHAR, "
        "country VARCHAR, app_version VARCHAR, network VARCHAR, device_tier VARCHAR, users BIGINT, "
        "d1_users BIGINT, d7_users BIGINT, d30_users BIGINT, observed_days INTEGER, "
        "_source VARCHAR, _loaded_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE stg_sdk_installs (app_id VARCHAR, install_id VARCHAR, cohort_date DATE, "
        "platform VARCHAR, country VARCHAR, first_version VARCHAR, device_tier VARCHAR, "
        "app_last_date DATE)"
    )
    con.execute(
        "CREATE TABLE stg_pulse_events (app_id VARCHAR, install_id VARCHAR, date DATE, is_prod BOOLEAN)"
    )
    last = D0 + dt.timedelta(days=10)
    installs, events = [], []

    def add(app, n, platform, country, back_d1, back_d7, start=0):
        for i in range(start, start + n):
            iid = f"{app}-{platform}-{i}"
            installs.append((app, iid, D0, platform, country, "2.0.0", "mid", last))
            events.append((app, iid, D0, True))
            if i - start < back_d1:
                events.append((app, iid, D0 + dt.timedelta(days=1), True))
            if i - start < back_d7:
                events.append((app, iid, D0 + dt.timedelta(days=7), True))

    add("sdk_a", 70, "ANDROID", "TR", 35, 7)
    add("sdk_a", 50, "IOS", "US", 10, 0)
    events.append(("sdk_a", "sdk_a-IOS-49", D0 + dt.timedelta(days=1), False))  # debug build
    add("sdk_b", 49, "ANDROID", "TR", 49, 49)
    con.executemany("INSERT INTO stg_sdk_installs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", installs)
    con.executemany("INSERT INTO stg_pulse_events VALUES (?, ?, ?, ?)", events)
    assert runner  # the real pipeline orders this model after both inputs (name references)
    rows = con.execute(
        f"SELECT grouping_set, platform, country, users, d1_users, d7_users, d30_users "
        f"FROM ({sql}) WHERE app_id = ? ORDER BY grouping_set, platform, country",
        ["sdk_a"],
    ).fetchall()
    got = {(g, p, c): (u, d1, d7, d30) for g, p, c, u, d1, d7, d30 in rows}
    assert got[("", None, None)] == (120, 45, 7, None)
    assert got[("platform", "ANDROID", None)] == (70, 35, 7, None)
    assert got[("platform", "IOS", None)] == (50, 10, 0, None)
    assert got[("country", None, "TR")] == (70, 35, 7, None)
    assert got[("platform,country", "IOS", "US")] == (50, 10, 0, None)
    assert got[("app_version", None, None)] == (120, 45, 7, None)  # every install on 2.0.0
    assert not any("network" in g for g, _, _ in got)  # the SDK does not know the campaign
    n_b = con.execute(f"SELECT COUNT(*) FROM ({sql}) WHERE app_id = 'sdk_b'").fetchone()[0]
    assert n_b == 0
