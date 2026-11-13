"""Dashboard build smoke test and demo end-to-end run."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest

from studio_pulse import cli
from studio_pulse.dashboard import charts


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("demo")
    site, work = tmp / "site", tmp / "work"
    rc = cli.main(["demo", "--scale", "0.15", "--out", str(site), "--workdir", str(work)])
    assert rc == 0
    return site, work


DEMO_APPS = ["demo_lanternleap", "demo_tidyhabit", "mossy_match", "pacer_pal", "pebble_pop",
             "quartz_merge"]  # fmt: skip
# the Studio as static pages: English in studio/, Turkish in studio/tr/
STUDIO_PAGES = sorted(
    f"studio/{lang}{p}.html" for lang in ("", "tr/") for p in [*DEMO_APPS, "index", "market"]
)


def test_demo_builds_every_page(demo):
    site, _ = demo
    pages = sorted(p.relative_to(site).as_posix() for p in site.rglob("*.html"))
    assert pages == [
        "apps/demo_lanternleap.html",  # SDK games: registered themselves through the collector
        "apps/demo_tidyhabit.html",
        "apps/mossy_match.html",
        "apps/pacer_pal.html",
        "apps/pebble_pop.html",  # SDK game declared in pulse.yaml
        "apps/quartz_merge.html",
        "experiments.html",
        "index.html",
        *STUDIO_PAGES,
        "ua.html",
    ]


def test_demo_banner_and_fictional_names(demo):
    site, _ = demo
    for page in site.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        if "tr" in page.relative_to(site).parts[:-1]:  # the Studio's Turkish copy
            assert "Sentetik demo verisi" in html and "uydurma" in html, page
            continue
        assert "Synthetic demo data" in html, page
        assert "fictional" in html


def test_pages_are_self_contained(demo):
    site, _ = demo
    for page in site.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert not re.search(r"""src=["']https?://""", html), (
            page
        )  # nothing is loaded from elsewhere
        assert not re.search(r"""<link[^>]+href=["']https?://""", html), page
        assert '<link rel="stylesheet"' not in html
        assert "prefers-color-scheme: dark" in html and 'data-theme="dark"' in html


def test_every_chart_has_a_data_table_and_legend_rules(demo):
    site, _ = demo
    html = (site / "apps" / "mossy_match.html").read_text(encoding="utf-8")
    figures = re.findall(r'<figure class="chart".*?</figure>', html, flags=re.S)
    assert len(figures) >= 15
    for fig in figures:
        if "No data" in fig or "No purchases" in fig:
            continue
        assert "Data table" in fig
    # multi-series charts carry a legend
    active = next(f for f in figures if "<h3>Active users</h3>" in f)
    assert 'class="legend"' in active and "WAU" in active


def test_demo_pipeline_loaded_all_sources(demo):
    _, work = demo
    con = duckdb.connect(str(work / "warehouse.duckdb"), read_only=True)
    sources = {
        r[0] for r in con.execute("SELECT DISTINCT source FROM fct_source_freshness").fetchall()
    }
    assert {
        "ga4_files",
        "crashlytics_files",
        "revenuecat",
        "admob",
        "play_console",
        "app_store",
        "unity_jsonl",
        "pulse_sdk",
    } <= sources
    # The incremental RevenueCat export re-delivers rows; the model keeps one version.
    raw, deduped, multi = con.execute(
        "SELECT (SELECT COUNT(*) FROM raw_revenuecat_transactions), (SELECT COUNT(*) FROM stg_rc_transactions), "
        "(SELECT COUNT(*) FROM stg_rc_transactions WHERE versions_seen > 1)"
    ).fetchone()
    assert raw > deduped and multi > 0
    apps = {
        r[0] for r in con.execute("SELECT app_id FROM fct_app_summary WHERE dau_7d > 0").fetchall()
    }
    assert apps == {
        "mossy_match", "pacer_pal", "quartz_merge", "pebble_pop", "demo_tidyhabit", "demo_lanternleap",
    }  # fmt: skip
    tiers = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT device_tier FROM fct_perf_by_tier WHERE app_id = 'quartz_merge'"
        ).fetchall()
    }
    assert {"low", "mid", "high", "all"} <= tiers
    con.close()


def test_charts_handle_empty_and_gaps():
    assert "No data" in charts.line_chart("Empty", [], [])
    html = charts.line_chart("Gaps", ["a", "b", "c"], [charts.Series("s", [1.0, None, 3.0])])
    assert html.count("M") >= 2  # the gap splits the path
    assert "Data table" in charts.bar_chart("Bars", ["x"], [charts.Series("s", [2.0])])
    one = charts.line_chart(
        "One", ["a", "b"], [charts.Series("kept", [1.0, 2.0]), charts.Series("empty", [None, None])]
    )
    assert "kept" in one and "empty" not in one


def test_nice_ticks_cover_range():
    t = charts.nice_ticks(0, 1234)
    assert t[0] == 0 and t[-1] >= 1234 and len(t) <= 7
    assert charts.fmt_usd(0) == "$0" and charts.fmt_pct(0.1234) == "12.3%"


def test_build_dashboard_cli_uses_existing_warehouse(demo, tmp_path: Path):
    _, work = demo
    out = tmp_path / "again"
    assert cli.main(["build-dashboard", "-c", str(work / "pulse.yaml"), "--out", str(out)]) == 0
    assert (out / "index.html").exists()


