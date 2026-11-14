"""`pulse serve` Canlı / Live page and the SDK app pages (levels, death heatmap)."""

from __future__ import annotations

import re

import pytest
import yaml
from fastapi.testclient import TestClient

from studio_pulse.collector.inprocess import running_collector
from studio_pulse.collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings
from studio_pulse.config import load_config
from studio_pulse.serve.app import create_app
from studio_pulse.simulate.model import GAMES
from studio_pulse.simulate.run import Poster, live

SNAP = {
    "now_ms": 1_793_000_000_000,
    "window_s": 300,
    "spark_minutes": 30,
    "total_active": 7,
    "apps": [
        {"app_id": "demo.pebblepop", "name": "Pebble Pop", "status": "testing", "platform_status": {},
         "first_seen_ms": 1_792_000_000_000, "last_seen_ms": 1_792_999_990_000, "genre": "game",
         "active": 5, "platforms": {"android": 3, "ios": 2},
         "by": [{"platform": "android", "store": "google_play", "country": "TR", "build_env": "release", "active": 3},
                {"platform": "ios", "store": "app_store", "country": "US", "build_env": "release", "active": 2}],
         "events_per_min": [0] * 28 + [40, 55], "events_last_min": 55},
        {"app_id": "demo.lanternleap", "name": "Lantern Leap", "status": "new", "platform_status": {},
         "first_seen_ms": 1_792_999_880_000, "last_seen_ms": 1_792_999_995_000, "genre": "game",
         "active": 2, "platforms": {"android": 2},
         "by": [{"platform": "android", "store": "unknown", "country": "BR", "build_env": "debug", "active": 2}],
         "events_per_min": [0] * 29 + [9], "events_last_min": 9},
    ],
    "new_apps": [{"app_id": "demo.lanternleap", "platform": "android", "name": "Lantern Leap",
                  "first_seen_ms": 1_792_999_880_000}],
    "collector": {"mode": "local", "accepted": 1234, "duplicates": 5, "quarantined": 1},
}  # fmt: skip
HOST = "http://127.0.0.1:8765"


@pytest.fixture(scope="module")
def demo_cfg(demo_build, tmp_path_factory):
    from studio_pulse import cli

    cfg = demo_build.copy(tmp_path_factory.mktemp("live_ui"))
    assert cli.main(["run", "-c", str(cfg.path)]) == 0
    return cfg


def test_live_page_shows_apps_badges_breakdown_and_new_app_banner(demo_cfg):
    client = TestClient(
        create_app(demo_cfg, token="t", live_fetcher=lambda: SNAP),
        base_url=HOST,
        cookies={"ui-lang": "en"},
    )
    html = client.get("/live").text
    assert "Live" in html and 'href="/live" aria-current="page"' in html
    assert 'id="live-total">7<' in html
    assert re.search(r'data-app="demo.pebblepop">5<', html)
    assert 'class="badge warn">testing' in html and 'class="badge info">new' in html
    assert "New app registered: Lantern Leap" in html
    assert "google_play" in html and ">TR<" in html and "debug" in html
    assert '<svg class="spark"' in html and "polyline" in html
    assert 'hx-get="/fragments/live"' in html and 'hx-trigger="every 5s"' in html
    # links go to the config app ids: pebble_pop is declared, lantern leap was registered
    assert 'href="/live/apps/pebble_pop"' in html and 'href="/live/apps/demo_lanternleap"' in html
    frag = client.get("/fragments/live").text
    assert frag.startswith('<div id="live"') and "<html" not in frag
    assert "prefers-color-scheme: dark" in html  # light and dark


