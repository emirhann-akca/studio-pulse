"""B1: the AST guard and the locked DuckDB session.

Reproduces every case of the research's evidence script (ek_koruma_kaniti.py:
8 AST cases, 3 engine cases) plus the attacks the script did not try.
"""

from __future__ import annotations

import time

import duckdb
import pytest

from studio_pulse.brain import guard
from studio_pulse.brain.executor import LockedSession, QueryTimeout


@pytest.fixture()
def session(brain_demo, tmp_path):
    # a folder with Turkish characters: Windows paths must work as well
    path = brain_demo.copy(tmp_path / "Türkçe Şık klasör")
    s = LockedSession.open(path, timeout_s=2.0)
    yield s
    s.close()


GOOD = "SELECT level, win_rate, churn_rate FROM fct_level_funnel WHERE app_id = 'quartz_merge' ORDER BY win_rate LIMIT 3"


def test_evidence_script_ast_cases(session):
    r = session.run_untrusted(GOOD)  # 1 good query: qualified, 3 rows
    assert len(r.rows) == 3 and "fct_level_funnel.win_rate" in r.sql
    r = session.run_untrusted("SELECT app_id, date, dau FROM fct_app_daily")  # 2 no LIMIT
    assert r.sql.rstrip().endswith("LIMIT 500")
    refused = {
        "unlisted table": "SELECT * FROM meta_loads",
        "write": "DELETE FROM fct_app_daily",
        "two statements": "SELECT 1; DROP TABLE fct_app_daily",
        "file read": "SELECT * FROM read_csv('/etc/passwd')",
        "invented column": "SELECT level, fail_rate FROM fct_level_funnel",
        "attach": "ATTACH '/tmp/x.duckdb' AS x",
    }
    for name, sql in refused.items():
        with pytest.raises(guard.Refused):
            session.run_untrusted(sql)
        assert name


def test_invented_column_names_the_column(session):
    with pytest.raises(guard.Refused, match="fail_rate"):
        session.run_untrusted("SELECT level, fail_rate FROM fct_level_funnel")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO fct_app_daily SELECT * FROM fct_app_daily",
        "UPDATE fct_app_daily SET dau = 0",
        "CREATE TABLE x AS SELECT 1",
        "DROP TABLE fct_app_daily",
        "ALTER TABLE fct_app_daily ADD COLUMN x INT",
        "COPY fct_app_daily TO 'out.csv'",
        "EXPORT DATABASE 'dump'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "SET enable_external_access = true",
        "CALL pragma_version()",
        "DESCRIBE fct_app_daily",
        "SHOW TABLES",
        "SUMMARIZE fct_app_daily",
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM read_parquet('x.parquet')",
        "SELECT * FROM glob('*')",
        "SELECT * FROM range(10)",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM query_table('meta_loads')",
        "SELECT getenv('HOME')",
        "SELECT json_execute_serialized_sql('x')",
        "SELECT * FROM fct_app_daily, read_csv('x.csv')",
        "SELECT (SELECT COUNT(*) FROM meta_loads) AS n",
        "SELECT app_id FROM fct_app_daily UNION SELECT detail FROM meta_loads",
        "SELECT * FROM other.main.fct_app_daily",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM raw_store_reviews",
        "SELECT * INTO new_table FROM fct_app_daily",
        "WITH x AS (SELECT * FROM meta_run) SELECT * FROM x",
        "",
        "SELEC dau FROM fct_app_daily",
    ],
)
def test_malicious_and_broken_queries_are_refused(session, sql):
    with pytest.raises(guard.Refused):
        session.run_untrusted(sql)


def test_allowed_shapes_run(session):
    for sql in (
        "WITH a AS (SELECT app_id, avg(dau) AS d FROM fct_app_daily GROUP BY ALL) SELECT * FROM a ORDER BY d DESC",
        "SELECT app_id, median(dau), quantile_cont(dau, 0.9), arg_max(date, dau) FROM fct_app_daily GROUP BY app_id",
        "SELECT * FROM fct_app_daily QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY date DESC) = 1",
        "SELECT app_id FROM fct_app_daily UNION SELECT app_id FROM meta_apps",
        'SELECT "dau" FROM main.fct_app_daily LIMIT 2',
    ):
        assert session.run_untrusted(sql).rows


def test_limit_is_capped_and_kept(session):
    assert session.run_untrusted("SELECT * FROM fct_app_daily LIMIT 100000").sql.endswith(
        "LIMIT 500"
    )
    assert session.run_untrusted("SELECT * FROM fct_app_daily LIMIT 7").sql.endswith("LIMIT 7")
    res = LockedSession(session.con, max_rows=5).run_untrusted("SELECT * FROM fct_app_daily")
    assert len(res.rows) == 5


def test_engine_layer_holds_when_the_guard_is_bypassed(session):
    """The research's three engine cases, executed directly on the locked connection."""
    con = session.con
    with pytest.raises(duckdb.PermissionException):
        con.execute("SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(duckdb.Error, match="(?i)configuration"):
        con.execute("SET enable_external_access = true")
    with pytest.raises(duckdb.Error, match="(?i)read-only|read only"):
        con.execute("CREATE TABLE x AS SELECT 1")
    with pytest.raises(duckdb.Error):
        con.execute("SET lock_configuration = false")
    with pytest.raises(duckdb.Error):
        con.execute("LOAD httpfs")


def test_slow_query_is_interrupted(session):
    t0 = time.perf_counter()
    with pytest.raises(QueryTimeout):
        session.run_untrusted(
            "SELECT COUNT(*) FROM fct_app_daily a, fct_app_daily b, fct_app_daily c, "
            "fct_app_daily d, fct_app_daily e"
        )
    assert time.perf_counter() - t0 < 10
    assert session.run_untrusted("SELECT COUNT(*) FROM meta_apps").rows  # still usable


def test_in_memory_database_is_refused():
    with pytest.raises(ValueError):
        LockedSession.open(":memory:")


def test_llm_sql_is_never_transpiled_to_bigquery():
    """No brain module may render untrusted SQL for BigQuery (research 4.2)."""
    from pathlib import Path

    import studio_pulse.brain as brain

    for f in Path(brain.__file__).parent.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "transpile(" not in text, f
        assert 'write="bigquery"' not in text and "write='bigquery'" not in text, f
