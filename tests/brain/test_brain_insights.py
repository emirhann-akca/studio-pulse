"""B2 acceptance on `pulse demo` data: the planted defects are found, clean areas stay clean."""

from __future__ import annotations

from collections import Counter

import duckdb
import pytest

from studio_pulse.brain import evidence, insights, narrate
from studio_pulse.simulate.model import GAMES

SDK_APPS = {"pebble_pop", "demo_tidyhabit", "demo_lanternleap"}  # played through the collector

PLANTED_KINDS_QUARTZ = {
    "anomaly",
    "crash_regression",
    "perf_regression",
    "difficult_level",
    "level_survival",
}


def by_kind(rep, kind, app=None):
    return [i for i in rep.insights if i.kind == kind and (app is None or i.app_id == app)]


def planted_hard_levels(con) -> set[tuple[str, int]]:
    """Levels the demo made hard, among those with enough plays to judge.

    GA4-shaped apps (demo/generate.py): every 7th level. SDK games played
    through the collector (simulate/model.py): their `spikes`; the demo maps
    demo.pebblepop to the configured app pebble_pop, other bundles register
    themselves with dots turned into underscores.
    """
    sdk = {g.app_id: set(g.spikes) for g in GAMES}
    sdk_apps = {b.replace(".", "_"): s for b, s in sdk.items()} | {
        "pebble_pop": sdk["demo.pebblepop"]
    }
    rows = con.execute(
        "SELECT app_id, level FROM fct_level_funnel WHERE completes + fails >= 200"
    ).fetchall()
    judged = Counter(a for a, _ in rows)  # the job needs 5 judged levels for a baseline
    return {
        (a, lvl)
        for a, lvl in rows
        if judged[a] >= 5 and (lvl in sdk_apps[a] if a in sdk_apps else lvl % 7 == 0)
    }


def test_difficult_levels_precision_and_recall_are_one(brain_demo, demo_report):
    con = duckdb.connect(str(brain_demo.warehouse), read_only=True)
    planted = planted_hard_levels(con)
    con.close()
    found = {(i.app_id, i.facts()["level"].value) for i in by_kind(demo_report, "difficult_level")}
    assert found == planted
    assert {lvl for _, lvl in found} >= {7, 14, 21, 28}
    # the SDK path: level funnel built from collector events, spikes from the simulator
    assert {lvl for a, lvl in found if a == "pebble_pop"} == {12, 25, 40}


def test_quartz_level_7_matches_the_research_numbers(demo_report):
    ins = next(i for i in demo_report.insights if i.id == "difficult_level:quartz_merge:7")
    f = ins.facts()
    assert f["fail_rate"].value == pytest.approx(0.609, abs=0.005)
    assert f["med_fail"].value == pytest.approx(0.205, abs=0.005)
    assert f["fail_lo"].value > f["med_fail"].value
    assert f["robust_z"].value >= 3
    assert ins.q is not None and ins.q <= 0.10


def test_release_1_8_0_regressions_are_found(demo_report):
    perf = by_kind(demo_report, "perf_regression", "quartz_merge")
    top = perf[0]
    f = top.facts()
    assert (f["version_new"].value, f["tier"].value) == ("1.8.0", "low")  # the top perf card
    assert f["rel"].value > 0.25 and "gpu_bound" in top.main.caveats
    assert {p.facts()["tier"].value for p in perf} <= {"low", "mid"}  # high tier unchanged
    crash = by_kind(demo_report, "crash_regression", "quartz_merge")
    assert len(crash) == 1 and crash[0].facts()["version"].value == "1.8.0"
    assert crash[0].facts()["crash_after"].value > 3 * crash[0].facts()["crash_before"].value


def test_d1_drop_alert_is_explained(demo_report):
    ins = next(i for i in demo_report.insights if i.id == "anomaly:quartz_merge:d1_retention_drop")
    assert ins.template == "retention_drop_cause" and ins.severity == "warning"
    assert not ins.causal  # an association in time, worded as such


