"""The shared shell: design-system files pinned, one language rule, one sidebar on every page,
honest small numbers and KPI drill-downs that print their SQL."""

from __future__ import annotations

import hashlib
import re
from importlib import resources

import pytest
from fastapi.testclient import TestClient
from test_live_page import HOST, SNAP  # tests/ is on sys.path (no package)

from studio_pulse.serve import studio_metrics as sm
from studio_pulse.serve import ui
from studio_pulse.serve.app import create_app

# studio-ui is byte-identical in Creative Forge; a change bumps the version line and these
# hashes in both repos.
UI_VERSION = "studio-ui 1.1.0"
UI_CSS_SHA256 = "4c8bba0bc59ad9396317b839a0c07ffe28d83b223c4b3bb4b045f6a0119e612c"
UI_JS_SHA256 = "45324136adb91282a87b43484761936ae1c16fbc5f3e84d4fe9464d914a5ec93"


def test_shared_design_system_files_are_pinned():
    static = resources.files("studio_pulse.serve").joinpath("static")
    css = static.joinpath("ui.css").read_bytes()
    js = static.joinpath("ui.js").read_bytes()
    assert css.decode("utf-8").startswith(f"/* {UI_VERSION} ")
    assert js.decode("utf-8").startswith(f"/* {UI_VERSION} ")
    assert hashlib.sha256(css).hexdigest() == UI_CSS_SHA256
    assert hashlib.sha256(js).hexdigest() == UI_JS_SHA256
    # both are inlined into <style> / <script>: a closing tag inside would end them early
    assert b"</style" not in css.lower() and b"</script" not in js.lower()


def test_both_languages_have_the_same_keys():
    assert set(ui.S["tr"]) == set(ui.S["en"])
    from studio_pulse.serve.studio import T

    assert set(T["tr"]) == set(T["en"])


@pytest.fixture()
def client(studio_cfg):
    return TestClient(create_app(studio_cfg, token="t", live_fetcher=lambda: SNAP), base_url=HOST)


@pytest.fixture(scope="module")
def studio_cfg(demo_build, tmp_path_factory):
    from studio_pulse import cli

    cfg = demo_build.copy(tmp_path_factory.mktemp("shell_ui"))
    assert cli.main(["run", "-c", str(cfg.path)]) == 0
    return cfg


def test_turkish_by_default_and_the_choice_is_remembered(client):
    tr = client.get("/health").text
    assert '<html lang="tr">' in tr and "Veri kalitesi" in tr and "Şimdi yenile" in tr
    en = client.get("/health", params={"lang": "en"})
    assert "Data quality" in en.text and en.cookies.get("ui-lang") == "en"
    # no ?lang= now: the cookie keeps English on every other page
    assert '<html lang="en">' in client.get("/alerts").text
    assert '<html lang="en">' in client.get("/studio/pebble_pop").text


@pytest.mark.parametrize("path", ["/health", "/alerts", "/live", "/insights", "/ask", "/radar",
                                  "/studio/pebble_pop", "/studio/market"])  # fmt: skip
def test_every_page_has_the_same_sidebar_and_no_english_leftovers(client, path):
    html = client.get(path).text
    side = html[html.index('id="sidebar"') : html.index("</nav>", html.index('id="sidebar"'))]
    assert "Uygulamalar" in side and "Araçlar" in side and "Pebble Pop" in side
    assert 'id="ui-commands"' in html and "⌘K" in html  # the command palette
    for word in ("Dark mode", "Light mode", "Run refresh now", "Collector not reachable",
                 "Data quality", "No alerts fired"):  # fmt: skip
        assert word not in html, (path, word)


def test_the_palette_lists_apps_and_pages(client):
    import json

    html = client.get("/studio/pebble_pop").text
    cmds = json.loads(re.search(r'id="ui-commands"[^>]*>(.*?)</script>', html, re.S).group(1))
    labels = {c["label"] for c in cmds}
    assert {"Pebble Pop", "Pazar radarı", "Veri sağlığı", "Uyarılar", "Canlı"} <= labels
    assert all(c["href"].startswith("/") for c in cmds)


def test_period_switch_and_comparison(client):
    page = client.get("/studio/mossy_match", params={"days": 30}).text
    assert re.search(r'href="/studio/mossy_match\?days=30" aria-current="true">30 gün<', page)
    assert "karşılaştırma:" in page and "öncekine göre" in page
    assert 'href="/studio/mossy_match/metric/d1?days=30"' in page  # every tile drills down
    assert client.get("/studio/mossy_match", params={"days": 5}).status_code == 200  # falls back


def test_metric_drill_down_prints_its_query(client):
    r = client.get("/studio/mossy_match/metric/d1", params={"days": 30})
    assert r.status_code == 200
    assert "Bu sayının sorgusu" in r.text and "FROM fct_retention" in r.text
    assert "Günlük değerler" in r.text and "Önceki dönem" in r.text
    assert client.get("/studio/mossy_match/metric/nope").status_code == 404
    assert client.get("/studio/nope/metric/dau").status_code == 404


def test_small_samples_say_so_instead_of_a_percentage():
    rows = [
        {"date": "2026-11-01", "value": 0.5, "n": 2},
        {"date": "2026-11-02", "value": 0.0, "n": 1},
    ]
    v, n = sm.aggregate(rows, "weighted")
    assert n == 3 and v == pytest.approx(1 / 3)

    def fake_rows(con, sql, params):
        return rows if params[1].isoformat() >= "2026-10-27" else []

    import datetime as dt

    k = sm.compute(fake_rows, None, "app", "d1", dt.date(2026, 11, 3), 7, "tr")
    assert k["period"] == (dt.date(2026, 10, 27), dt.date(2026, 11, 2))  # D1: one day back
    assert k["has_data"] and not k["enough"] and k["value"] == "—" and k["n"] == 3
    assert k["change"] is None  # no change against a period it cannot measure
    assert sm.fmt(0.123, "pct", "tr") == "%12,3" and sm.fmt(1234.5, "usd", "tr") == "$1.234,50"
    assert sm.fmt(0.123, "pct", "en") == "12.3%" and sm.fmt(1234.5, "usd", "en") == "$1,234.50"


def test_an_app_without_data_offers_a_next_step(client, studio_cfg):
    import dataclasses

    import duckdb

    base = studio_cfg.apps[0]
    waiting = dataclasses.replace(
        base, id="quiet_game", name="Quiet Game", sources={"pulse_sdk": {"bundle_ids": ["x.quiet"]}}
    )
    studio_cfg.apps.append(waiting)
    con = duckdb.connect(str(studio_cfg.warehouse))
    con.execute(
        "INSERT INTO meta_apps (app_id, name, genre, platforms, is_demo, timezone) "
        "VALUES ('quiet_game', 'Quiet Game', 'puzzle', 'ANDROID', true, 'UTC')"
    )
    con.close()
    try:
        c = TestClient(create_app(studio_cfg, token="t", live_fetcher=lambda: SNAP), base_url=HOST)
        page = c.get("/studio/quiet_game").text
        assert "Bu uygulamanın verisi yok" in page and "SDK mağaza derlemesinde kapalı" in page
        assert 'href="/health"' in page and "Mağaza verisi bağlı değil" in page
        assert "Toplayıcıya ulaşılamıyor" not in page  # a normal state is not shown as an error
    finally:
        studio_cfg.apps.remove(waiting)
