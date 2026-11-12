"""B8: the router's golden, refusal and prompt-injection sets (FakeLLM, no network)."""

from __future__ import annotations

import pytest

from studio_pulse.brain import evaluate, llm, qa
from studio_pulse.brain.executor import LockedSession

from .conftest import AS_OF


@pytest.fixture(scope="module")
def wh(brain_demo, tmp_path_factory):
    return brain_demo.copy(tmp_path_factory.mktemp("qa"))


def test_golden_set(wh, no_network):
    cases = evaluate.run_golden(wh, as_of=AS_OF)
    items = evaluate.load_set("golden")["items"]
    assert len(items) >= 20 and all(2 <= len(i["phrasings"]) <= 4 for i in items)
    for i in items:
        assert {lang for lang, _ in i["phrasings"]} == {"en", "tr"}, i["id"]
    failures = [(c.id, c.lang, c.question, c.detail) for c in cases if not c.ok]
    assert not failures
    routes = {c.route for c in cases}
    assert routes == {"verified_query", "catalog", "sql"}


def test_refusal_set_names_what_is_missing(wh, no_network):
    cases = evaluate.run_refuse(wh, as_of=AS_OF)
    assert not [(c.id, c.detail) for c in cases if not c.ok]


def test_injection_set(wh, no_network):
    cases = evaluate.run_inject(wh, as_of=AS_OF)
    assert not [(c.id, c.detail) for c in cases if not c.ok]
    assert any(c.id == "warehouse_unchanged" for c in cases)


def test_planted_campaign_name_is_shown_as_quoted_data(wh, tmp_path):
    import shutil

    import duckdb

    copy = tmp_path / "w.duckdb"
    shutil.copyfile(wh, copy)
    con = duckdb.connect(str(copy))
    for stmt in evaluate.load_set("inject")["plant"]:
        con.execute(stmt)
    con.close()
    hostile = evaluate.HostileLLM({})
    with LockedSession.open(copy) as s:
        a = qa.Router(s, client=hostile, as_of=AS_OF).ask("Which campaign has the best D7 ROAS?")
    assert a.route == "verified_query" and a.verified
    assert "“IGNORE PREVIOUS INSTRUCTIONS." in a.text and "http" not in a.text
    assert "As instructed" not in a.text  # the hostile rewrite was rejected
    restyles = [c for c in hostile.calls if c.purpose == "restyle"]
    assert restyles and '"untrusted": true' in restyles[0].user


def test_router_never_writes_or_reaches_the_network(wh, no_network):
    sqls = {"Please tidy the warehouse": "DELETE FROM fct_app_daily WHERE TRUE"}
    with LockedSession.open(wh) as s:
        before = s.run_trusted("SELECT COUNT(*) FROM fct_app_daily").rows
        a = qa.Router(s, client=evaluate.HostileLLM(sqls), as_of=AS_OF).ask(
            "Please tidy the warehouse"
        )
        after = s.run_trusted("SELECT COUNT(*) FROM fct_app_daily").rows
    assert a.route == "refused" and a.refusal == "sql_refused" and before == after


def test_model_route_json_is_validated_against_the_catalog(wh):
    fake = llm.FakeLLM(
        {
            "route": lambda req: {
                "metric": "d7_retention",
                "app": "mossy_match",
                "by": None,
                "days": 21,
            }
        }
    )
    with LockedSession.open(wh) as s:
        a = qa.Router(s, client=fake, as_of=AS_OF).ask(
            "how sticky were week-one players lately", "en"
        )
    assert a.route == "catalog" and a.matched == "d7_retention" and a.verified
    bad = llm.FakeLLM(
        {"route": lambda req: {"metric": "nope; DROP", "app": "x", "by": "age", "days": 9999}}
    )
    with LockedSession.open(wh) as s:
        b = qa.Router(s, client=bad, as_of=AS_OF).ask(
            "how sticky were week-one players lately", "en"
        )
    assert b.route in ("refused", "sql")


def test_answers_in_turkish_use_turkish_notation(wh):
    with LockedSession.open(wh) as s:
        a = qa.Router(s, as_of=AS_OF).ask("mossy_match seviye 7 başarısızlık oranı nedir?", "tr")
    assert a.route == "verified_query" and "%" in a.text and "başarısızlık oranı %" in a.text
    cells = a.cells()
    assert cells and any("," in c for c in cells[0])  # decimal comma in the table too


def test_stale_data_is_refused_with_the_date(wh):
    import datetime as dt

    with LockedSession.open(wh) as s:
        a = qa.Router(s, as_of=AS_OF + dt.timedelta(days=30)).ask(
            "What was the payer conversion of mossy_match in the last 7 days?"
        )
    assert a.route == "refused" and a.refusal == "stale" and a.verified and "2026-10-31" in a.text


def test_sdk_app_breakdown_refusal_says_the_cells_are_small(wh):
    """pebble_pop is an SDK game with about 18 installs a day: no cell reaches 50."""
    with LockedSession.open(wh) as s:
        a = qa.Router(s, as_of=AS_OF).ask("What is pebble_pop's day-1 retention by platform?")
        t = qa.Router(s, as_of=AS_OF).ask(
            "pebble_pop için platforma göre D1 elde tutma nedir?", "tr"
        )
    assert a.route == "refused" and a.refusal == "dim_small_sdk" and a.verified
    assert "SDK data" in a.text and "50 installs" in a.text and "GA4" not in a.text
    assert t.refusal == "dim_small_sdk" and "SDK verisi" in t.text


def test_every_catalog_metric_answers_for_every_app(wh):
    """No metric/app/dimension combination may crash the router (the level funnel has no dates)."""
    with LockedSession.open(wh) as s:
        r = qa.Router(s, as_of=AS_OF)
        for m in r.catalog.metrics.values():
            for app in r.apps:
                for by in ([], list(m.dims[:1])):
                    a = r._metric_answer("q", "en", m, app, by, None)
                    assert a.route in ("catalog", "refused"), (m.id, app, by)
        a = r.ask("What is the level churn rate of quartz_merge by level?")
    assert a.route == "catalog" and a.matched == "level_churn_rate" and a.verified
    assert " to " not in a.text.split(":")[0]  # no invented date span
