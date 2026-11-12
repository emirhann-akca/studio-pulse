"""/studio: the one-window app view (apps on the left, the app in the middle, its AI on the right)."""

from __future__ import annotations

import copy
import re

import pytest
from fastapi.testclient import TestClient
from test_live_page import HOST, SNAP  # tests/ is on sys.path (no package)

from studio_pulse.serve.app import create_app

# a game whose SDK just sent its first events: in the collector, not in the warehouse yet
BRAND_NEW = {
    "app_id": "demo.brandnew", "name": "Brand New", "status": "new", "platform_status": {},
    "first_seen_ms": 1_792_999_990_000, "last_seen_ms": 1_792_999_999_000, "genre": "game",
    "active": 3, "platforms": {"android": 3},
    "by": [{"platform": "android", "store": "unknown", "country": "TR", "build_env": "debug", "active": 3}],
    "events_per_min": [0] * 29 + [12], "events_last_min": 12,
}  # fmt: skip


@pytest.fixture(scope="module")
def studio_cfg(demo_build, tmp_path_factory):
    from studio_pulse import cli

    cfg = demo_build.copy(tmp_path_factory.mktemp("studio_ui"))
    assert cli.main(["run", "-c", str(cfg.path)]) == 0
    return cfg


def snap_with_new_game():
    s = copy.deepcopy(SNAP)
    s["apps"].append(BRAND_NEW)
    s["new_apps"].append({"app_id": "demo.brandnew", "platform": "android", "name": "Brand New",
                          "first_seen_ms": BRAND_NEW["first_seen_ms"]})  # fmt: skip
    return s


@pytest.fixture()
def client(studio_cfg):
    return TestClient(
        create_app(studio_cfg, token="t", live_fetcher=snap_with_new_game), base_url=HOST
    )


def test_studio_opens_on_an_app_and_lists_every_app(client, studio_cfg):
    r = client.get("/studio", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("/studio/")
    page = client.get(r.headers["location"]).text
    for a in studio_cfg.apps:
        assert f'href="/studio/{a.id}?lang=tr"' in page, a.id
    assert "Uygulamalar" in page and "Yapay zekâ" in page and "Pazar radarı" in page


def test_a_new_sdk_game_appears_at_the_bottom_with_its_live_players(client):
    page = client.get("/studio/pebble_pop").text
    side = page[page.index('id="sidebar"') :]
    assert "Yeni gelenler" in side
    new_at = side.index("Yeni gelenler")
    assert side.index("Brand New") > new_at  # under "new arrivals", not among the apps
    assert re.search(
        r"Brand New</span>\s*<span class=\"now\"[^>]*><span class=\"dot\"[^>]*></span> 3<", side
    )
    assert client.get("/studio/demo_brandnew").status_code == 200  # opens before any refresh
    frag = client.get("/studio/fragments/sidebar", params={"app": "pebble_pop"}).text
    assert "Brand New" in frag and 'aria-current="page"' in frag


def test_app_page_shows_numbers_live_players_levels_and_ai(client):
    page = client.get("/studio/pebble_pop").text
    assert "Pebble Pop" in page and "Günlük aktif (7 gün ort.)" in page and "D1 elde tutma" in page
    assert re.search(r'class="dot"[^>]*></span> 5 şu an', page)  # 5 active in the snapshot
    assert "En zor bölümler" in page and "Öneriler" in page
    assert '<svg class="dau"' in page  # the 30-day DAU chart


def test_a_ga4_app_shows_no_live_count(client, studio_cfg):
    ga4 = next(
        a.id for a in studio_cfg.apps if not any(k.startswith("pulse_sdk") for k in a.sources)
    )
    page = client.get(f"/studio/{ga4}").text
    head = page[page.index("<main") : page.index("</h1>") + 200]
    assert "şu an" not in head


def test_unknown_app_is_404(client):
    assert client.get("/studio/no_such_app").status_code == 404


def test_english_and_recommendations_are_distinct(client):
    page = client.get("/studio/quartz_merge", params={"lang": "en"}).text
    assert "Recommendations" in page and "Apps" in page
    recs = re.findall(r'<div class="rec">([^<]+)<', page)
    assert len(recs) == len(set(recs))


def test_ask_is_scoped_to_the_app_and_needs_the_token(client):
    assert client.post("/studio/pebble_pop/ask", data={"q": "x"}).status_code == 403
    r = client.post(
        "/studio/mossy_match/ask",
        data={"q": "en zor seviyeler hangileri?", "lang": "tr"},
        headers={"X-Pulse-Token": "t"},
    )
    assert r.status_code == 200 and "mossy_match" in r.text


def test_an_sdk_only_app_without_data_is_awaiting_its_sdk(demo_build, tmp_path):
    """13 Nov 2026: Acorn Mart (SDK source only, no build ships it yet) showed a GA4 badge."""
    import dataclasses

    import duckdb

    cfg = demo_build.copy(tmp_path)
    from studio_pulse import cli

    assert cli.main(["run", "-c", str(cfg.path)]) == 0
    base = next(a for a in cfg.apps if a.id == "pebble_pop")
    waiting = dataclasses.replace(
        base, id="next_game", name="Next Game", sources={"pulse_sdk": {"bundle_ids": ["x.next"]}}
    )
    cfg.apps.append(waiting)
    con = duckdb.connect(str(cfg.warehouse))
    con.execute(
        "INSERT INTO meta_apps (app_id, name, genre, platforms, is_demo, timezone) "
        "VALUES ('next_game', 'Next Game', 'puzzle', 'ANDROID', true, 'UTC')"
    )
    con.close()
    c = TestClient(create_app(cfg, token="t", live_fetcher=lambda: SNAP), base_url=HOST)
    page = c.get("/studio/next_game").text
    assert 'class="st waiting">SDK bekleniyor<' in page
    assert re.search(r"Mossy Match</span>\s*<span class=\"st ga4\">GA4<", page)
