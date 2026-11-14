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
    """pebble_pop is an SDK game with about 18 installs a day: no cohort_dims cell reaches
    50. The platform split comes from stg_cohort_days instead (every app has it); the other
    dimensions are refused with the reason."""
    with LockedSession.open(wh) as s:
        a = qa.Router(s, as_of=AS_OF).ask("What is pebble_pop's day-1 retention by country?")
        t = qa.Router(s, as_of=AS_OF).ask("pebble_pop için ülkeye göre D1 elde tutma nedir?", "tr")
        p = qa.Router(s, as_of=AS_OF).ask("What is pebble_pop's day-1 retention by platform?")
    assert a.route == "refused" and a.refusal == "dim_small_sdk" and a.verified
    assert "SDK data" in a.text and "50 installs" in a.text and "GA4" not in a.text
    assert t.refusal == "dim_small_sdk" and "SDK verisi" in t.text
    assert p.route == "catalog" and "platform" in p.columns and "stg_cohort_days" in p.sql
    assert p.verified


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


def test_a_one_row_generated_answer_says_the_value(wh, no_network):
    """The SQL route used to answer "the checked query returned 1 row(s)" and leave the
    number in the table; a one-row result is now stated, and still passes the verifier."""
    with LockedSession.open(wh) as s:
        r = qa.Router(s, client=evaluate._fake_for_golden(), as_of=AS_OF)
        en = r.ask("On which day did mossy_match have its highest DAU?", "en")
        tr = r.ask(
            "quartz_merge oyununda kazanma oranı yüzde 50'nin altında olan kaç seviye var?", "tr"
        )
    assert en.route == "sql" and en.verified
    day, dau = en.rows[0][0], en.rows[0][1]
    assert f"date {day}" in en.text and f"dau {dau:,}" in en.text
    assert tr.route == "sql" and tr.verified and f"Sonuç: levels {tr.rows[0][0]}." in tr.text


def test_explicit_periods_are_read_from_the_question():
    import datetime as dt

    last = dt.date(2026, 10, 31)
    fr = lambda q: qa.Router.find_range(qa.normalize(q), last)  # noqa: E731
    assert fr("revenue on 2026-10-31") == (dt.date(2026, 10, 31), dt.date(2026, 10, 31))
    assert fr("between 2026-10-03 and 2026-10-01") == (dt.date(2026, 10, 1), dt.date(2026, 10, 3))
    assert fr("What was revenue in October 2026?") == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert fr("pacer_pal ekim 2026'da kaç kullanıcı") == (
        dt.date(2026, 10, 1),
        dt.date(2026, 10, 31),
    )
    assert fr("ağustosta gelir") == (dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    assert fr("kasımda gelir") == (
        dt.date(2025, 11, 1),
        dt.date(2025, 11, 30),
    )  # after the data: last year
    assert fr("revenue in may") is None  # "may" alone is a verb
    assert fr("revenue in the last 7 days") is None


def test_named_period_metric_and_app_reach_the_right_answer(wh, no_network):
    """Found with a real model on new questions (14 Nov): a month was answered with the
    last 7 days, "money spent" with revenue, campaigns were "not a breakdown", and a
    question about one app got a portfolio ranking."""
    with LockedSession.open(wh) as s:
        r = qa.Router(s, client=evaluate._fake_for_golden(), as_of=AS_OF)
        oct_rev = r.ask("What was quartz_merge's total revenue in October 2026?", "en")
        last7 = r.ask(
            "Average crash-free users rate of quartz_merge in the last 7 days of October 2026?",
            "en",
        )
        camp = r.ask("Which UA campaign spent the most money in October 2026?", "en")
        share = r.ask("What share of mossy_match's revenue in October 2026 came from ads?", "en")
        day = r.ask(
            "mossy_match'in 2026-10-31 tarihindeki günlük aktif kullanıcı sayısı kaç?", "tr"
        )
    assert oct_rev.route == "catalog" and "2026-10-01" in oct_rev.text and oct_rev.verified
    assert "2026-10-25" in last7.text and last7.matched == "crash_free_users"
    assert camp.matched == "ua_spend_usd" and "campaign_name" in camp.columns and camp.verified
    assert share.matched == "ad_revenue_share" and share.verified
    assert day.route == "catalog" and "2026-10-31" in day.text and len(day.rows) == 1


def test_top_n_overall_and_the_small_sample_caveat_follow_the_question(wh, no_network):
    """Third round (14 Nov, morning): "top 3 hardest levels" names three levels, "average
    rating" reads the whole history instead of the last week, and the small-sample caveat
    is about the apps the answer names, not every app in the warehouse."""
    with LockedSession.open(wh) as s:
        r = qa.Router(s, client=evaluate._fake_for_golden(), as_of=AS_OF)
        top = r.ask("What are the top 3 hardest levels in mossy_match?", "en")
        one = r.ask("What is the hardest level of mossy_match?", "en")
        cmp_ = r.ask("Compare D1 retention of mossy_match and quartz_merge in October 2026", "en")
        week = r.ask("What is the D1 retention of quartz_merge?", "en")
        avg = r.ask("What is the average D1 retention of quartz_merge?", "en")
        why = r.ask("Why did quartz_merge's D1 retention drop?", "en")
    # "why": driver analysis (drivers.py) names the release and keeps the caveat
    assert why.matched == "d1_retention:why" and why.verified and "drivers" in why.notes
    assert "1.8.0" in why.text and "Association in time" in why.text
    assert top.matched == "hardest_levels" and top.verified
    assert "Next:" in top.text and top.text.count("level") >= 3
    assert "Next:" not in one.text
    assert "small_sample" not in cmp_.notes and "Small sample" not in cmp_.text
    first = lambda a: min(row[a.columns.index("first_day")] for row in a.rows)  # noqa: E731
    assert avg.verified and first(avg) < first(week)  # the whole history, not the last 14 days
    assert qa._wanted_count("en zor 3 seviye") == 3 and qa._wanted_count("hardest level") == 1


def test_compare_trend_platform_and_ranking_questions(wh, no_network):
    """Second round of real-model questions (14 Nov): comparisons answered about one app,
    "going up or down" with a mean, "android and ios" without the platform split, the
    app ranking refused, D30 answered with D1, crash-free by version answered with D1."""
    with LockedSession.open(wh) as s:
        r = qa.Router(s, client=evaluate._fake_for_golden(), as_of=AS_OF)
        cmp_ = r.ask("Compare D1 retention of mossy_match and quartz_merge in October 2026", "en")
        trend = r.ask("Is mossy_match's DAU going up or down over the last 30 days?", "en")
        plat = r.ask("mossy_match'te android ve ios D1 elde tutmayı karşılaştır", "tr")
        rank = r.ask("Hangi uygulamanın günlük aktif kullanıcısı en yüksek?", "tr")
        d30 = r.ask("mossy_match'in D30 elde tutması kaç?", "tr")
        ver = r.ask("Which app version of quartz_merge has the lowest crash-free users rate?", "en")
    apps = {row[cmp_.columns.index("app_id")] for row in cmp_.rows}
    assert apps == {"mossy_match", "quartz_merge"} and cmp_.verified
    assert (
        "Trend:" in trend.text
        and trend.verified
        and ("up" in trend.text or "down" in trend.text or "flat" in trend.text)
    )
    assert "platform" in plat.columns and plat.verified
    assert rank.route == "catalog" and rank.matched == "dau" and len(rank.rows) >= 2
    assert d30.matched == "d30_retention"
    assert ver.route == "refused"  # no crash-free by version in this warehouse: said, not guessed
