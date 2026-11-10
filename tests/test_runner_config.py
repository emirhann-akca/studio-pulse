"""Model runner ordering/idempotence and config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from studio_pulse import runner, warehouse
from studio_pulse.config import ConfigError, config_from_dict


def test_models_run_in_dependency_order():
    order = [m.name for m in runner.resolve_order(runner.discover())]
    pos = {n: i for i, n in enumerate(order)}
    assert pos["stg_active_users"] < pos["fct_app_daily"] < pos["fct_app_summary"]
    assert pos["fct_retention"] < pos["fct_retention_summary"] < pos["fct_app_summary"]
    assert pos["stg_rc_transactions"] < pos["fct_ltv_curve"]
    for m in runner.discover():
        assert m.description, f"{m.name} needs a header comment"


def test_cycle_is_detected(tmp_path: Path):
    (tmp_path / "a.sql").write_text("SELECT * FROM b", encoding="utf-8")
    (tmp_path / "b.sql").write_text("SELECT * FROM a", encoding="utf-8")
    with pytest.raises(runner.ModelError, match="cycle"):
        runner.resolve_order(runner.discover(tmp_path))


def test_dependencies_ignore_comments_and_strings(tmp_path: Path):
    (tmp_path / "a.sql").write_text("-- built from b\nSELECT 'b' AS x", encoding="utf-8")
    (tmp_path / "b.sql").write_text("SELECT 1 AS y", encoding="utf-8")
    ms = {m.name: m for m in runner.discover(tmp_path)}
    assert ms["a"].depends_on == set()


def test_runner_is_idempotent(tmp_path: Path):
    con = warehouse.connect(tmp_path / "w.duckdb")
    first = {r.name: r.rows for r in runner.run_models(con)}
    second = {r.name: r.rows for r in runner.run_models(con)}
    assert first == second
    con.close()


def test_failed_model_reports_file(tmp_path: Path):
    (tmp_path / "broken.sql").write_text(
        "-- broken on purpose\nSELECT * FROM does_not_exist", encoding="utf-8"
    )
    con = warehouse.connect()
    with pytest.raises(runner.ModelError, match="broken.sql"):
        runner.run_models(con, tmp_path)


def test_config_env_expansion(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PULSE_TEST_PROJECT", "my-project")
    cfg = config_from_dict(
        {
            "warehouse": "local/w.duckdb",
            "apps": [
                {
                    "id": "game_one",
                    "sources": {
                        "ga4_bigquery": {
                            "project": "${PULSE_TEST_PROJECT}",
                            "dataset": "${DS:-analytics_1}",
                        }
                    },
                }
            ],
        },
        base_dir=tmp_path,
    )
    src = cfg.apps[0].sources["ga4_bigquery"]
    assert src == {"project": "my-project", "dataset": "analytics_1"}
    assert cfg.warehouse == tmp_path / "local" / "w.duckdb"
    start, end = cfg.window()
    assert (end - start).days == cfg.lookback_days - 1


def test_config_errors(tmp_path: Path):
    with pytest.raises(ConfigError):
        config_from_dict({"apps": []}, tmp_path)
    with pytest.raises(ConfigError):
        config_from_dict({"apps": [{"id": "Bad-Id"}]}, tmp_path)
    with pytest.raises(ConfigError, match="NOT_SET_ANYWHERE"):
        config_from_dict({"apps": [{"id": "a", "name": "${NOT_SET_ANYWHERE}"}]}, tmp_path)


def test_app_timezone_is_validated(tmp_path: Path):
    ok = config_from_dict({"apps": [{"id": "a", "timezone": "Europe/Istanbul"}]}, base_dir=tmp_path)
    assert ok.apps[0].timezone == "Europe/Istanbul"
    assert config_from_dict({"apps": [{"id": "a"}]}, base_dir=tmp_path).apps[0].timezone == "UTC"
    for bad in ("Mars/Olympus", "UTC'; --", "../etc/localtime"):
        with pytest.raises(ConfigError):
            config_from_dict({"apps": [{"id": "a", "timezone": bad}]}, base_dir=tmp_path)
