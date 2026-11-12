"""Cloud mode: the pulse_sdk source reads the collector's BigQuery tables like its local store.

The same simulated sessions go through a real collector (local NDJSON store); the
store is then copied into BigQuery-shaped tables (TIMESTAMP columns, as
contract/generated/bigquery/*.json) in DuckDB, and the BigQuery SQL runs there
through the DuckDB dialect. Both paths must put the same rows in raw_pulse_events.
"""

from __future__ import annotations

import datetime as dt

import pytest

from studio_pulse import warehouse
from studio_pulse.collector.ingest import (
    _columns,
    _files,
    bigquery_registry_sql,
    bigquery_sql,
    ingest,
    stage_rows,
)
from studio_pulse.collector.inprocess import running_collector
from studio_pulse.collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings
from studio_pulse.connectors.dialect import BigQueryDialect, DuckDBDialect
from studio_pulse.connectors.executors import DuckDBExecutor
from studio_pulse.contract import spec
from studio_pulse.simulate.model import GAMES
from studio_pulse.simulate.run import Poster, SimClock, backfill

START = dt.date(2026, 10, 1)
DAYS = 5
END = START + dt.timedelta(days=DAYS - 1)
BUNDLE = "demo.pebblepop"


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("bq_parity") / "collector"
    t0 = int(dt.datetime.combine(START, dt.time(), dt.UTC).timestamp() * 1000)
    clock = SimClock(t0)
    with running_collector(CollectorSettings(data_dir=data_dir), clock) as (url, _col):
        backfill(Poster(url, DEFAULT_LOCAL_KEY, clock=clock), start=START, days=DAYS,
                 games=GAMES[:1], scale=0.3, seed=5, log=lambda m: None)  # fmt: skip
    return data_dir


def fake_bigquery(data_dir, deleted: list[str]) -> DuckDBExecutor:
    """The local store as BigQuery-shaped tables: TIMESTAMP columns, real schema names."""
    ex = DuckDBExecutor({})
    for table in ("events", "quarantine"):
        cols = spec()["storage"][table]["columns"]
        local = _columns(table)
        files = _files(data_dir / table)
        sel = ", ".join(
            f"make_timestamp(CAST({c['name']} AS BIGINT) * 1000) AS {c['name']}"
            if c["type"] == "TIMESTAMP" else c["name"]
            for c in cols
        )  # fmt: skip
        if files:
            spec_cols = "{" + ", ".join(f"'{c}': '{t}'" for c, t in local.items()) + "}"
            ex.con.execute(
                f"CREATE TABLE {table} AS SELECT {sel} FROM read_ndjson(?, columns = {spec_cols})",
                [files],
            )
        else:
            ex.con.execute(f"CREATE TABLE {table} AS SELECT {sel} FROM (SELECT "
                           + ", ".join(f"CAST(NULL AS {t}) AS {c}" for c, t in local.items())
                           + ") WHERE false")  # fmt: skip
    ex.con.execute("CREATE TABLE deletion_requests (install_id VARCHAR, app_id VARCHAR)")
    # the registry, from the collector's SQLite into BigQuery's shape (TIMESTAMP columns)
    from studio_pulse.collector.registry import read_registry

    ms = {"first_seen_ms", "last_seen_ms", "status_changed_ms", "store_checked_ms"}
    ex.con.execute(
        "CREATE TABLE app_registry (app_id VARCHAR, platform VARCHAR, display_name VARCHAR, "
        "genre VARCHAR, status VARCHAR, first_seen TIMESTAMP, last_seen TIMESTAMP, "
        "first_version VARCHAR, last_version VARCHAR, status_changed_at TIMESTAMP, "
        "store_listed BOOLEAN, store_version VARCHAR, store_url VARCHAR, store_checked_at TIMESTAMP)"
    )
    for r in read_registry(data_dir):
        t = {k: (None if r[k] is None else dt.datetime.fromtimestamp(r[k] / 1000, dt.UTC).replace(tzinfo=None))
             for k in ms}  # fmt: skip
        ex.con.execute(
            "INSERT INTO app_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [r["app_id"], r["platform"], r["display_name"], r["genre"], r["status"],
             t["first_seen_ms"], t["last_seen_ms"], r["first_version"], r["last_version"],
             t["status_changed_ms"], None if r["store_listed"] is None else bool(r["store_listed"]),
             r["store_version"], r["store_url"], t["store_checked_ms"]],
        )  # fmt: skip
    for i in deleted:
        ex.con.execute("INSERT INTO deletion_requests VALUES (?, ?)", [i, BUNDLE])
    return ex


def load(con, *, data_dir=None, ex=None) -> list[tuple]:
    kw = {}
    if ex is not None:
        d = DuckDBDialect()
        kw = {
            "stage": lambda c, tag: stage_rows(
                c,
                tag,
                {t: ex.run(t, bigquery_sql(d, t, START, END)) for t in ("events", "quarantine")},
            ),
            "stage_key": "bq:test",
            "registry": ex.run("reg", bigquery_registry_sql(d)).to_pylist(),
        }
    ingest(con, app_id="pebble_pop", source="pulse_sdk", data_dir=data_dir, bundle_ids=[BUNDLE],
           timezone="UTC", start=START, end=END, log=lambda m: None, **kw)  # fmt: skip
    cols = [
        c[0] for c in con.execute("DESCRIBE raw_pulse_events").fetchall() if c[0] != "_loaded_at"
    ]
    return sorted(
        con.execute(f"SELECT {', '.join(cols)} FROM raw_pulse_events").fetchall(), key=repr
    )


def test_bigquery_path_loads_the_same_rows_as_the_local_store(store, tmp_path):
    a = warehouse.connect(tmp_path / "a.duckdb")
    b = warehouse.connect(tmp_path / "b.duckdb")
    local_rows = load(a, data_dir=store)
    bq_rows = load(b, ex=fake_bigquery(store, deleted=[]))
    assert len(local_rows) > 500
    assert bq_rows == local_rows
    q = "SELECT app_id, bundle_id, platform, status, first_seen, first_version FROM raw_pulse_app_registry ORDER BY ALL"
    assert a.execute(q).fetchall() == b.execute(q).fetchall() != []


def test_bigquery_path_leaves_out_deleted_installs(store, tmp_path):
    con = warehouse.connect(tmp_path / "c.duckdb")
    ex = fake_bigquery(store, deleted=[])
    victim = ex.con.execute(
        "SELECT install_id FROM events WHERE app_id = ? LIMIT 1", [BUNDLE]
    ).fetchone()[0]
    rows = load(con, ex=fake_bigquery(store, deleted=[victim]))
    installs = {
        r[0] for r in con.execute("SELECT DISTINCT install_id FROM raw_pulse_events").fetchall()
    }
    assert rows and victim not in installs


def test_bigquery_sql_prunes_partitions_and_converts_timestamps():
    sql = bigquery_sql(BigQueryDialect("my-proj", "pulse_raw"), "events", START, END)
    assert sql.startswith("SELECT ") and "FROM `my-proj.pulse_raw.events`" in sql
    assert "UNIX_MILLIS(ts) AS ts" in sql and "UNIX_MILLIS(received_at) AS received_at" in sql
    assert "received_at >= TIMESTAMP '2026-09-30 00:00:00'" in sql  # the partition filter
    assert "`my-proj.pulse_raw.deletion_requests`" in sql
    from studio_pulse.connectors.executors import assert_read_only

    assert_read_only(sql)
