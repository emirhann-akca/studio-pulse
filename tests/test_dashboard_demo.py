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


def test_demo_builds_every_page(demo):
    site, _ = demo
    pages = sorted(p.relative_to(site).as_posix() for p in site.rglob("*.html"))
    assert pages == ["apps/mossy_match.html", "apps/pacer_pal.html", "apps/quartz_merge.html", "index.html"]


def test_demo_banner_and_fictional_names(demo):
    site, _ = demo
    for page in site.rglob("*.html"):
        html = page.read_text()
        assert "Synthetic demo data" in html, page
        assert "fictional" in html


def test_pages_are_self_contained(demo):
    site, _ = demo
    for page in site.rglob("*.html"):
        html = page.read_text()
        assert not re.search(r"""(src|href)=["']https?://""", html), page
        assert "<link rel=\"stylesheet\"" not in html
        assert "prefers-color-scheme: dark" in html and 'data-theme="dark"' in html


def test_every_chart_has_a_data_table_and_legend_rules(demo):
    site, _ = demo
    html = (site / "apps" / "mossy_match.html").read_text()
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
    sources = {r[0] for r in con.execute("SELECT DISTINCT source FROM fct_source_freshness").fetchall()}
    assert {"ga4_files", "crashlytics_files", "revenuecat", "admob", "play_console", "app_store", "unity_jsonl"} <= sources
    # The incremental RevenueCat export re-delivers rows; the model keeps one version.
    raw, deduped, multi = con.execute(
        "SELECT (SELECT COUNT(*) FROM raw_revenuecat_transactions), (SELECT COUNT(*) FROM stg_rc_transactions), "
        "(SELECT COUNT(*) FROM stg_rc_transactions WHERE versions_seen > 1)").fetchone()
    assert raw > deduped and multi > 0
    apps = {r[0] for r in con.execute("SELECT app_id FROM fct_app_summary WHERE dau_7d > 0").fetchall()}
    assert apps == {"mossy_match", "pacer_pal", "quartz_merge"}
    tiers = {r[0] for r in con.execute("SELECT DISTINCT device_tier FROM fct_perf_by_tier WHERE app_id = 'quartz_merge'").fetchall()}
    assert {"low", "mid", "high", "all"} <= tiers
    con.close()


def test_charts_handle_empty_and_gaps():
    assert "No data" in charts.line_chart("Empty", [], [])
    html = charts.line_chart("Gaps", ["a", "b", "c"], [charts.Series("s", [1.0, None, 3.0])])
    assert html.count("M") >= 2  # the gap splits the path
    assert "Data table" in charts.bar_chart("Bars", ["x"], [charts.Series("s", [2.0])])
    one = charts.line_chart("One", ["a", "b"], [charts.Series("kept", [1.0, 2.0]), charts.Series("empty", [None, None])])
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