def test_clean_areas_are_not_flagged(demo_report):
    """No false alarms: pacer_pal and mossy_match have no planted release or money defects."""
    flagged = [
        i for i in demo_report.insights
        if i.severity in ("warning", "critical") and i.kind not in ("difficult_level",)
    ]  # fmt: skip
    assert {i.app_id for i in flagged} == {"quartz_merge"}
    assert not by_kind(demo_report, "monetization_shift")
    # GA4-shaped apps are clean. The SDK games are small (about 20 installs a
    # day), so they get the small-sample notice and nothing else.
    trust_cards = by_kind(demo_report, "data_trust")
    assert {i.app_id for i in trust_cards} == SDK_APPS
    assert all(i.severity == "info" and i.main.caveats == ["small_sample"] for i in trust_cards)
    assert demo_report.errors == {}


def test_every_finding_carries_method_sql_and_numbers(demo_report):
    for ins in demo_report.insights:
        main = ins.main
        assert main.method and main.id
        if ins.kind != "level_survival":
            assert main.sql, ins.id
        if ins.p is not None and ins.kind != "anomaly":  # alerts are rules, not scanned tests
            assert ins.q is not None and ins.q <= 0.10, ins.id


@pytest.mark.parametrize("lang", ["tr", "en"])
def test_all_narrations_verify_and_cite_evidence(demo_report, lang):
    for ins in demo_report.insights:
        n = narrate.template_narration(ins, lang)
        assert n.verified, (ins.id, n.unsupported)
        assert n.title and (n.body or ins.kind == "data_trust")
        ids = {e.id for e in ins.evidence}
        for _text, cited in n.actions:
            assert cited and set(cited) <= ids, ins.id


def test_turkish_text_uses_turkish_number_notation(demo_report):
    ins = next(i for i in demo_report.insights if i.id == "difficult_level:quartz_merge:7")
    tr = narrate.template_narration(ins, "tr").body
    en = narrate.template_narration(ins, "en").body
    assert "%60,9" in tr and "60.9%" in en


def test_store_and_reload_round_trip(brain_demo, demo_report, tmp_path):
    path = brain_demo.copy(tmp_path)
    con = duckdb.connect(str(path))
    run_id = evidence.store(con, demo_report.insights, demo_report.as_of)
    run, back = evidence.latest(con)
    con.close()
    assert run["run_id"] == run_id
    assert [i.id for i in back] == [i.id for i in demo_report.insights]
    assert back[0].main.facts.keys() == demo_report.insights[0].main.facts.keys()


def test_pulse_demo_stores_insights(brain_demo):
    con = duckdb.connect(str(brain_demo.warehouse), read_only=True)
    run, stored = evidence.latest(con)
    con.close()
    assert run is not None and stored
    assert any(i.kind == "difficult_level" for i in stored)


def test_stale_data_blocks_explanations(brain_demo):
    """Two weeks without new GA4 data: the gate blocks and the app gets a data card only."""
    import datetime as dt

    con = duckdb.connect(str(brain_demo.warehouse), read_only=True)
    rep = insights.run(
        con, as_of=brain_demo.cfg.as_of + dt.timedelta(days=14), rules=brain_demo.rules
    )
    con.close()
    trust = by_kind(rep, "data_trust")
    # GA4 apps and SDK apps alike: the SDK is the primary source of an SDK app
    assert {i.app_id for i in trust} == {"mossy_match", "pacer_pal", "quartz_merge"} | SDK_APPS
    assert all(i.blocked and i.severity == "critical" for i in trust)
    assert {i.kind for i in rep.insights} == {"data_trust"}
    assert rep.insights[0].blocked
    text = narrate.template_narration(trust[0], "tr").body
    assert "açıklama yapılmıyor" in text
    pebble = next(i for i in trust if i.app_id == "pebble_pop")
    assert "SDK verisi" in narrate.template_narration(pebble, "tr").body
    quartz = next(i for i in trust if i.app_id == "quartz_merge")
    assert "GA4 verisi" in narrate.template_narration(quartz, "tr").body


def test_a_failing_job_does_not_hide_the_others(brain_demo, monkeypatch):
    def boom(ctx):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(insights, "JOBS", [("boom", boom), *insights.JOBS[1:]])
    con = duckdb.connect(str(brain_demo.warehouse), read_only=True)
    rep = insights.run(con, as_of=brain_demo.cfg.as_of, rules=brain_demo.rules)
    con.close()
    assert "boom" in rep.errors and by_kind(rep, "difficult_level")