def test_retention_benchmarks_on_game_pages_only(demo):
    site, _ = demo
    game = (site / "apps" / "mossy_match.html").read_text(encoding="utf-8")
    fitness = (site / "apps" / "pacer_pal.html").read_text(encoding="utf-8")
    assert 'class="bench"' in game and "top 10%" in game
    assert 'class="bench"' not in fitness and "describe games" in fitness
    for html in (game, fitness, (site / "index.html").read_text(encoding="utf-8")):
        assert "GameAnalytics" in html and "16,262" in html


def test_benchmark_brackets():
    from studio_pulse import benchmarks as B

    assert (
        B.bracket(1, 0.38) == "top 25%"
        and B.bracket(1, 0.41) == "top 10%"
        and B.bracket(1, 0.22) == "below top 25%"
    )
    assert (
        B.bracket(7, 0.03) == "below median"
        and B.bracket(7, 0.05) == "above median"
        and B.bracket(7, 0.12) == "top 10%"
    )
    assert B.bracket(30, 0.02) == "top 25%" and B.bracket(30, None) is None
    assert not B.applies_to("fitness") and B.applies_to("puzzle")


def test_share_axis_never_exceeds_100_percent():
    html = charts.line_chart(
        "Crash-free",
        ["a", "b"],
        [charts.Series("s", [0.991, 0.9995])],
        charts.fmt_pct2,
        zero=False,
        cap=1.0,
    )
    assert "100.50%" not in html and "100.20%" not in html and ">100.00%<" in html


def test_static_studio_is_the_app_view_without_a_server(demo):
    """docs/demo/studio: the Studio for the demo games; every link is a file next to it."""
    from urllib.parse import urlsplit

    site, work = demo
    studio = site / "studio"
    # opens on the morning brief, as the app does, with every demo app on it
    index = (studio / "index.html").read_text(encoding="utf-8")
    assert "Needs attention" in index and "Apps · last 7 days" in index
    assert all(f'href="{a}.html"' in index for a in DEMO_APPS)
    for page in studio.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        for bad in ("hx-get", "hx-post", "htmx.min.js", "X-Pulse-Token", "127.0.0.1", "<form"):
            assert bad not in html, (page, bad)
        for href in re.findall(r'href="([^"]+)"', html):
            if urlsplit(href).scheme:
                continue
            assert not href.startswith("/"), (page, href)
            assert (page.parent / urlsplit(href).path).exists(), (page, href)
    en = (studio / "mossy_match.html").read_text(encoding="utf-8")
    assert 'href="tr/mossy_match.html" hreflang="tr">Türkçe<' in en
    assert 'href="../apps/mossy_match.html"' in en  # the full dashboard page
    assert "The question box opens in the running app" in en
    tr = (studio / "tr" / "mossy_match.html").read_text(encoding="utf-8")
    assert (
        'href="../mossy_match.html" hreflang="en">English<' in tr
        and 'href="../../apps/mossy_match.html"' in tr
    )
    assert "Mağaza · son 30 gün" in tr and "Günlük aktif (ort.)" in tr  # Turkish throughout
    market = (studio / "market.html").read_text(encoding="utf-8")
    assert "Rising today" in market and "Teacup Tumble" in market and "mechanic_shown=" in market
    assert 'href="studio/index.html">Studio<' in (site / "index.html").read_text(encoding="utf-8")


def test_demo_live_snapshot_is_taken_in_the_evening_of_the_last_day(demo):
    import datetime as dt
    import json

    from studio_pulse.demo.generate import DEMO_END

    _, work = demo
    snap = json.loads((work / "live_snapshot.json").read_text(encoding="utf-8"))
    at = dt.datetime.fromtimestamp(snap["now_ms"] / 1000, dt.UTC)
    assert at == dt.datetime.combine(DEMO_END, dt.time(20), dt.UTC)
    assert {a["app_id"] for a in snap["apps"]} == {"demo.pebblepop", "demo.tidyhabit",
                                                    "demo.lanternleap"}  # fmt: skip


def test_demo_market_narration_only_states_its_own_facts():
    """The synthetic market day: invented games, no store links, a summary whose numbers
    all come from the facts next to it."""
    import datetime as dt

    from studio_pulse.demo.market import NARRATION, facts
    from studio_pulse.radar.taxonomy import load

    f = facts(dt.date(2026, 11, 1))
    items = {i["name"]: i for i in f["items"]}
    assert all(i["store_url"] is None and i["play_url"] is None for i in items.values())
    by_mech: dict[str, int] = {}
    for r in f["trends"]["rows"]:
        by_mech[r["mechanic"]] = by_mech.get(r["mechanic"], 0) + r["n_apps"]
    tea, ink = items["Teacup Tumble"], items["Inkwell Words"]
    assert (tea["ranks"][0]["rank"], tea["age"]["value"]) == (6, 21)
    assert (ink["ranks"][0]["rank"], ink["age"]["value"]) == (4, 15)
    assert by_mech["merge"] == 31 == max(by_mech.values()) and by_mech["match-3"] == 24
    numbers = {"6", "21", "4", "15", "31", "24", "3"}  # "match-3" in English
    for text in NARRATION.values():
        assert set(re.findall(r"\d+", text)) <= numbers, text
    tax = load()
    for idea in f["ideas"][1:]:
        assert idea["forge_mechanic"] == tax.forge(idea["mechanic"])
