"""Data-quality checks: each acceptance fault fails its named check; the clean demo passes."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import duckdb
import pytest

from studio_pulse import checks, cli, pipeline, runstate, warehouse

ROOT = Path(__file__).parents[1]
CHECKS = checks.load_checks(ROOT / "checks.yaml")


def _run(cfg, defs=CHECKS):
    con = warehouse.connect(cfg.warehouse)
    try:
        return {r.name: r for r in checks.run_checks(con, defs, cfg)}
    finally:
        con.close()


def _failed(results):
    return sorted(n for n, r in results.items() if r.status != "pass")


def _mutate(cfg, *sql: str) -> None:
    con = duckdb.connect(str(cfg.warehouse))
    for s in sql:
        con.execute(s)
    con.close()


def test_clean_demo_passes_every_check(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    res = _run(cfg)
    assert len(res) >= 35
    assert _failed(res) == []
    types = {r.type for r in res.values()}
    assert types == set(checks.CHECK_TYPES)


def test_duplicated_revenuecat_row_fails(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    _mutate(
        cfg,
        "INSERT INTO raw_revenuecat_transactions SELECT * FROM raw_revenuecat_transactions LIMIT 1",
    )
    res = _run(cfg)
    assert _failed(res) == ["raw_revenuecat_rows_unique"]
    r = res["raw_revenuecat_rows_unique"]
    assert r.failing_rows == 1 and r.severity == "error" and "duplicated key" in r.message


def test_null_app_id_fails(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    _mutate(
        cfg,
        "UPDATE fct_app_daily SET app_id = NULL WHERE rowid = (SELECT MIN(rowid) FROM fct_app_daily)",
    )
    res = _run(cfg)
    assert "app_daily_keys_not_null" in _failed(res)
    assert res["app_daily_keys_not_null"].failing_rows == 1
    assert "NULL in app_id, date" in res["app_daily_keys_not_null"].message


def test_four_day_stale_source_fails(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    # The demo's as-of date is the day after its last data day (lag 1); move
    # AdMob's newest day back 3 days for one app: lag 4 > 3.
    _mutate(
        cfg,
        "UPDATE meta_loads SET max_date = max_date - 3 "
        "WHERE app_id = 'mossy_match' AND source = 'admob'",
    )
    res = _run(cfg)
    assert _failed(res) == ["source_freshness"]
    r = res["source_freshness"]
    assert r.failing_rows == 1
    assert r.sample[0]["app_id"] == "mossy_match" and r.sample[0]["lag_days"] == 4
    assert "mossy_match/admob" in r.message and "4 d, limit 3" in r.message


def test_never_loaded_source_fails_freshness(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    _mutate(cfg, "DELETE FROM meta_loads WHERE app_id = 'pacer_pal' AND source = 'revenuecat'")
    r = _run(cfg)["source_freshness"]
    assert r.status == "fail" and "pacer_pal/revenuecat never loaded" in r.message


def test_sdk_source_waiting_for_its_first_event_is_not_stale(demo_build, tmp_path):
    """13 Nov 2026, first real run: an app whose SDK no build ships yet failed freshness."""
    cfg = demo_build.copy(tmp_path)
    _mutate(
        cfg,
        "UPDATE meta_loads SET max_date = NULL, min_date = NULL, rows_loaded = 0 "
        "WHERE app_id = 'pebble_pop' AND source = 'pulse_sdk'",
    )
    assert "source_freshness" not in _failed(_run(cfg))
    # a GA4 source with no dated rows is still a problem
    _mutate(
        cfg,
        "UPDATE meta_loads SET max_date = NULL WHERE app_id = 'mossy_match' AND source = 'ga4_files'",
    )
    r = _run(cfg)["source_freshness"]
    assert r.status == "fail" and "mossy_match/ga4_files loaded, but no dated rows" in r.message
    assert "pebble_pop" not in r.message


def test_seventy_percent_row_drop_fails(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    con = duckdb.connect(str(cfg.warehouse))
    last, n = con.execute(
        "SELECT date, COUNT(*) FROM raw_perf_windows WHERE app_id = 'quartz_merge' "
        "GROUP BY 1 ORDER BY 1 DESC LIMIT 1"
    ).fetchone()
    drop = int(n * 0.7)
    con.execute(
        "DELETE FROM raw_perf_windows WHERE rowid IN (SELECT rowid FROM raw_perf_windows "
        "WHERE app_id = 'quartz_merge' AND date = ? ORDER BY rowid LIMIT ?)",
        [last, drop],
    )
    con.close()
    res = _run(cfg)
    assert _failed(res) == ["perf_window_volume"]
    r = res["perf_window_volume"]
    assert r.sample[0]["app_id"] == "quartz_merge"
    assert r.sample[0]["value"] == n - drop
    assert r.sample[0]["drop_share"] > 0.6


def test_reconciliation_checks(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    _mutate(
        cfg,
        "UPDATE fct_app_daily SET mau = dau - 1 WHERE rowid = (SELECT MAX(rowid) FROM fct_app_daily WHERE mau IS NOT NULL)",
        "UPDATE fct_retention SET retention = 1.2 WHERE rowid = (SELECT MAX(rowid) FROM fct_retention)",
        "UPDATE raw_ga4_active_users SET crashed_users = dau + 1 WHERE rowid = (SELECT MIN(rowid) FROM raw_ga4_active_users)",
    )
    failed = _failed(_run(cfg))
    assert {
        "dau_le_wau_le_mau",
        "retention_between_0_and_1",
        "ga4_crash_counts_within_denominators",
    } <= set(failed)


def _one(**kw) -> checks.Check:
    return checks.Check.from_dict({"name": "t", **kw}, {})


def test_each_check_type_on_a_tiny_table():
    con = warehouse.connect(":memory:")
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES ('a', 1, 0.5, DATE '2026-10-01'), "
        "('a', 1, 'NaN'::DOUBLE, DATE '2026-10-02'), ('z', NULL, 2.0, DATE '2026-10-03')) v(k, n, x, d)"
    )
    con.execute("CREATE TABLE p AS SELECT 'a' AS k")
    as_of = dt.date(2026, 10, 4)

    def status(**kw):
        return checks.run_check(con, _one(**kw), None, as_of)

    assert status(type="not_null", table="t", columns=["n"]).failing_rows == 1
    assert status(type="unique", table="t", columns=["k", "n"]).failing_rows == 1
    r = status(type="accepted_values", table="t", column="k", values=["a"])
    assert r.failing_rows == 1 and "z" in r.message
    r = status(type="range", table="t", column="x", min=0, max=1)
    assert r.failing_rows == 2  # 2.0 and NaN; the NULL-free 0.5 passes
    assert status(type="relationship", table="t", column="k", to="p.k").failing_rows == 1
    # NaN compares greater than every number in DuckDB, so it fails `x < 1` too
    assert status(type="expression", table="t", condition="x < 1").failing_rows == 2
    assert status(type="sql", sql="SELECT 1 WHERE FALSE").status == "pass"
    missing = status(type="unique", table="nope", columns=["k"])
    assert missing.status == "error" and missing.is_blocking
    assert status(type="unique", table="nope", columns=["k"], optional=True).status == "pass"
    con.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "unique", "table": "t", "columns": ["k"], "colour": "red"},
        {"type": "unique", "table": "t; DROP TABLE x", "columns": ["k"]},
        {"type": "unique", "table": "t", "columns": ["k) OR (1=1"]},
        {"type": "sql", "sql": "SELECT 1; DELETE FROM t"},
        {"type": "sql", "sql": "DELETE FROM t"},
        {"type": "range", "table": "t", "column": "x"},
        {"type": "relationship", "table": "t", "column": "k", "to": "p"},
        {"type": "nonsense", "table": "t"},
        {"type": "unique", "table": "t", "columns": ["k"], "severity": "fatal"},
    ],
)
def test_check_definitions_are_validated(bad):
    with pytest.raises(checks.CheckError):
        _one(**bad)


def test_results_are_stored_and_read_back(demo_build, tmp_path):
    cfg = demo_build.copy(tmp_path)
    con = warehouse.connect(cfg.warehouse)
    for _ in range(2):
        checks.store_results(con, checks.run_checks(con, CHECKS, cfg))
    runs = con.execute("SELECT COUNT(DISTINCT run_id) FROM meta_check_results").fetchone()[0]
    latest = checks.latest_results(con)
    con.close()
    assert runs >= 2
    assert len(latest) == len(CHECKS) and all(r.status == "pass" for r in latest)


def test_pulse_check_exit_codes(demo_build, tmp_path, capsys):
    cfg = demo_build.copy(tmp_path)
    cfg_path = cfg.path
    assert cli.main(["check", "-c", str(cfg_path)]) == 0
    warn_only = tmp_path / "warn.yaml"
    warn_only.write_text(
        "checks:\n  - {name: always_warn, type: sql, severity: warn, sql: 'SELECT 1 AS x'}\n",
        encoding="utf-8",
    )
    assert cli.main(["check", "-c", str(cfg_path), "--checks", str(warn_only)]) == 0
    assert cli.main(["check", "-c", str(cfg_path), "--checks", str(warn_only), "--strict"]) == 1
    _mutate(
        cfg,
        "INSERT INTO raw_revenuecat_transactions SELECT * FROM raw_revenuecat_transactions LIMIT 1",
    )
    capsys.readouterr()
    assert cli.main(["check", "-c", str(cfg_path), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["errors"] == 1
    assert [r["name"] for r in out["results"] if r["status"] == "fail"] == [
        "raw_revenuecat_rows_unique"
    ]


def test_run_fails_on_error_checks_and_shows_them(demo_build, tmp_path, capsys):
    cfg = demo_build.copy(tmp_path)
    cfg_path = cfg.path
    failing = tmp_path / "failing.yaml"
    failing.write_text(
        "checks:\n"
        "  - {name: never_true, type: expression, table: fct_app_daily, condition: 'FALSE'}\n"
        "  - {name: soft_one, type: sql, severity: warn, sql: 'SELECT 1 AS x'}\n",
        encoding="utf-8",
    )
    rc = cli.main(["run", "-c", str(cfg_path), "--checks", str(failing)])
    err = capsys.readouterr().err
    assert rc == 1 and "never_true" in err
    hb = runstate.read_heartbeat(cfg.state_dir)
    assert hb["status"] == "failed" and "never_true" in hb["error"]
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "Data quality" in html and "never_true" in html and "soft_one" in html
    con = duckdb.connect(str(cfg.warehouse), read_only=True)
    stored = con.execute(
        "SELECT name, status FROM meta_check_results ORDER BY checked_at DESC, name LIMIT 2"
    ).fetchall()
    con.close()
    assert sorted(stored) == [("never_true", "fail"), ("soft_one", "fail")]
    # a warn-level failure alone does not fail the run
    soft = tmp_path / "soft.yaml"
    soft.write_text(
        "checks:\n  - {name: soft_one, type: sql, severity: warn, sql: 'SELECT 1 AS x'}\n",
        encoding="utf-8",
    )
    assert cli.main(["run", "-c", str(cfg_path), "--checks", str(soft)]) == 0
    assert runstate.read_heartbeat(cfg.state_dir)["status"] == "success"


def test_failed_checks_are_notified_as_alerts():
    r = checks.CheckResult("x", "unique", "t", "error", "fail", 2, "2 duplicated key(s)")
    w = checks.CheckResult("y", "unique", "t", "warn", "fail", 1, "1 duplicated key(s)")
    ok = checks.CheckResult("z", "unique", "t", "error", "pass", 0, "ok")
    out = checks.as_alerts([r, w, ok], dt.date(2026, 11, 1))
    assert [(a.rule, a.severity) for a in out] == [("check:x", "critical"), ("check:y", "warning")]
    assert not math.isnan(out[0].value)


def test_packaged_default_files_are_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pipeline.default_file(None, None, "checks_path", "checks.yaml") is not None
    assert pipeline.default_file(None, None, "alerts_path", "alerts.yaml") is not None
