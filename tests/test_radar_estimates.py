"""Market estimates: Play counter windows, rank->downloads curves with bootstrap bands and
backtests, public-anchor revenue bands, the watchlist, the daily collection hooks, the game
page, checks and labels. Synthetic data (tests/radar_synth.py) and the recorded 12 Nov
fixture only: no test reaches the network."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import random
from pathlib import Path

import pytest
import radar_synth
import yaml

from studio_pulse import checks as checks_mod
from studio_pulse import cli, warehouse
from studio_pulse.config import ConfigError, config_from_dict
from studio_pulse.radar import digest, store
from studio_pulse.radar import run as radar_run
from studio_pulse.radar.estimate import collect, downloads, plan, revenue, rpd, stage, stats
from studio_pulse.radar.estimate.settings import EstimateSettings, from_raw
from studio_pulse.radar.http import Fetcher
from studio_pulse.radar.replay import RadarReplaySession
from studio_pulse.radar.settings import DEFAULT_COUNTRIES, from_config

FIX = Path(__file__).parent / "fixtures" / "radar" / "2026-11-12"


@pytest.fixture()
def econ():
    c = warehouse.connect(":memory:")
    store.init_schema(c)
    stage.init_schema(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A 35-day synthetic warehouse with estimates computed for its last day."""
    path = tmp_path_factory.mktemp("est") / "w.duckdb"
    con = warehouse.connect(path)
    store.init_schema(con)
    stage.init_schema(con)
    w = radar_synth.build(con)
    radar_run.run_models(con)
    summary = stage.compute(
        con, ["us", "tr", "ca"], EstimateSettings(), w.days[-1], log=lambda m: None
    )
    con.close()
    # a copy for the page test, made while no connection holds the file (Windows locks it)
    snap = path.with_name("page.duckdb")
    snap.write_bytes(path.read_bytes())
    con = warehouse.connect(path)
    yield snap, con, w, summary
    con.close()


# -- statistics ----------------------------------------------------------------------------------


def test_ols_quantile_and_log_uniform():
    a, s = stats.ols([0, 1, 2, 3], [1, 3, 5, 7])
    assert a == pytest.approx(1) and s == pytest.approx(2)
    with pytest.raises(ValueError):
        stats.ols([1, 1, 1], [1, 2, 3])
    assert stats.quantile([1, 2, 3, 4], 0.5) == 2.5  # numpy's default (type 7)
    rng = random.Random(1)
    xs = [stats.log_uniform(rng, 0.5, 2.0) for _ in range(2000)]
    assert min(xs) >= 0.5 and max(xs) <= 2.0
    assert stats.quantile(xs, 0.5) == pytest.approx(1.0, rel=0.08)  # geometric middle


def test_power_law_fit_recovers_the_slope_and_its_band_covers():
    rng = random.Random(3)
    pts, test = [], []
    for g in range(60):
        for _ in range(3):
            r = rng.uniform(1, 100)
            y = 50000 * r**-0.9 * math.exp(rng.gauss(0, 0.4))
            (test if g >= 45 else pts).append(stats.Point(f"g{g}", r, y))
    fit = stats.fit_power_law(pts, boot=300, rng=rng)
    assert fit.b == pytest.approx(0.9, abs=0.12)
    lo, hi = fit.b_band
    assert lo < fit.b < hi
    inside = 0
    for p in test:
        d = fit.draws(p.rank, rng)
        inside += math.exp(stats.quantile(d, 0.1)) <= p.y <= math.exp(stats.quantile(d, 0.9))
    assert 0.65 <= inside / len(test) <= 0.95  # nominal 80%: a prediction band, not a CI


# -- ground truth: the Play counter -----------------------------------------------------------------


def _obs(day: int, installs: int, ratings: int, hour: int = 9) -> downloads.Obs:
    d = dt.date(2026, 11, 1) + dt.timedelta(days=day)
    return downloads.Obs(d, dt.datetime.combine(d, dt.time(hour)), installs, ratings)


