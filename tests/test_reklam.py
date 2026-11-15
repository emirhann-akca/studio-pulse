"""/reklam, the ad centre: every game's campaigns on one screen, and making / publishing through creative-forge.

The demo warehouse supplies the numbers (three fictional games with UA, one without); creative-forge is replaced by
tests/fixtures/reklam/fake_forge.py, a stand-in that answers its documented JSON contract and logs every call, so
these tests check exactly what studio-pulse asks creative-forge to do (and that it never asks for --live on its own).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_live_page import HOST  # tests/ is on sys.path (no package)

from studio_pulse.serve import reklam_ai as A
from studio_pulse.serve import reklam_charts as C
from studio_pulse.serve import reklam_data as D
from studio_pulse.serve import reklam_events as E
from studio_pulse.serve.app import create_app

FAKE = Path(__file__).parent / "fixtures" / "reklam" / "fake_forge.py"
UA_APPS = ("mossy_match", "pacer_pal", "quartz_merge")


@pytest.fixture(scope="module")
def cfg(demo_build, tmp_path_factory):
    from studio_pulse import cli

    c = demo_build.copy(tmp_path_factory.mktemp("reklam"))
    assert cli.main(["run", "-c", str(c.path)]) == 0
    return c


@pytest.fixture()
def forge_env(monkeypatch, tmp_path):
    log = tmp_path / "forge.log"
    monkeypatch.setenv("PULSE_FORGE_BIN", str(FAKE))
    monkeypatch.setenv("FAKE_FORGE_LOG", str(log))
    monkeypatch.setenv("FAKE_FORGE_STATE", str(tmp_path / "forge_state.json"))
    monkeypatch.setenv("FAKE_FORGE_ROOT", str(tmp_path / "forge_root"))
    return log


@pytest.fixture()
def client(cfg, forge_env):
    return TestClient(create_app(cfg, token="t"), base_url=HOST)


@pytest.fixture()
def bare(cfg, monkeypatch, tmp_path):
    """No creative-forge anywhere: the page must still show every number."""
    monkeypatch.setenv("PULSE_FORGE_BIN", str(tmp_path / "missing-forge"))
    return TestClient(create_app(cfg, token="t"), base_url=HOST)


def calls(log: Path) -> list[list[str]]:
    return (
        [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )


POST = {"x-pulse-token": "t"}


# -- the page -------------------------------------------------------------------------------------------
def test_home_opens_a_game_with_ad_data_and_remembers_the_choice(client):
    r = client.get("/reklam", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].split("/")[-1] in UA_APPS
    page = client.get("/reklam/quartz_merge")
    assert page.status_code == 200 and page.cookies.get("reklam-app") == "quartz_merge"
    r = client.get("/reklam", follow_redirects=False)
    assert r.headers["location"] == "/reklam/quartz_merge"
    r = client.get("/reklam?oyun=pacer_pal", follow_redirects=False)  # the switcher's form
    assert r.status_code == 303 and r.headers["location"] == "/reklam/pacer_pal"


def test_every_configured_game_gets_the_same_page(client, cfg):
    for a in cfg.apps:
        for tab in ("genel", "uret", "yayinla", "oynanabilir"):
            r = client.get(f"/reklam/{a.id}?sekme={tab}")
            assert r.status_code == 200, (a.id, tab)
            assert f'value="{a.id}" selected' in r.text  # the switcher lists it, chosen
    no_ua = next(a.id for a in cfg.apps if a.id not in UA_APPS)
    page = client.get(f"/reklam/{no_ua}").text
    assert "Bu oyunun reklam verisi yok" in page and "/connections" in page


def test_overview_has_kpis_charts_markers_table_and_sample_badge(client):
    page = client.get("/reklam/quartz_merge?range=30d").text
    for label in (
        "Harcama",
        "Gösterim",
        "Video izlenme",
        "Kurulum",
        "CPI",
        "D0 ROAS",
        "D7 ROAS",
        "Geri ödeme",
    ):
        assert f'<div class="k">{label}</div>' in page, label
    assert (
        "Örnek veri" in page and "Rakip kreatifleri" in page and 'href="/radar/kreatifler"' in page
    )
    figures = re.findall(r'<figure class="chart rk".*?</figure>', page, re.S)
    assert len(figures) >= 6
    for fig in figures:  # one y-axis per chart, a table view for every chart
        if "rk-empty" not in fig and "hbars" not in fig:  # the network split is horizontal bars
            assert fig.count('class="yaxis"') == 1 and "Veri tablosu" in fig
    assert 'class="mk-n"' in page  # the demo's new TikTok creative is marked on the date charts
    assert "yeni kreatif" in page and "TT_Merge_AEO" in page
    assert "data-sortable" in page and "lv-network" in page and "lv-creative" in page
    assert "TikTok" in page and "Unity Ads" in page and "Bağlı değil" in page
    en = client.get("/reklam/quartz_merge?range=30d&lang=en").text
    assert "Ad Centre" in en and "Sample data" in en and "Data table" in en


def test_periods_compare_with_the_period_before():
    last, today = dt.date(2026, 10, 31), dt.date(2026, 11, 1)
    p = D.period("7d", last, today)
    assert (p.start, p.end, p.prev_start, p.prev_end) == (
        dt.date(2026, 10, 25),
        dt.date(2026, 10, 31),
        dt.date(2026, 10, 18),
        dt.date(2026, 10, 24),
    )
    assert D.period("today", last, today).days == 1 and not D.period("today", last, today).is_today
    assert D.period("today", today, today).is_today
    c = D.period(
        "custom", last, today, "2026-10-31", "2026-10-01"
    )  # swapped bounds are put in order
    assert (c.start, c.end, c.days) == (dt.date(2026, 10, 1), dt.date(2026, 10, 31), 31)
    assert D.period("custom", last, today, "nonsense", None).key == "7d"
    assert D.period("custom", last, today, "2020-01-01", "2026-10-31").days == 180


def test_creative_drawer_fragment_and_full_page(client):
    page = client.get("/reklam/quartz_merge?range=30d").text
    key = re.search(r'/reklam/quartz_merge/kreatif\?key=([^"&]+)', page).group(1)
    frag = client.get(
        f"/reklam/quartz_merge/kreatif?key={key}&range=30d", headers={"hx-request": "true"}
    )
    assert frag.status_code == 200 and "<html" not in frag.text
    assert (
        "Yorgunluk" in frag.text and "Nerede yayında" in frag.text and "Zaman içinde" in frag.text
    )
    full = client.get(f"/reklam/quartz_merge/kreatif?key={key}")
    assert full.status_code == 200 and "<html" in full.text
    assert client.get("/reklam/quartz_merge/kreatif?key=nope").status_code == 404


def test_unknown_game_is_plain_text_and_never_echoed(client):
    r = client.get("/reklam/%3Cscript%3Ealert(1)")
    assert (
        r.status_code == 404
        and "<script>" not in r.text
        and r.headers["content-type"].startswith("text/plain")
    )


# -- the AI summary ------------------------------------------------------------------------------------
def test_summary_is_grounded_and_says_when_a_change_is_noise(client):
    r = client.get("/reklam/quartz_merge/ozet?range=30d")
    assert r.status_code == 200
    text = re.search(r'<p class="rk-ai-text">(.*?)</p>', r.text, re.S).group(1)
    assert "harcandı" in text and ("anlamlı" in text) and "şablon" in r.text
    # the change the demo made (a new TikTok creative) is named with its before / after
    assert "TT_Merge_AEO" in text and "→" in text


def _ctx(cfg, app_id: str, key: str = "30d") -> dict:
    from collections import defaultdict

    from studio_pulse.serve.app import _rows as rows
    from studio_pulse.serve.app import open_db

    con, _ = open_db(cfg)
    try:
        app = next(a for a in cfg.apps if a.id == app_id)
        last = D.last_day(rows, con, app_id)
        p = D.period(key, last, cfg.as_of)
        camp = defaultdict(dict)
        for r in rows(con, f"SELECT campaign_name, date, {D._AGG} FROM stg_ua_spend WHERE app_id = ? "
                           "GROUP BY campaign_name, date", [app_id]):  # fmt: skip
            camp[str(r["campaign_name"])][r["date"]] = D.metrics(r)
        return {
            "period": p, "totals": D.totals(rows, con, app_id, p.start, p.end),
            "prev_totals": D.totals(rows, con, app_id, p.prev_start, p.prev_end),
            "roas": D.roas(rows, con, app_id, p.start, p.end, last),
            "daily_cur": D.daily(rows, con, app_id, p.start, p.end)["(all)"],
            "daily_prev": D.daily(rows, con, app_id, p.prev_start, p.prev_end)["(all)"],
            "creatives": D.creatives_in(rows, con, app_id, p.start, p.end),
            "events": E.load(cfg, app_id, None, D.store_ids(app)), "data_end": last, "campaign_daily": camp,
            "tired": [],
        }  # fmt: skip
    finally:
        con.close()


@pytest.mark.parametrize("lang", ["tr", "en"])
def test_every_number_in_the_template_verifies(cfg, lang):
    for app_id in UA_APPS:
        sentences, facts = A.build(_ctx(cfg, app_id), lang)
        text, dropped = A.verified_text(sentences, facts, app_id)
        assert dropped == [] and text, (app_id, dropped)


def test_a_model_may_reword_but_not_add_numbers(cfg, tmp_path):
    from studio_pulse.brain.llm import FakeLLM, LLMReply

    ctx = _ctx(cfg, "quartz_merge")
    s = A.Summarizer(cfg)
    s.cache = tmp_path / "ai"
    s._client = FakeLLM({"restyle": lambda req: LLMReply(data={"text": "IPM 99,9 oldu, harika."})})
    out = s.summary(ctx, "tr", "quartz_merge")
    assert out["source"] == "template" and "99,9" not in out["text"]
    s.cache = tmp_path / "ai2"
    s._client = FakeLLM()  # an obedient model: returns the text unchanged, which verifies
    out = s.summary(ctx, "tr", "quartz_merge")
    assert out["source"] == "narrator" and not out["cached"]
    assert s.summary(ctx, "tr", "quartz_merge")["cached"]  # the second open costs nothing


def test_noise_test_needs_three_days_a_side():
    assert A.welch_days([1.0, 2.0], [3.0, 4.0, 5.0]) is None
    p = A.welch_days([5.0, 5.2, 4.9, 5.1, 5.0], [7.0, 7.1, 6.9, 7.2, 7.0])
    assert p is not None and p < 0.001
    assert A.welch_days([5.0, 7.0, 4.0], [6.0, 4.5, 6.5]) > 0.05


def test_ai_budget_defaults_to_five_cents(cfg):
    assert A.Summarizer(cfg).block["budget_usd"] == 0.05


# -- charts ------------------------------------------------------------------------------------------------
def test_chart_formats_and_escapes():
    assert C.fmt(1234.5, "usd", "tr") == "$1.234,50" and C.fmt(1234.5, "usd", "en") == "$1,234.50"
    assert C.fmt(0.123, "pct", "tr") == "%12,3" and C.fmt(0.123, "pct", "en") == "12.3%"
    assert C.fmt(1.25, "roas", "tr") == "%125" and C.fmt(None, "usd", "tr") == "—"
    assert (
        C.day(dt.date(2026, 10, 9), "tr") == "9 Eki"
        and C.day(dt.date(2026, 10, 9), "en") == "Oct 9"
    )
    html = C.line(C.Chart("<b>t</b>", markers=[C.Marker(dt.date(2026, 1, 2), 1, "<img src=x>")]),
                  [dt.date(2026, 1, 1), dt.date(2026, 1, 2)], [C.Series("<s>", [1, 2])], "tr")  # fmt: skip
    assert "<b>" not in html and "<img" not in html and "<s>" not in html and "&lt;img" in html


# -- refresh -------------------------------------------------------------------------------------------------
def test_refresh_reads_only_this_games_ad_networks_through_today(cfg, forge_env):
    app = create_app(cfg, token="t")
    seen = []
    app.state.reklam_launcher = lambda c, app_id, kinds: seen.append((app_id, kinds)) or 1
    c = TestClient(app, base_url=HOST)
    assert c.post("/reklam/mossy_match/yenile").status_code == 403  # no token
    assert (
        c.post(
            "/reklam/mossy_match/yenile", headers={**POST, "origin": "https://evil.example"}
        ).status_code
        == 403
    )
    r = c.post("/reklam/mossy_match/yenile", headers=POST)
    assert r.status_code == 202 and seen == [("mossy_match", ["google_ads", "meta_ads", "ua_csv"])]
    no_ua = next(a.id for a in cfg.apps if a.id not in UA_APPS)
    assert c.post(f"/reklam/{no_ua}/yenile", headers=POST).status_code == 409


def test_run_accepts_app_source_and_through_today(monkeypatch, cfg):
    from studio_pulse import cli, pipeline

    got = {}

    def fake_run_all(c, **kw):
        got.update(kw, end=c.end_date)
        return {"ok": True, "pages": []}

    monkeypatch.setattr(pipeline, "run_all", fake_run_all)
    rc = cli.main(
        [
            "run",
            "-c",
            str(cfg.path),
            "--app",
            "mossy_match",
            "--source",
            "meta_ads",
            "--through-today",
        ]
    )
    assert rc == 0 and got["apps"] == ["mossy_match"] and got["sources"] == ["meta_ads"]
    assert got["end"] == dt.date.today()


# -- creative-forge: absent, then through its CLI --------------------------------------------------------------
def test_without_creative_forge_the_numbers_stay_and_making_is_off(bare):
    page = bare.get("/reklam/mossy_match?sekme=uret").text
    assert "Creative Forge bulunamadı" in page and "disabled" in page
    assert bare.get("/reklam/mossy_match").status_code == 200
    r = bare.post(
        "/reklam/mossy_match/uret", headers=POST, data={"format": "video", "variants": "2"}
    )
    assert r.status_code == 200 and "m=" in r.headers["hx-redirect"]
    assert "Creative Forge bulunamadı" in bare.get(r.headers["hx-redirect"]).text


def test_wizard_runs_forge_make_with_the_games_ids(client, forge_env):
    r = client.post("/reklam/mossy_match/uret", headers=POST,
                    data={"format": "video", "brief": "toz <b>temizle</b>", "variants": "3", "lang": ["tr", "en"],
                          "mechanic": "goods-sort", "mode": "fake", "hook": "reveal", "ref": "meta:42"})  # fmt: skip
    assert r.status_code == 200 and r.headers["hx-redirect"].endswith("#isler")
    import time

    for _ in range(50):
        make = [c for c in calls(forge_env) if c[0] == "make"]
        if make:
            break
        time.sleep(0.1)
    (args,) = make
    assert (
        args[args.index("--app") + 1] == "mossy_match"
        and args[args.index("--providers") + 1] == "fake"
    )
    assert (
        args[args.index("--store-url") + 1]
        == "https://play.google.com/store/apps/details?id=demo.mossymatch"
    )
    assert (
        args[args.index("--variants") + 1] == "3"
        and args[args.index("--brief") + 1] == "toz <b>temizle</b>"
    )
    assert ["--language", "tr"] == args[args.index("--language") : args.index("--language") + 2]
    assert args[args.index("--hook") + 1] == "reveal" and args[args.index("--ref") + 1] == "meta:42"
    assert "--budget-usd" not in args  # a trial run has no budget and calls nothing paid


def test_wizard_refuses_a_paid_run_without_budget_and_gameplay(client, forge_env):
    r = client.post(
        "/reklam/mossy_match/uret", headers=POST, data={"format": "video", "mode": "real"}
    )
    page = client.get(r.headers["hx-redirect"]).text
    assert "Formu kontrol edin" in page and not [c for c in calls(forge_env) if c[0] == "make"]


def test_review_list_and_decisions_go_through_forge_review(client, forge_env):
    page = client.get("/reklam/mossy_match?sekme=uret").text
    assert (
        "rk-cardx" in page and "dust &lt;b&gt;bold&lt;/b&gt;" in page
    )  # data from forge is escaped
    key = re.search(r'name="key" value="([^"]+)"', page).group(1)
    r = client.post(
        "/reklam/mossy_match/inceleme", headers=POST, data={"key": key, "decision": "reject"}
    )
    assert "Reddetmek için" in client.get(r.headers["hx-redirect"]).text  # a rejection needs a note
    client.post(
        "/reklam/mossy_match/inceleme",
        headers=POST,
        data={"key": key, "decision": "approve", "note": "iyi"},
    )
    rv = [c for c in calls(forge_env) if c[0] == "review"]
    assert len(rv) == 1 and "--approve" in rv[0] and rv[0][rv[0].index("--note") + 1] == "iyi"
    assert client.post("/reklam/mossy_match/inceleme", headers=POST, data={"key": "../../x", "decision": "approve"}
                       ).status_code == 400  # fmt: skip


def test_publish_prepares_paused_then_activation_needs_the_typed_phrase(client, forge_env):
    key = re.search(
        r'name="key" value="([^"]+)"', client.get("/reklam/mossy_match?sekme=uret").text
    ).group(1)
    client.post(
        "/reklam/mossy_match/inceleme",
        headers=POST,
        data={"key": key, "decision": "approve", "note": "ok"},
    )
    form = {"run": "runs/mossy-match-1", "name": "mm-test", "daily": "20", "total": "100", "countries": "TR, US",
            "network": ["meta", "applovin", "evil"]}  # fmt: skip
    r = client.post("/reklam/mossy_match/hazirla", headers=POST, data=form)
    page = client.get(r.headers["hx-redirect"]).text
    assert "duraklatılmış oluşturuldu" in page and "Hiçbiri yayında değil" in page
    seq = [
        c[0] if c[0] == "publish" else " ".join(c[:2])
        for c in calls(forge_env)
        if c[0] in ("publish", "campaign")
    ]
    assert seq == ["publish", "campaign init", "campaign create"]
    init = next(c for c in calls(forge_env) if c[:2] == ["campaign", "init"])
    assert ["--network", "evil"] not in [init[i : i + 2] for i in range(len(init))]
    assert init[init.index("--app") + 1] == "mossy_match" and init.count("--country") == 2

    r = client.post(
        "/reklam/mossy_match/yayina-al",
        headers=POST,
        data={"ref": "p1:meta", "phrase": "YAYINA AL"},
    )
    page = client.get(r.headers["hx-redirect"]).text
    assert (
        "onay ifadesi tutmuyor" in page and "YAYINA AL 20.00 USD" in page
    )  # creative-forge refused, in Turkish
    r = client.post("/reklam/mossy_match/yayina-al", headers=POST,
                    data={"ref": "p1:meta", "phrase": "YAYINA AL 20.00 USD", "live": "1"})  # fmt: skip
    assert "yayına alındı" in client.get(r.headers["hx-redirect"]).text
    acts = [c for c in calls(forge_env) if c[:2] == ["campaign", "activate"]]
    assert all("--live" not in a for a in acts)  # a mock campaign: the live box is ignored
    r = client.post("/reklam/mossy_match/durdur", headers=POST, data={"ref": "p1:meta"})
    assert "durduruldu" in client.get(r.headers["hx-redirect"]).text
    assert (
        client.post(
            "/reklam/mossy_match/yayina-al", headers=POST, data={"ref": "other:meta"}
        ).status_code
        == 400
    )


def test_a_live_campaign_is_only_switched_on_with_the_live_box(client, forge_env, monkeypatch):
    monkeypatch.setenv("FAKE_FORGE_MODE", "live")
    page = client.get("/reklam/mossy_match?sekme=yayinla").text
    assert "Gerçek hesap (live)" in page and "gerçek hesap" in page
    r = client.post(
        "/reklam/mossy_match/yayina-al",
        headers=POST,
        data={"ref": "p1:meta", "phrase": "YAYINA AL 20.00 USD"},
    )
    assert "gerçek hesapta (live)" in client.get(r.headers["hx-redirect"]).text
    client.post("/reklam/mossy_match/yayina-al", headers=POST,
                data={"ref": "p1:meta", "phrase": "YAYINA AL 20.00 USD", "live": "1"})  # fmt: skip
    acts = [c for c in calls(forge_env) if c[:2] == ["campaign", "activate"]]
    assert ["--live" in a for a in acts] == [False, True]


def test_forge_changes_are_markers_on_the_charts(client):
    page = client.get("/reklam/mossy_match?range=custom&from=2026-10-01&to=2026-10-31").text
    assert "Creative Forge" in page and "Meta_AEO_Tier1 yayına alındı" in page


def test_media_and_playables_are_served_only_by_token(client):
    page = client.get("/reklam/mossy_match?sekme=oynanabilir").text
    tok = re.search(r"/reklam/oynat/([0-9a-f]{24})", page).group(1)
    r = client.get(f"/reklam/oynat/{tok}")
    assert r.status_code == 200 and "sandbox allow-scripts" in r.headers["content-security-policy"]
    assert "connect-src 'none'" in r.headers["content-security-policy"]
    assert client.get("/reklam/oynat/" + "0" * 24).status_code == 404
    assert client.get("/reklam/medya/../../etc/passwd").status_code == 404
    assert client.get("/reklam/medya/" + "a" * 24).status_code == 404
    page = client.get("/reklam/mossy_match?sekme=uret").text
    vid = re.search(r"/reklam/medya/([0-9a-f]{24})", page).group(1)
    assert client.get(f"/reklam/medya/{vid}").headers["content-type"] == "video/mp4"


def test_competitor_library_link_prefills_the_wizard(client):
    r = client.get("/reklam?brief=<script>alert(1)</script> toz&hook=reveal&mechanic=hexa-sort&ref=meta:99"
                   "&orientation=portrait&genre=puzzle", follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and "sekme=uret" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; toz" in page
    assert 'value="reveal" selected' in page and 'value="color-sort" selected' in page
    assert 'name="ref" value="meta:99"' in page and "Rakip kreatifinden geldi" in page
    # the library's own hook labels map to creative-forge's hook types; unknown ones are dropped
    page = client.get(
        "/reklam/mossy_match?sekme=uret&brief=x&hook=before_after&ref=youtube:AbC_1"
    ).text
    assert 'value="before-after" selected' in page
    page = client.get("/reklam/mossy_match?sekme=uret&brief=x&hook=celebrity_meme").text
    assert "celebrity_meme" not in page


# -- the demo data -------------------------------------------------------------------------------------------
def test_demo_writes_changes_views_and_fatigue(cfg):
    from studio_pulse.serve.app import _rows as rows
    from studio_pulse.serve.app import open_db

    ev = E.from_files(cfg, "quartz_merge")
    assert {e["kind"] for e in ev} >= {"activated", "creative_added"} and all(
        e["source"] == "demo" for e in ev
    )
    con, _ = open_db(cfg)
    try:
        v = rows(con, "SELECT network, SUM(video_views) AS v, SUM(video_completions) AS c FROM stg_ua_spend "
                      "GROUP BY network")  # fmt: skip
    finally:
        con.close()
    by = {r["network"]: r for r in v}
    assert by["meta"]["v"] and by["tiktok"]["v"] and by["meta"]["c"] < by["meta"]["v"]
    assert (
        by["google_ads"]["v"] is None and by["applovin"]["v"] is None
    )  # networks without views stay empty


def test_connectors_read_video_views():
    from studio_pulse.connectors.ua import meta, tiktok

    row = meta.to_row({"date_start": "2026-10-01", "actions": [{"action_type": "video_view", "value": "120"},
                                                               {"action_type": "mobile_app_install", "value": "3"}],
                       "video_p100_watched_actions": [{"action_type": "video_view", "value": "20"}]})  # fmt: skip
    assert (row["video_views"], row["video_completions"], row["network_installs"]) == (120, 20, 3.0)
    assert meta.to_row({"date_start": "2026-10-01"})["video_views"] is None
    t = tiktok.to_row({"dimensions": {"stat_time_day": "2026-10-01 00:00:00"},
                       "metrics": {"video_watched_2s": "50", "video_views_p100": "7"}}, "1")  # fmt: skip
    assert (t["video_views"], t["video_completions"]) == (50, 7)