def test_sdk_app_name_is_escaped_on_the_live_page(tmp_path):
    """Audit, 14 Nov 2026 (stored XSS): ctx.app_name, sent with the public write key, became
    the registry display name and reached the page unescaped through the sparkline's
    aria-label, which _live.html renders with |safe."""
    import time
    import uuid

    from studio_pulse.collector.app import create_app as collector_app
    from studio_pulse.collector.core import Collector
    from studio_pulse.collector.store import LocalStore
    from studio_pulse.collector.web import sparkline

    payload = '"><img src=x onerror=alert(document.domain)>'
    s = CollectorSettings(data_dir=tmp_path / "c")
    store = LocalStore(s.data_dir)
    col = Collector(s, store)
    now = int(time.time() * 1000)
    body = {"v": 2, "schema": "2", "sent_at": now,
            "ctx": {"app_id": "com.example.evil", "platform": "android", "app_version": "1.0",
                    "install_id": str(uuid.uuid4()), "sdk": "unity/2.0.0", "app_name": payload},
            "events": [{"id": str(uuid.uuid4()), "seq": 0, "name": "level_start", "ts": now,
                        "sid": str(uuid.uuid4()), "snum": 1, "fg_ms": 0, "p": {"level": 1}}]}  # fmt: skip
    r = TestClient(collector_app(s, col)).post(
        "/v2/batch", json=body, headers={"X-Pulse-Key": DEFAULT_LOCAL_KEY}
    )
    assert r.json()["accepted"] == 1 and col.live_snapshot()["apps"][0]["name"] == payload

    cfg_path = tmp_path / "pulse.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "warehouse": str(tmp_path / "w.duckdb"),
                "apps": [{"id": "x", "name": "X", "sources": {}}],
            }
        ),  # fmt: skip
        encoding="utf-8",
    )
    ui = TestClient(
        create_app(load_config(cfg_path), token="t", live_fetcher=col.live_snapshot),
        base_url=HOST,
    )
    for path in ("/fragments/live", "/live"):
        html = ui.get(path).text
        assert "<img src=x" not in html, path
        # the new-app banner and the sparkline both show the name, as text
        assert html.count("&gt;&lt;img src=x onerror=alert(document.domain)&gt;") >= 2, path
    svg = sparkline([0, 3], payload)
    assert "<img" not in svg and 'aria-label="&quot;&gt;&lt;img src=x' in svg
    store.close()


def test_live_page_says_when_the_collector_is_down(demo_cfg):
    def down():
        raise ConnectionError("refused")

    client = TestClient(
        create_app(demo_cfg, token="t", live_fetcher=down), base_url=HOST, cookies={"ui-lang": "en"}
    )
    r = client.get("/live")
    assert (
        r.status_code == 200 and "Collector not reachable" in r.text and "pulse collect" in r.text
    )


def test_sdk_app_page_has_funnel_heatmap_and_settings(demo_cfg):
    client = TestClient(
        create_app(demo_cfg, token="t", live_fetcher=lambda: SNAP),
        base_url=HOST,
        cookies={"ui-lang": "en"},
    )
    r = client.get("/live/apps/pebble_pop")
    assert r.status_code == 200
    html = r.text
    assert (
        "Fail rate by level" in html
        and "Level funnel" in html
        and "Settings players turn off" in html
    )
    hot = re.search(r"Deaths on level (\d+)", html)
    assert hot and int(hot.group(1)) in (12, 25, 40)  # the difficulty spikes have the most deaths
    r = client.get("/live/apps/pebble_pop?level=25")
    assert "Deaths on level 25" in r.text
    r = client.get("/live/apps/demo_tidyhabit")  # an app without levels
    assert r.status_code == 200 and "No level events" in r.text
    assert client.get("/live/apps/unknown_app").status_code == 200


def test_live_page_against_a_real_collector_and_simulator(demo_cfg, tmp_path):
    s = CollectorSettings(data_dir=tmp_path / "c", country_header="CF-IPCountry")
    with running_collector(s) as (url, _):
        rep = live(Poster(url, DEFAULT_LOCAL_KEY), duration_s=3, players=9, games=GAMES,
                   new_game_after_s=1, pace=0.02, first_within_s=0.5, log=lambda m: None)  # fmt: skip
        data = yaml.safe_load(demo_cfg.path.read_text(encoding="utf-8"))
        data["collector"]["url"] = url
        p = tmp_path / "pulse.yaml"
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
        cfg = load_config(p)
        client = TestClient(create_app(cfg, token="t"), base_url=HOST, cookies={"ui-lang": "en"})
        html = client.get("/live").text
    total = int(re.search(r'id="live-total">(\d+)<', html).group(1))
    assert total == sum(rep.installs.values())
    assert "Lantern Leap" in html and "New app registered" in html