def test_refresh_windows_ignore_a_counter_that_did_not_move():
    es = EstimateSettings(min_window_days=7)
    series = {
        # frozen counter, ratings growing (like 18 of 30 live packages on 13-14 Nov 2026)
        "frozen": [_obs(i, 4_283_864, 31_255 + 300 * i) for i in range(20)],
        # refreshed every 3 days by 3,000 a day, ratings at 1.5% of installs
        "steady": [_obs(i, 100_000 + 9_000 * (i // 3), 1500 + 45 * i) for i in range(22)],
        # a refresh of +62 installs while 2,000 ratings came in: a lagging counter
        "lagging": [_obs(0, 2_368_345, 70_200), _obs(1, 2_368_400, 70_500)]
        + [_obs(i, 2_368_400 + 10 * (i // 8), 70_500 + 2_000 * i) for i in range(2, 30)],
    }
    w = downloads.refresh_windows(series, es)
    assert not [x for x in w if x.play_id == "frozen"]
    steady = [x for x in w if x.play_id == "steady"]
    assert steady and all(x.status == "ok" and x.days >= 7 for x in steady)
    assert all(x.rate == pytest.approx(3000, rel=0.01) for x in steady)
    assert {x.status for x in w if x.play_id == "lagging"} == {"implausible"}
    st = downloads.counter_staleness(series)
    assert st["pairs"] == 19 + 21 + 29 and st["stale"] >= 19


def test_one_day_differences_are_never_windows():
    es = EstimateSettings(min_window_days=7)
    series = {"p": [_obs(0, 1_013_697, 11_740, 21), _obs(1, 1_042_256, 11_878, 2)]}
    assert downloads.refresh_windows(series, es) == []
    with pytest.raises(ConfigError):
        from_raw({"min_window_days": 0.5})
    with pytest.raises(ConfigError):
        from_raw({"band": [0.9, 0.1]})


# -- public sources ------------------------------------------------------------------------------


def test_ios_android_yaml_covers_the_default_countries_with_sources():
    split = downloads.load_platform_split()
    for cc in DEFAULT_COUNTRIES:
        lo, hi = downloads.ios_share_range(split, cc)
        assert 0 < lo < hi < 1, cc
    assert downloads.ios_share_range(split, "jp") == (0.50, 0.58)  # published store split
    us = downloads.ios_share_range(split, "us")
    assert us[0] == pytest.approx(0.5196 * 0.58) and us[1] == pytest.approx(0.5922 * 1.17)
    assert downloads.ios_share_range(split, "zz") is None
    rlo, rhi = downloads.ratio_range(split, ["tr", "us"])
    assert rlo < 0.2 < 2 < rhi  # the band spans the most Android- and iOS-heavy country
    for key, src in split["sources"].items():
        assert src["url"].startswith("https://"), key
    for c in split["countries"].values():
        for s in c.get("download_sources") or []:
            assert s in split["sources"]


def test_revenue_anchors_yaml_every_figure_has_a_source_with_url_and_date():
    doc = revenue.load_anchors()
    srcs = doc["sources"]
    for a in doc["anchors"]:
        assert a["sources"] and all(s in srcs for s in a["sources"]), a["id"]
        assert a["period"]["start"] < a["period"]["end"]
        for f in a["factors"]:
            lo, hi = f["range"]
            assert 0 < lo <= hi <= 1 and f["basis"], (a["id"], f["name"])
    for cc, m in doc["market_size"].items():
        assert m["source"] in srcs and m["basis"], cc
    for k in ("garg_telang_2013", "chevalier_goolsbee_2003", "ecb_exr"):
        assert srcs[k]["url"].startswith("https://")
    for key, s in srcs.items():
        assert s["url"].startswith("https://"), key
        assert s.get("published") or s.get("retrieved") or s.get("note"), key
    assert doc["slope"]["low"] < 0.86 < doc["slope"]["high"]  # Garg & Telang's grossing fit


def test_revenue_bands_fall_with_rank_and_unknown_markets_get_none():
    doc = revenue.load_anchors()
    ranks = {a["id"]: revenue.anchor_ranks(None, a) for a in doc["anchors"]}
    es = EstimateSettings(draws=600)
    m = revenue.build(doc, doc["anchors"], ranks, ["us", "tr", "ca", "br"], es, random.Random(5))
    assert m is not None and set(m.anchors) == {
        "roblox_q3_2026",
        "disney_solitaire_q3_2026",
        "bingo_blitz_q3_2026",
    }
    assert m.country_draws("ca", 1, random.Random(1)) is None  # no public market size
    rng = random.Random(2)
    mids = [stats.quantile(m.country_draws("us", r, rng), 0.5) for r in (1, 10, 100)]
    assert mids[0] > mids[1] > mids[2]
    us1 = [math.exp(x) for x in m.country_draws("us", 1, rng)]
    tr1 = [math.exp(x) for x in m.country_draws("tr", 1, rng)]
    assert stats.quantile(tr1, 0.5) < stats.quantile(us1, 0.5) / 10
    lo, hi = stats.quantile(us1, 0.1), stats.quantile(us1, 0.9)
    assert hi / lo > 3  # wide on purpose: three anchors and a slope range
    # one anchor is not enough to calibrate
    one = [a for a in doc["anchors"] if a["id"] == "roblox_q3_2026"]
    assert revenue.build(doc, one, ranks, ["us"], es, random.Random(1)) is None


def test_revenue_per_download_yaml_and_genres():
    doc = rpd.load()
    for key, src in doc["sources"].items():
        assert src["url"].startswith(("https://", "http://")), key
    assert doc["genre_rpd_net"]["source"] in doc["sources"]
    assert doc["mechanic_rpd_net"]["source"] in doc["sources"]
    assert doc["ad_arpu_d90"]["source"] in doc["sources"]
    # the platform split reproduces the market: 6.73 / 1.62 and 0.71 / 1.62
    assert doc["platform_factor"]["ios"] == pytest.approx(6.73 / 1.62, rel=0.01)
    assert doc["platform_factor"]["android"] == pytest.approx(0.71 / 1.62, rel=0.02)
    assert rpd.genre_of(doc, ["Puzzle", "Casual"], "match-3").genre == "match-3"
    assert rpd.genre_of(doc, ["Casual", "Puzzle"], None).genre == "puzzle"
    assert rpd.genre_of(doc, ["Role Playing"], "word").genre == "rpg"
    assert rpd.genre_of(doc, ["Education"], None).basis == "unknown"


def test_revenue_from_downloads_weighs_ios_and_ads():
    doc = rpd.load()
    rng = random.Random(1)

    def mid(prof, android, ios):
        f = rpd.revenue_sampler(doc, prof)
        return stats.quantile([f(rng, android, ios) for _ in range(3000)], 0.5)

    puzzle_no_ads = rpd.GameProfile("puzzle", "apple:Puzzle", False)
    a = mid(puzzle_no_ads, 10_000, 0)
    i = mid(puzzle_no_ads, 0, 10_000)
    assert i / a == pytest.approx(4.15 / 0.44, rel=0.15)  # an iOS download earns ~9x more
    # puzzle: 0.79 net x ~1.3 gross x 0.44 Android ~ $0.45 per Android download
    assert 2_000 < a < 9_000
    with_ads = mid(rpd.GameProfile("puzzle", "apple:Puzzle", True), 10_000, 0)
    assert with_ads > a + 3_000  # D90 casual ad ARPU $0.55 to twice that per install
    rpg = mid(rpd.GameProfile("rpg", "apple:Role Playing", False), 10_000, 0)
    assert rpg > 5 * a


def test_revenue_validation_leaves_anchors_out():
    doc = revenue.load_anchors()
    ranks = {a["id"]: revenue.anchor_ranks(None, a) for a in doc["anchors"]}
    ranks["lineage_m_2q26"] = [12]
    rows = revenue.validate(
        doc, doc["anchors"], ranks, ["us", "kr"], EstimateSettings(draws=800), random.Random(4)
    )
    schemes = {r["scheme"]: r for r in rows}
    assert schemes["loo_anchor"]["n"] == 3 and schemes["loo_anchor"]["coverage"] is not None
    assert schemes["holdout_anchor"]["country"] == "kr"
    assert "top-1000 sum" in schemes["market_sum"]["note"]


# -- the whole computation on a synthetic warehouse ----------------------------------------------


def test_synthetic_world_calibrates_backtests_and_bands(world):
    _, con, w, summary = world
    fits = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT list_key, status, n_games, b FROM raw_mr_est_fit WHERE kind = 'downloads'"
        ).fetchall()
    }
    status, n, b = fits["us:topfreeapplications:6014"]
    assert status == "ok" and n >= 50
    assert 0.55 < b < 0.95  # true 0.8; the TR share of installs flattens it a little
    tests = con.execute(
        "SELECT scheme, n, coverage, mape FROM raw_mr_est_backtest WHERE kind = 'downloads' "
        "AND list_key = 'us:topfreeapplications:6014'"
    ).fetchall()
    assert {t[0] for t in tests} == {"logo", "time"}
    for _scheme, n_, cov, mape in tests:
        assert n_ >= 20 and 0.6 <= cov <= 0.97 and mape < 0.8
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM raw_mr_est_game GROUP BY 1").fetchall())
    assert kinds["android_installs_ww"] > 50 and kinds["ios_downloads_ww"] > 50
    assert kinds["revenue_ww"] == kinds["ios_downloads_ww"]  # revenue from downloads, per band
    assert kinds["ios_revenue"] == 100  # the US grossing list; CA has no public market size
    rev = dict(
        con.execute("SELECT country, status FROM raw_mr_est_fit WHERE kind = 'revenue'").fetchall()
    )
    assert rev == {"us": "ok", "tr": "ok", "ca": "no_source"}
    assert summary["windows"].get("ok", 0) > 100
    assert summary["windows"].get("implausible", 0) > 0  # lagging counters are left out
    assert summary["counter_pairs"]["stale"] > 0  # the frozen counters are counted


def test_estimates_do_not_depend_on_the_chart_source(econ):
    """Ranks from another feed (raw_mr_rank_extra) calibrate and band like the Apple RSS."""
    w = radar_synth.build(econ, days=30, n_games=80)
    econ.execute(
        """
        INSERT INTO raw_mr_rank_extra
        SELECT snapshot_date, 'other_feed', 'apple', country,
          country || CASE chart WHEN 'topfreeapplications' THEN ':free:all' ELSE ':grossing:all' END,
          CASE chart WHEN 'topfreeapplications' THEN 'free' ELSE 'grossing' END,
          TRUE, 100, rank, app_id, bundle_id, name
        FROM raw_mr_chart
        """
    )
    econ.execute("DELETE FROM raw_mr_chart")  # no Apple RSS at all
    radar_run.run_models(econ)
    summary = stage.compute(
        econ, ["us", "tr"], EstimateSettings(draws=400), w.days[-1], log=lambda m: None
    )
    assert summary["download_lists"]["us:free:all"]["status"] == "ok"
    kinds = dict(
        econ.execute(
            "SELECT kind, COUNT(*) FROM raw_mr_est_game WHERE list_key LIKE '%:all' GROUP BY 1"
        ).fetchall()
    )
    assert kinds["android_installs_ww"] > 20 and kinds["ios_revenue"] == 80  # 80 games listed
    assert econ.execute(
        "SELECT status FROM raw_mr_est_fit WHERE kind = 'revenue' AND list_key = 'us:grossing:all'"
    ).fetchone() == ("ok",)


def test_estimate_checks_pass_on_the_synthetic_world(world):
    _, con, w, _ = world
    stage.run_models(con)
    results = checks_mod.run_checks(
        con, checks_mod.load_checks(stage.checks_path()), None, w.days[-1]
    )
    errors = [r for r in results if r.severity == "error" and r.status != "pass"]
    assert not errors, [(r.name, r.message) for r in errors]
    names = {r.name for r in results}
    assert {
        "radar_est_band_order",
        "radar_est_only_calibrated_lists",
        "radar_watch_unique",
    } <= names


def test_insufficient_data_shows_no_number_and_says_so(econ):
    w = radar_synth.build(econ, days=12, n_games=40)
    radar_run.run_models(econ)
    es = EstimateSettings(min_games=500, draws=300)
    summary = stage.compute(econ, ["us", "tr"], es, w.days[-1], log=lambda m: None)
    assert all(v["status"] == "insufficient" for v in summary["download_lists"].values())
    assert not econ.execute(
        "SELECT COUNT(*) FROM raw_mr_est_game WHERE kind <> 'ios_revenue'"
    ).fetchone()[0]
    facts = {"as_of": w.days[-1].isoformat(), "countries": ["us"], "items": [], "meta": {}}
    stage.add_facts(econ, facts, w.days[-1], summary)
    tr = stage.render_md(facts, "tr")
    assert "yetersiz veri" in tr and "indirme tahmini gösterilmiyor" in tr
    assert "insufficient data" in stage.render_md(facts, "en")


def test_facts_keep_every_number_labelled(world):
    _, con, w, summary = world
    day = w.days[-1]
    app = con.execute(
        "SELECT app_id FROM raw_mr_est_game WHERE kind = 'android_installs_ww' LIMIT 1"
    ).fetchone()[0]
    facts = {
        "as_of": day.isoformat(),
        "countries": ["us", "tr"],
        "items": [{"app_id": app, "name": "x", "class": "breakout"}],
        "meta": {},
    }
    stage.add_facts(con, facts, day, summary)
    assert digest.check_labels(facts) == []
    est = facts["items"][0]["market_estimates"]
    assert est and all(e["basis"] == "estimate" and e["low"] <= e["mid"] <= e["high"] for e in est)
    assert len(facts["estimates"]["grossing_top"]) == 3  # top 3 of the US list
    md = stage.render_md(facts, "tr")
    assert "## Tahminler (TAHMİN)" in md and "TAHMİN" in md and "MAPE" in md
    assert "(ESTIMATE;" in stage.render_md(facts, "en")
    facts["estimates"]["grossing_top"][0]["band"].pop("n")
    assert digest.check_labels(facts)  # an estimate without n is refused


def test_version_timeline_and_play_counter_models(world):
    _, con, _, _ = world
    v = con.execute(
        "SELECT version, first_seen, version_date FROM fct_mr_version_timeline "
        "WHERE app_id = 'com.synth.game1' ORDER BY first_seen"
    ).fetchall()
    assert [x[0] for x in v] == ["1.0", "1.1", "1.2", "1.3"]
    assert all(x[2] <= x[1] for x in v)
    st = con.execute(
        "SELECT COUNT(*) FILTER (WHERE stale), COUNT(*) FILTER (WHERE refreshed) "
        "FROM stg_mr_play_counter"
    ).fetchone()
    assert st[0] > 0 and st[1] > 0


# -- watchlist and collection ---------------------------------------------------------------------


def test_parse_target_accepts_ids_packages_and_store_urls():
    assert collect.parse_target("6811006356") == ("apple", "6811006356")
    assert collect.parse_target("id6811006356") == ("apple", "6811006356")
    assert collect.parse_target("https://apps.apple.com/tr/app/foodie-pop/id6811006356?l=tr") == (
        "apple",
        "6811006356",
    )
    assert collect.parse_target("com.abi.foodpop") == ("play", "com.abi.foodpop")
    assert collect.parse_target(
        "https://play.google.com/store/apps/details?id=com.abi.foodpop&hl=tr"
    ) == ("play", "com.abi.foodpop")
    for bad in ("foodpop", "https://example.com/id12", "com..x", "12 34"):
        with pytest.raises(collect.WatchError):
            collect.parse_target(bad)
    assert collect.plausible_android_id("com.abi.foodpop")
    for ios_only in ("com.playrix.township-ios", "com.x.game.ios", "com.x.9lives", None):
        assert not collect.plausible_android_id(ios_only)


def test_watch_cli_add_list_remove(tmp_path, capsys):
    cfgp = tmp_path / "pulse.yaml"
    cfgp.write_text(
        yaml.safe_dump({"apps": [{"id": "x"}], "warehouse": "w.duckdb"}), encoding="utf-8"
    )
    run = lambda *a: cli.main(["radar", "watch", *a, "-c", str(cfgp)])  # noqa: E731
    assert run("add", "6811006356", "--note", "rakip") == 0
    assert run("add", "https://apps.apple.com/us/app/x/id6811006356") == 0  # same game
    assert run("add", "com.abi.foodpop") == 0
    assert run("add", "not a game") == 2
    capsys.readouterr()
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "6811006356" in out and "com.abi.foodpop" in out and "rakip" in out
    assert run("remove", "com.abi.foodpop") == 0
    con = warehouse.connect(tmp_path / "w.duckdb", read_only=True)
    try:
        assert collect.watched(con) == [("apple", "6811006356")]
        # the removed entry stays as history
        assert con.execute("SELECT COUNT(*) FROM raw_mr_watch").fetchone()[0] == 2
    finally:
        con.close()


def _replay_fetcher(con, day, budgets=None):
    sess = RadarReplaySession(FIX)
    f = Fetcher(
        con,
        day,
        session=sess,
        run_id="t",
        clock=lambda: 0.0,
        sleep=lambda s: None,
        budgets=budgets or {"play.google.com": 200},
    )
    return f, sess


def test_watched_games_are_read_daily_through_the_polite_fetcher(econ, tmp_path):
    day = dt.date(2026, 11, 12)
    collect.watch_add(econ, "6811006356")  # Foodie Pop!: in the recorded Lookup pools
    collect.watch_add(econ, "com.abi.foodpop")
    cfg = config_from_dict(
        {"apps": [{"id": "x"}], "radar": {"countries": ["us", "tr"], "play": {}}},
        base_dir=tmp_path,
    )
    s = from_config(cfg)
    f, sess = _replay_fetcher(econ, day)

    class Res:
        notes: list[str] = []
        errors: list[str] = []
        counts: dict = {}

    res = Res()
    assert collect.lookup_watched(econ, f, s, day, res, lambda m: None) == 2  # us + tr
    rows = econ.execute(
        "SELECT country, source FROM raw_mr_app WHERE app_id = '6811006356' ORDER BY 1"
    ).fetchall()
    assert rows == [("tr", "watch"), ("us", "watch")]
    radar_run.run_models(econ)
    stats_ = collect.collect_play(
        econ, f, s, EstimateSettings(panel_size=0), day, res, lambda m: None
    )
    assert stats_["stored"] == 1 and not res.errors
    paths = [r.path for r in sess.recorded]
    assert paths.index("/robots.txt") < paths.index("/store/apps/details")
    ledger = econ.execute(
        "SELECT purpose FROM raw_mr_request WHERE NOT cached ORDER BY ts"
    ).fetchall()
    assert ("lookup_watch:us",) in ledger and ("play_details",) in ledger


def test_panel_is_stratified_sticky_and_leaves_the_play_reserve(econ):
    w = radar_synth.build(econ, days=3, n_games=60)
    radar_run.run_models(econ)
    day = w.days[-1]
    cfg = config_from_dict(
        {"apps": [{"id": "x"}], "radar": {"countries": ["us", "tr"]}}, base_dir=Path(".")
    )
    s = from_config(cfg)
    # every package was already read today (the radar's own candidates): no panel slot used
    assert collect.panel_jobs(econ, s, EstimateSettings(panel_size=12), day) == []
    econ.execute(
        "DELETE FROM raw_mr_app WHERE store = 'play' AND snapshot_date = ? "
        "AND app_id <> 'com.synth.game0'",
        [day],
    )
    jobs = collect.panel_jobs(econ, s, EstimateSettings(panel_size=12), day)
    assert len(jobs) == 12 and all(j[2].startswith("panel:") for j in jobs)
    assert "com.synth.game0" not in {j[1] for j in jobs}
    best = dict(
        econ.execute(
            "SELECT app_id, MIN(rank) FROM stg_mr_chart WHERE snapshot_date = ? "
            "AND chart = 'topfreeapplications' GROUP BY 1",
            [day],
        ).fetchall()
    )
    bands = [collect._band(best[a]) for a, _, _ in jobs[:4]]
    assert sorted(bands) == [0, 1, 2, 3]  # one per rank band before a second from any band
    # the budget: with 195 of 200 Play requests used and a reserve of 10, nothing is sent
    econ.execute(
        "INSERT INTO raw_mr_request (run_id, snapshot_date, host, path, cached) "
        "SELECT 'earlier', ?, 'play.google.com', '/store/apps/details?id=x', FALSE "
        "FROM range(195)",
        [day],
    )
    econ.execute("DELETE FROM raw_mr_app WHERE store = 'play' AND snapshot_date = ?", [day])
    f, sess = _replay_fetcher(econ, day)

    class Res:
        notes: list[str] = []
        errors: list[str] = []
        counts: dict = {}

    out = collect.collect_play(
        econ, f, s, EstimateSettings(panel_size=12, play_reserve=10), day, Res(), lambda m: None
    )
    assert out["requested"] == 0 and out["skipped_budget"] == 12 and sess.recorded == []


# -- settings, request plan -----------------------------------------------------------------------


def test_default_countries_and_the_budget_check():
    cfg = config_from_dict({"apps": [{"id": "x"}], "radar": {}}, base_dir=Path("."))
    s = from_config(cfg)
    assert s.countries == ["us", "gb", "de", "fr", "br", "tr", "jp", "kr"]  # unchanged default
    p = plan.request_plan(s, EstimateSettings())
    assert p["within_budget"] and p["chart_source"] == "web"
    assert p["apps_apple_com_pages_per_day"] == 32  # 8 storefronts x 4 genres, free+paid each
    assert p["assumptions"]["lists_not_on_the_web"] == ["topgrossingapplications:6014"]
    assert 20 <= p["itunes_requests_per_day"] <= s.apple_budget == 120  # Lookup only
    assert p["play_requests_per_day"] <= 200 and p["per_country"][0]["chart_requests"] == 4
    ten = from_config(
        config_from_dict(
            {
                "apps": [{"id": "x"}],
                "radar": {"countries": [*DEFAULT_COUNTRIES, "ca", "au"]},
            },
            base_dir=Path("."),
        )
    )
    assert len(ten.countries) == 10 and plan.request_plan(ten, EstimateSettings())["per_country"]
    with pytest.raises(ConfigError, match="daily_budget"):
        from_config(
            config_from_dict(
                {"apps": [{"id": "x"}], "radar": {"apple": {"daily_budget": 5}}},
                base_dir=Path("."),
            )
        )
    with pytest.raises(ConfigError, match="web_daily_budget"):
        from_config(
            config_from_dict(
                {"apps": [{"id": "x"}], "radar": {"apple": {"web_daily_budget": 10}}},
                base_dir=Path("."),
            )
        )
    assert s.apple_chart_source == "web"
    with pytest.raises(ConfigError, match="storefront"):
        from_config(
            config_from_dict(
                {"apps": [{"id": "x"}], "radar": {"countries": ["usa"]}}, base_dir=Path(".")
            )
        )
    s2 = from_config(
        config_from_dict(
            {"apps": [{"id": "x"}], "radar": {"play": {"daily_budget": 900}}}, base_dir=Path(".")
        )
    )
    assert s2.play_budget == 200  # Play never more than 200 a day, whatever the config says


def test_plan_cli_prints_the_volume(tmp_path, capsys):
    cfgp = tmp_path / "pulse.yaml"
    cfgp.write_text(yaml.safe_dump({"apps": [{"id": "x"}], "radar": {}}), encoding="utf-8")
    assert cli.main(["radar", "plan", "-c", str(cfgp)]) == 0
    out = capsys.readouterr().out
    assert '"apps_apple_com_pages_per_day"' in out and '"within_budget": true' in out


# -- the game page ----------------------------------------------------------------------------------


def test_game_page_in_pulse_serve(world, tmp_path):
    from fastapi.testclient import TestClient

    from studio_pulse.serve.app import create_app

    snap, con, w, _ = world
    app_id = con.execute(
        "SELECT app_id FROM raw_mr_est_game WHERE kind = 'android_installs_ww' "
        "AND snapshot_date = ? ORDER BY rank LIMIT 1",
        [w.days[-1]],
    ).fetchone()[0]
    cfg = config_from_dict({"apps": [{"id": "x"}], "warehouse": str(snap)}, base_dir=tmp_path)
    client = TestClient(create_app(cfg, token="t"), base_url="http://127.0.0.1:8765")
    r = client.get(f"/radar/oyun/{app_id}")
    assert r.status_code == 200
    page = r.text
    assert "Tahminler" in page and "(TAHMİN)" in page and "(GÖZLEM)" in page
    assert "<svg" in page and "<title>Oyunlar · " in page
    assert "Android kurulum/gün" in page and "Sürüm / güncelleme" in page
    assert "Doğruluk (örneklem dışı)" in page and "logo" in page
    pkg = f"com.synth.game{int(app_id) - 990000000}"
    assert "realInstalls" in page and pkg in page
    by_pkg = client.get(f"/radar/oyun/{pkg}?lang=en")
    assert by_pkg.status_code == 200 and "Estimates" in by_pkg.text and "(ESTIMATE)" in by_pkg.text
    assert client.get("/radar/oyun/123").status_code == 404


def test_estimate_settings_disable_and_panel_off():
    es = from_raw({"enabled": False, "panel_size": 0, "band": [0.05, 0.95]})
    assert not es.enabled and es.panel_size == 0 and es.band_pct == 90
    assert dataclasses.replace(es, panel_size=3).panel_size == 3
