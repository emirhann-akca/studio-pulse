"""B9: /insights and /ask in `pulse serve` (TestClient, no network)."""

from __future__ import annotations

import re

import pytest
import yaml
from starlette.testclient import TestClient

from studio_pulse.config import load_config
from studio_pulse.serve.app import create_app

TOKEN = "test-token"


@pytest.fixture()
def cfg(brain_demo, tmp_path):
    data = yaml.safe_load(brain_demo.config_path.read_text(encoding="utf-8"))
    brain_demo.copy(tmp_path)
    data.update(warehouse="warehouse.duckdb", state_dir="pulse-state", site_dir="site")
    for app in data["apps"]:
        app["sources"] = {}
    p = tmp_path / "pulse.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_config(p)


@pytest.fixture()
def client(cfg):
    c = TestClient(create_app(cfg, token=TOKEN, launcher=lambda c: 0), base_url="http://127.0.0.1")
    c.cookies.set("ui-lang", "en")  # these tests read the English texts
    return c


def test_insights_page_shows_cards_with_evidence(client, no_network):
    r = client.get("/insights")
    assert r.status_code == 200
    html = r.text
    assert "Insights" in html and "Level 7 is a difficulty spike" in html
    assert "concentrated in app version = 1.8.0" in html
    assert "Mann-Whitney" in html and "<code>difficult_level:quartz_merge:7</code>" in html
    assert "SELECT" in html and "Recommended" in html
    assert 'aria-current="page"' in html and 'href="/ask"' in html


def test_insights_page_in_turkish(client):
    r = client.get("/insights?lang=tr")
    assert r.status_code == 200
    assert "İçgörüler" in r.text and "Seviye 7 bir zorluk sıçraması" in r.text
    assert "%60,9" in r.text and 'lang="tr"' in r.text


def test_insights_are_computed_live_before_the_first_stored_run(cfg, client):
    import duckdb

    con = duckdb.connect(str(cfg.warehouse))
    con.execute("DELETE FROM meta_insights")
    con.close()
    r = client.get("/insights")
    assert r.status_code == 200 and "Computed live" in r.text and "difficulty spike" in r.text


def test_stale_data_shows_the_data_first_banner(cfg, tmp_path):
    import dataclasses
    import datetime as dt

    import duckdb

    con = duckdb.connect(str(cfg.warehouse))
    con.execute("DELETE FROM meta_insights")
    con.close()
    stale = dataclasses.replace(cfg, as_of=cfg.as_of + dt.timedelta(days=20))
    c = TestClient(
        create_app(stale, token=TOKEN, launcher=lambda c: 0), base_url="http://127.0.0.1"
    )
    r = c.get("/insights", params={"lang": "en"})
    assert "Data first:" in r.text and "explanations are withheld" in r.text
    assert "difficulty spike" not in r.text


def test_ask_get_answers_with_route_sql_and_table(client, no_network):
    r = client.get("/ask", params={"q": "Which levels are the hardest in quartz_merge?"})
    assert r.status_code == 200
    assert (
        "Verified query" in r.text
        and "fct_level_funnel" in r.text
        and '<table class="t">' in r.text
    )
    r = client.get("/ask", params={"q": "quartz_merge en zor seviyeler hangileri?", "lang": "tr"})
    assert "Doğrulanmış sorgu" in r.text and "%" in r.text


def test_ask_post_needs_the_token_and_same_origin(client):
    assert client.post("/ask", data={"q": "Is the data fresh?"}).status_code == 403
    r = client.post(
        "/ask", data={"q": "Is the data fresh?"},
        headers={"X-Pulse-Token": TOKEN, "Origin": "http://evil.example"},
    )  # fmt: skip
    assert r.status_code == 403
    r = client.post(
        "/ask", data={"q": "Give me the user ids", "lang": "en"}, headers={"X-Pulse-Token": TOKEN}
    )
    assert r.status_code == 200 and "Cannot answer" in r.text and "<html" not in r.text


def test_untrusted_text_is_escaped(cfg, client):
    import duckdb

    con = duckdb.connect(str(cfg.warehouse))
    con.execute(
        "UPDATE fct_ua_summary SET campaign_name = '<script>alert(1)</script>' "
        "WHERE roas_d7 = (SELECT MAX(roas_d7) FROM fct_ua_summary WHERE campaign_key IS NOT NULL)"
    )
    con.close()
    r = client.get("/ask", params={"q": "Which campaign has the best D7 ROAS?"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_requests_for_other_hosts_are_refused(client):
    r = client.get("/insights", headers={"host": "evil.example"})
    assert r.status_code == 400


def test_nav_links_on_existing_pages(client):
    html = client.get("/").text
    assert re.search(r'href="/insights"', html) and re.search(r'href="/ask"', html)


def test_a_link_from_another_site_does_not_run_a_question(client, no_network):
    """GET /ask?q= from another site (a link, a redirect) shows the form but asks nothing:
    an LLM call costs money, so only this UI's own pages, a typed URL or a bookmark answer."""
    q = {"q": "Which levels are the hardest in quartz_merge?"}
    for site in ("cross-site", "same-site"):
        r = client.get(
            "/ask", params=q, headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "navigate"}
        )
        assert r.status_code == 200 and "Verified query" not in r.text, site
        assert "quartz_merge" in r.text  # the question is kept in the box
    for site in ("same-origin", "none"):
        r = client.get("/ask", params=q, headers={"Sec-Fetch-Site": site})
        assert "Verified query" in r.text, site
    # an <img src=/ask?q=...> on another page is not a navigation: refused outright
    r = client.get(
        "/ask", params=q, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}
    )
    assert r.status_code == 403
