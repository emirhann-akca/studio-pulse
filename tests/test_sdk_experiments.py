"""fct_sdk_experiments: the warehouse side of the SDK's experiment assignment.

The bucket is recomputed in SQL from the experiment key and install id and must equal the
bucket studio_pulse.assign (and the two SDKs, tested against GrowthBook's own cases) gives;
an install whose reported bucket differs, or that reported two variations, is flagged.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from studio_pulse.assign import assign, hash_value

MODEL = Path(__file__).resolve().parents[1] / "models" / "fct_sdk_experiments.sql"


def _db(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE stg_pulse_events (app_id VARCHAR, install_id VARCHAR, ts TIMESTAMP, "
        "name VARCHAR, params JSON, is_prod BOOLEAN)"
    )
    con.executemany("INSERT INTO stg_pulse_events VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE fct_sdk_experiments AS " + MODEL.read_text(encoding="utf-8"))
    return con


def _exposure(install: str, key: str, ts: str, variation=None, bucket=None, in_exp=None, prod=True):
    v, b = assign(key, install)
    p = {
        "experiment_key": key,
        "variation": max(v, 0) if variation is None else variation,
        "in_experiment": (v >= 0) if in_exp is None else in_exp,
        "hash_value": b if bucket is None else bucket,
    }
    return ("puzzle", install, ts, "experiment_exposure", json.dumps(p), prod)


@pytest.mark.parametrize(
    "key,install",
    [("", "a"), ("booster_offer", "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"), ("x", "ü-ğ-ş")],
)
def test_sql_bucket_equals_the_python_and_sdk_bucket(key, install):
    con = _db([_exposure(install, key, "2026-11-01 10:00:00")])
    got = con.execute("SELECT hash_mismatch_installs FROM fct_sdk_experiments").fetchall()
    assert got == [(0,)], hash_value(key, install)


def test_split_switches_and_mismatches_are_counted():
    rows = [_exposure(f"install-{i}", "onboarding_v2", "2026-11-01 10:00:00") for i in range(400)]
    rows.append(_exposure("install-0", "onboarding_v2", "2026-11-02 10:00:00"))  # same again: fine
    v0, _ = assign("onboarding_v2", "install-1")
    rows.append(  # install-1 later reports the other variation: switched
        _exposure("install-1", "onboarding_v2", "2026-11-02 11:00:00", variation=1 - v0)
    )
    rows.append(_exposure("bad", "onboarding_v2", "2026-11-01 12:00:00", bucket=0.0001))
    rows.append(_exposure("debug", "onboarding_v2", "2026-11-01 12:00:00", prod=False))
    con = _db(rows)
    arms = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT variation, installs, share, switched_installs, hash_mismatch_installs "
            "FROM fct_sdk_experiments ORDER BY variation"
        ).fetchall()
    }
    assert set(arms) == {0, 1}  # coverage 1: nobody outside, the debug build left out
    assert sum(a[0] for a in arms.values()) == 401  # 400 + "bad"
    assert all(0.4 < a[1] < 0.6 for a in arms.values())
    assert sum(a[2] for a in arms.values()) == 1  # install-1
    assert sum(a[3] for a in arms.values()) == 1  # "bad"
    flagged = con.execute(
        "SELECT COUNT(*) FROM fct_sdk_experiments "
        "WHERE switched_installs > 0 OR hash_mismatch_installs > 0"
    ).fetchone()[0]
    assert flagged >= 1


def test_installs_outside_the_coverage_are_variation_minus_one():
    con = _db([_exposure("i1", "tiny", "2026-11-01 10:00:00", variation=0, in_exp=False)])
    assert con.execute("SELECT variation, installs, share FROM fct_sdk_experiments").fetchall() == [
        (-1, 1, None)
    ]
