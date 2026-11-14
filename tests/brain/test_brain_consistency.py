"""Question-evidence consistency (brain/consistency.py): an answer must cover every filter
and the period the question names, not only verify its numbers against its own evidence."""

from __future__ import annotations

import datetime as dt

import pytest

from studio_pulse.brain import consistency, intent, llm
from studio_pulse.brain.executor import LockedSession
from studio_pulse.brain.qa import Router

from .conftest import AS_OF

APPS = {"mossy_match": "Mossy Match", "quartz_merge": "Quartz Merge", "pacer_pal": "Pacer Pal"}


def _it(q: str) -> intent.Intent:
    return intent.parse(q, apps=APPS, as_of=AS_OF)


def _check(q: str, sql: str, params=None, columns=None, rows=None, **kw):
    return consistency.check(
        _it(q), sql=sql, params=params or [], columns=columns or [], rows=rows or [], n_apps=3, **kw
    )


def test_a_dropped_network_filter_is_a_gap():
    """The audit's case: the CPI blend of every network for a question about Meta."""
    sql = (
        "SELECT app_id, SUM(spend_usd) / SUM(network_installs) AS value FROM fct_ua_daily "
        "WHERE app_id = ? AND date >= ? AND date <= ? GROUP BY 1"
    )
    params = ["quartz_merge", dt.date(2026, 10, 25), dt.date(2026, 10, 31)]
    gaps = _check("What is the CPI on Meta for quartz_merge?", sql, params)
    assert [(g.dim, g.reason) for g in gaps] == [("network", "not_applied")]
    other = _check(
        "What is the CPI on Meta for quartz_merge?",
        sql.replace("WHERE", "WHERE network = 'tiktok' AND"),
        params,
    )
    assert [(g.dim, g.reason, g.found) for g in other] == [("network", "other_value", ["tiktok"])]
    ok = _check(
        "What is the CPI on Meta for quartz_merge?",
        sql.replace("WHERE", "WHERE CAST(network AS VARCHAR) IN ('meta') AND"),
        params,
    )
    assert ok == []


def test_rows_platform_implied_and_country_names():
    rows_ok = _check(
        "Compare iOS and Android D1 for mossy_match",
        "SELECT platform, SUM(users) FROM stg_cohort_days WHERE app_id = 'mossy_match' GROUP BY 1",
        columns=["platform", "n"],
        rows=[["IOS", 1], ["ANDROID", 2]],
    )
    assert rows_ok == []
    vitals = _check(
        "Android ANR rate of mossy_match on Google Play",
        "SELECT AVG(anr_rate) FROM fct_android_vitals_daily WHERE app_id = 'mossy_match'",
    )
    assert vitals == []  # every vitals row is Android / Google Play
    names = _check(
        "D1 in the United States for mossy_match",
        "SELECT 1 FROM stg_cohort_dims WHERE app_id = 'mossy_match' AND country = 'United States'",
    )
    codes = _check(
        "spend in Turkey for mossy_match",
        "SELECT 1 FROM stg_ua_spend WHERE app_id = 'mossy_match' AND country = 'TR'",
    )
    assert names == [] and codes == []
    wrong_app = _check(
        "DAU of mossy_match", "SELECT dau FROM fct_app_daily WHERE app_id = 'quartz_merge'"
    )
    assert [(g.dim, g.reason) for g in wrong_app] == [("app", "other_value")]


def test_period_must_match():
    newest = dt.date(2026, 10, 31)
    sql = "SELECT SUM(x) FROM fct_app_daily WHERE app_id = ? AND date >= ? AND date <= ?"
    seven = ["mossy_match", dt.date(2026, 10, 25), newest]
    thirty = ["mossy_match", dt.date(2026, 10, 2), newest]
    assert _check("revenue of mossy_match in the last 7 days", sql, seven, newest=newest) == []
    gaps = _check("revenue of mossy_match in the last 7 days", sql, thirty, newest=newest)
    assert [g.dim for g in gaps] == ["period"]
    month = ["mossy_match", dt.date(2026, 10, 1), dt.date(2026, 10, 31)]
    assert _check("revenue of mossy_match in October 2026", sql, month, newest=newest) == []
    assert _check("revenue of mossy_match in September 2026", sql, month, newest=newest)
    # a snapshot table answers its own window only
    snap = "SELECT network_cpi_30d_usd FROM fct_ua_summary WHERE app_id = 'mossy_match'"
    assert _check("CPI of mossy_match over the last 30 days", snap) == []
    assert [g.dim for g in _check("CPI of mossy_match over the last 7 days", snap)] == ["period"]


def test_bind_leaves_quoted_question_marks_alone():
    out = consistency.bind(
        "SELECT '?' AS q, x FROM t WHERE a = ? AND d = ?", ["it's", dt.date(2026, 1, 2)]
    )
    assert out == "SELECT '?' AS q, x FROM t WHERE a = 'it''s' AND d = DATE '2026-01-02'"


@pytest.fixture(scope="module")
def wh(brain_demo, tmp_path_factory):
    return brain_demo.copy(tmp_path_factory.mktemp("consistency"))


def test_cpi_on_meta_for_quartz_merge_is_refused_with_the_reason(wh, no_network):
    with LockedSession.open(wh) as s:
        r = Router(s, as_of=AS_OF)
        en = r.ask("What is the CPI on Meta for quartz_merge?", "en")
        tr = r.ask("quartz_merge'ün Meta'daki CPI'ı ne?", "tr")
        ok = r.ask("What is the CPI on Meta for mossy_match over the last 30 days?", "en")
        truth = s.run_trusted(
            "SELECT SUM(spend_usd) / SUM(network_installs) FROM stg_ua_spend WHERE app_id = "
            "'mossy_match' AND network = 'meta' AND date BETWEEN DATE '2026-10-02' AND "
            "DATE '2026-10-31'"
        ).rows[0][0]
    assert en.route == "refused" and en.refusal == "no_such_value" and en.verified
    assert "Meta" in en.text and "tiktok" in en.text and "0.413" not in en.text
    assert tr.route == "refused" and "Meta verisi yok" in tr.text
    assert ok.route == "catalog" and ok.verified
    assert ok.rows[0][ok.columns.index("value")] == pytest.approx(truth, rel=1e-9)
    assert "network" in ok.sql


def test_a_model_that_drops_a_filter_is_refused(wh, no_network):
    """The same class of error with a model behind the router: its SQL is checked too."""
    q = "Which day had the most sessions on iOS for mossy_match?"
    fake = llm.FakeLLM(
        {
            "sql": {
                q: {
                    "sql": "SELECT date, sessions FROM fct_app_daily WHERE app_id = 'mossy_match' "
                    "ORDER BY sessions DESC LIMIT 1"
                }
            }
        }
    )
    with LockedSession.open(wh) as s:
        a = Router(s, client=fake, as_of=AS_OF).ask(q, "en")
    assert a.route == "refused" and a.refusal == "filter_not_applied"
    assert "platform = iOS" in a.text and a.verified
    assert any(n.startswith("consistency:") for n in a.notes)
    sql_calls = [c for c in fake.calls if c.purpose == "sql"]
    assert sql_calls and '"newest_data_day"' in sql_calls[0].user and "rules" in sql_calls[0].user


def test_filters_are_applied_instead_of_dropped(wh, no_network):
    with LockedSession.open(wh) as s:
        r = Router(s, as_of=AS_OF)
        ios = r.ask("How many iOS users did mossy_match have on 2026-10-31?", "en")
        tr = r.ask("pacer_pal'ın ABD'deki son 30 günlük Meta harcaması", "tr")
        absent = r.ask("pacer_pal CPI on iOS in the last 30 days", "en")
        de = r.ask("D7 retention of quartz_merge in Germany", "en")
        want_ios = s.run_trusted(
            "SELECT dau FROM stg_active_users WHERE app_id = 'mossy_match' AND platform = 'IOS' "
            "AND date = DATE '2026-10-31'"
        ).rows[0][0]
        want_us = s.run_trusted(
            "SELECT SUM(spend_usd) FROM stg_ua_spend WHERE app_id = 'pacer_pal' AND network = 'meta' "
            "AND country = 'US' AND date BETWEEN DATE '2026-10-02' AND DATE '2026-10-31'"
        ).rows[0][0]
    assert ios.route == "catalog" and ios.rows[0][ios.columns.index("value")] == want_ios
    assert tr.route == "catalog" and tr.rows[0][tr.columns.index("value")] == pytest.approx(want_us)
    assert absent.route == "refused" and absent.refusal in ("no_filter_rows", "no_such_value")
    assert de.route == "refused" and "Germany" in de.text
