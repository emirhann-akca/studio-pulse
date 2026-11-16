"""New launches: icon URLs, the curated publisher preset, fct_mr_launches, the digest section,
the local page /radar/yeni and the shareable deck (`pulse radar launches --share`).

Everything runs on invented data (tests/radar_synth.py build_launches): nothing is fetched,
and the PDF step never starts a real browser."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml
from radar_synth import LAUNCH_DAY, SYNTH_PUBLISHER, build_launches

from studio_pulse import warehouse
from studio_pulse.config import ConfigError, config_from_dict
from studio_pulse.radar import apple, digest, launches, publishers, store
from studio_pulse.radar import run as radar_run
from studio_pulse.radar.settings import DEFAULT_PARAMS, from_config
from studio_pulse.radar.taxonomy import load as load_taxonomy

D = LAUNCH_DAY
CDN_HOSTS = [f"https://is{i}-ssl.mzstatic.com" for i in range(1, 6)]
LAUNCHED = {"990100001", "990100002", "990100003", "990100004", "990100010"}


# -- icons -------------------------------------------------------------------------------------


def test_icon_urls_are_kept_only_on_apples_image_cdn():
    ok = "https://is1-ssl.mzstatic.com/image/thumb/Purple/v4/ab/cd/AppIcon/512x512bb.jpg"
    assert apple.image_url(ok) == ok
    assert apple.image_url(ok.replace("is1-", "is5-")) == ok.replace("is1-", "is5-")
    for bad in (
        ok.replace("https://", "http://"),
        ok.replace("is1-", "is6-"),
        "https://evil.example/AppIcon/512x512bb.jpg",
        "https://is1-ssl.mzstatic.com.evil.example/x.jpg",
        "https://user@is1-ssl.mzstatic.com/x.jpg",
        "https://is1-ssl.mzstatic.com:8443/x.jpg",
        "https://IS1-SSL.MZSTATIC.COM/x.jpg",
        "javascript:alert(1)",
        'https://is1-ssl.mzstatic.com/x.jpg" onerror="alert(1)',
        "https://is1-ssl.mzstatic.com/x.jpg?<script>",
        "https://is1-ssl.mzstatic.com/x\n.jpg",
        "",
        None,
        512,
    ):
        assert apple.image_url(bad) is None, bad
    base = {"trackId": 1, "trackName": "X", "genres": []}
    row = apple.app_row({**base, "artworkUrl512": ok}, D, "us", "lookup")
    assert row["icon_url"] == ok
    small = ok.replace("512x512", "100x100")
    row = apple.app_row(
        {**base, "artworkUrl512": "http://evil.example/i.png", "artworkUrl100": small},
        D, "us", "lookup",
    )  # fmt: skip
    assert row["icon_url"] == small
    assert apple.app_row(base, D, "us", "lookup")["icon_url"] is None
    assert (
        apple.app_row({**base, "artworkUrl512": "https://evil.example/i.png"}, D, "us", "lookup")[
            "icon_url"
        ]
        is None
    )


def test_icon_column_is_added_to_an_existing_warehouse(tmp_path):
    con = warehouse.connect(tmp_path / "old.duckdb")
    try:
        # a warehouse from before icon_url: raw_mr_app without the column
        con.execute("CREATE TABLE raw_mr_app (snapshot_date DATE, app_id VARCHAR, source VARCHAR)")
        con.execute("INSERT INTO raw_mr_app VALUES ('2026-11-01', '1', 'lookup')")
        store.init_schema(con)
        store.init_schema(con)  # idempotent
        assert "icon_url" in store.columns(con, "raw_mr_app")
        assert con.execute("SELECT COUNT(*) FROM raw_mr_app").fetchone()[0] == 1
    finally:
        con.close()


# -- the curated publisher preset --------------------------------------------------------------


def test_the_shipped_publisher_preset_is_well_formed():
    known = publishers.presets()
    assert "casual" in known
    pubs = known["casual"]["publishers"]
    assert len(pubs) >= 15
    ids = [p["id"] for p in pubs]
    assert len(ids) == len(set(ids))
    for p in pubs:
        assert publishers.ARTIST_ID.match(p["id"]), p
        assert p["name"] and p["artist_name"]
        assert dt.date(2026, 11, 1) <= p["verified_on"] <= dt.date.today()


def _preset(**entry):
    p = {
        "id": "1452111779",
        "name": "A",
        "artist_name": "A Ltd",
        "verified_on": dt.date(2026, 11, 16),
    }
    p.update(entry)
    return yaml.safe_dump({"version": 1, "presets": {"x": {"publishers": [p]}}})


def test_the_preset_loader_refuses_malformed_entries():
    assert publishers.parse(_preset())["x"]["publishers"][0]["id"] == "1452111779"
    for bad in (
        _preset(id="12ab"),
        _preset(id="0123456"),
        _preset(artist_name=""),
        _preset(name=None),
        _preset(verified_on=None),
        yaml.safe_dump({"version": 2, "presets": {}}),
        yaml.safe_dump({"version": 1, "presets": {"x": {"publishers": []}}}),
        yaml.safe_dump(
            {
                "version": 1,
                "presets": {
                    "x": {
                        "publishers": [
                            {
                                "id": "1452111779",
                                "name": "A",
                                "artist_name": "A",
                                "verified_on": dt.date(2026, 11, 16),
                            },
                            {
                                "id": "1452111779",
                                "name": "B",
                                "artist_name": "B",
                                "verified_on": dt.date(2026, 11, 16),
                            },
                        ]
                    }
                },
            }
        ),  # fmt: skip
    ):
        with pytest.raises(publishers.PresetError):
            publishers.parse(bad)


def _cfg(tmp_path: Path, **radar) -> object:
    return config_from_dict(
        {
            "apps": [{"id": "x"}],
            "warehouse": str(tmp_path / "w.duckdb"),
            "state_dir": str(tmp_path / "state"),
            "radar": {"out_dir": str(tmp_path / "radar"), **radar},
        },
        base_dir=tmp_path,
    )


def test_the_preset_is_opt_in_and_reads_catalogues_daily(tmp_path):
    plain = from_config(_cfg(tmp_path, publishers=[{"id": "1743349083", "name": "Mine"}]))
    assert plain.publisher_preset == [] and plain.publisher_every_days == 7
    assert [p["id"] for p in plain.publishers] == ["1743349083"]
    assert plain.params["launch_days"] == 14
    preset = publishers.presets()["casual"]["publishers"]
    s = from_config(
        _cfg(
            tmp_path,
            publisher_preset="casual",
            publishers=[{"id": "714804730", "name": "My name for it"}, {"id": "1743349083"}],
        )
    )
    assert s.publisher_preset == ["casual"] and s.publisher_every_days == 1
    ids = [p["id"] for p in s.publishers]
    assert ids[:2] == ["714804730", "1743349083"] and len(ids) == len(set(ids))
    assert set(ids) == {p["id"] for p in preset} | {"1743349083"}
    assert s.publishers[0]["name"] == "My name for it"  # an explicit entry wins
    weekly = from_config(_cfg(tmp_path, publisher_preset=["casual"], publisher_every_days=7))
    assert weekly.publisher_every_days == 7
    with pytest.raises(ConfigError, match="known: casual"):
        from_config(_cfg(tmp_path, publisher_preset="everything"))
    with pytest.raises(ConfigError, match="publisher_every_days"):
        from_config(_cfg(tmp_path, publisher_every_days=0))
    with pytest.raises(ConfigError, match="launch_days"):
        from_config(_cfg(tmp_path, params={"launch_days": 0}))
    # the daily catalogues count toward the Apple budget floor
    with pytest.raises(ConfigError, match="publishers need"):
        from_config(_cfg(tmp_path, publisher_preset="casual", apple={"daily_budget": 20}))


def test_a_publisher_catalogue_is_asked_for_its_newest_apps():
    class F:
        url = None

        def get(self, url, purpose, cacheable=True):
            F.url = url
            return type("R", (), {"status": 200, "json": lambda self: {"results": []}})()

    assert apple.lookup_publisher(F(), "1452111779", "US") == []
    assert F.url.startswith("https://itunes.apple.com/lookup?")
    assert "id=1452111779" in F.url and "sort=recent" in F.url and "limit=200" in F.url
    assert "country=us" in F.url and "/search" not in F.url


# -- the model ---------------------------------------------------------------------------------


@pytest.fixture()
def lcon():
    c = warehouse.connect(":memory:")
    store.init_schema(c)
    store.set_params(c, DEFAULT_PARAMS)
    build_launches(c)
    radar_run.run_models(c)
    yield c
    c.close()


def _rows(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_launch_model_window_found_via_and_exclusions(lcon):
    rows = {
        r["app_id"]: r
        for r in _rows(lcon, "SELECT * FROM fct_mr_launches WHERE snapshot_date = ?", [D])
    }
    assert set(rows) == LAUNCHED
    # one row per day and game, whatever the storefronts and sources
    n, distinct = lcon.execute(
        "SELECT COUNT(*), COUNT(DISTINCT (snapshot_date, app_id)) FROM fct_mr_launches"
    ).fetchone()
    assert n == distinct
    # never: a 400-day-old first chart entry, a soft launch from 100 days ago, a pre-order, an app
    ever = {r[0] for r in lcon.execute("SELECT DISTINCT app_id FROM fct_mr_launches").fetchall()}
    assert not ever & {"990100005", "990100006", "990100007", "990100008", "990100099"}
    jam = rows["990100002"]
    assert jam["found_via"] == "both" and jam["release_source"] == "lookup"
    assert (jam["best_rank"], jam["best_country"], jam["best_list"]) == (
        12, "tr", "tr:topfreeapplications:6014",
    )  # fmt: skip
    assert jam["countries"] == ["tr", "us"] and jam["age_days"] == 5
    assert jam["mechanic_primary"] == "jam" and jam["mechanics"] == ["block-puzzle", "jam"]
    assert jam["store_url"] == "https://apps.apple.com/tr/app/id990100002"
    assert jam["first_seen"] == D - dt.timedelta(days=1) and not jam["first_chart_entry"]
    assert rows["990100001"]["found_via"] == "publisher" and rows["990100001"]["best_rank"] is None
    run_ = rows["990100003"]
    assert run_["found_via"] == "chart" and run_["first_chart_entry"] and run_["listed_today"]
    feed = rows["990100004"]
    assert feed["release_source"] == "feed" and feed["release_date"] == D - dt.timedelta(days=6)
    assert feed["icon_url"] is None and json.loads(feed["screenshot_urls"]) == []
    for r in rows.values():
        shots = json.loads(r["screenshot_urls"])
        assert len(shots) <= 3 and all(apple.image_url(u) == u for u in shots)
        assert r["is_game"] and 0 <= r["age_days"] <= 14
    bad = rows["990100010"]  # a non-CDN icon and two bad screenshot URLs are dropped
    assert bad["icon_url"] is None and len(json.loads(bad["screenshot_urls"])) == 1
    # Month Old was a launch 20 days ago only
    old = _rows(lcon, "SELECT snapshot_date FROM fct_mr_launches WHERE app_id = '990100009'")
    assert [r["snapshot_date"] for r in old] == [D - dt.timedelta(days=20)]


def test_launch_window_follows_the_parameter_and_recent_reads_the_newest_row(lcon):
    assert {r["app_id"] for r in launches.recent(lcon, D, 14)} == LAUNCHED
    month = launches.recent(lcon, D, 30)
    assert {r["app_id"] for r in month} == LAUNCHED | {"990100009"}
    # its newest row is from 20 days ago, when it was 2 days old; today it is 22
    assert next(r for r in month if r["app_id"] == "990100009")["age_days"] == 22
    assert [r["release_date"] for r in month] == sorted(
        (r["release_date"] for r in month), reverse=True
    )
    assert {r["app_id"] for r in launches.recent(lcon, D, 7)} == LAUNCHED
    assert launches.latest_day(lcon) == D
    store.set_params(lcon, {**DEFAULT_PARAMS, "launch_days": 4})
    radar_run.run_models(lcon)
    got = {
        r[0]
        for r in lcon.execute(
            "SELECT app_id FROM fct_mr_launches WHERE snapshot_date = ?", [D]
        ).fetchall()
    }
    assert got == {
        "990100001",
        "990100003",
    }  # 2 and 3 days old (window: ages 0-3); the others are 4-6
    empty = warehouse.connect(":memory:")
    try:
        assert launches.recent(empty, D, 14) == [] and launches.latest_day(empty) is None
    finally:
        empty.close()


# -- facts and digest --------------------------------------------------------------------------


def test_launches_in_the_facts_and_the_digest(lcon, tmp_path):
    s = from_config(_cfg(tmp_path, publishers=[{"id": SYNTH_PUBLISHER, "name": "Synth Publisher"}]))
    facts = digest.build_facts(lcon, s, D, load_taxonomy())
    assert digest.check_labels(facts) == []
    lb = facts["launches"]
    assert lb["window"] == {
        "days": 14,
        "from": "2026-11-02",
        "to": "2026-11-15",
        "basis": "observed",
    }
    assert lb["count"] == {"value": 5, "basis": "observed"}
    assert {k: v["value"] for k, v in lb["by_found_via"].items()} == {
        "both": 1, "publisher": 2, "chart": 2,
    }  # fmt: skip
    jam = next(i for i in lb["items"] if i["app_id"] == "990100002")
    assert jam["publisher"] == "Synth Publisher" and jam["found_via"] == "both"
    assert jam["best_rank"] == {"value": 12, "basis": "observed", "list": "topfreeapplications:6014",
                                "country": "tr"}  # fmt: skip
    assert jam["release"] == {"value": "2026-11-10", "basis": "observed", "source": "lookup"}
    tr, en = digest.render(facts, "tr"), digest.render(facts, "en")
    assert "## Yeni çıkanlar (son 14 gün)" in tr and "## New launches (last 14 days)" in en
    assert "- Synth Block Jam (Synth Publisher): çıkış 2026-11-10 · yayıncı + liste" in tr
    assert "Türkiye Oyunlar #12" in tr and "Turkey Games #12" in en
    assert "publisher + chart" in en and "(OBSERVED)" in en
    # the watched-publisher section is still there, and still before the new one
    assert tr.index("## İzlenen yayıncıların yeni oyunları") < tr.index("## Yeni çıkanlar")
    assert tr.count("### ") == en.count("### ")
    # launch numbers are facts a narration may use; others are still refused
    assert digest.verify_narration("5 new games, the best at #12.", facts) == []
    assert digest.verify_narration("98 new games.", facts) == ["98"]
    empty = warehouse.connect(":memory:")
    try:
        store.init_schema(empty)
        f0 = digest.build_facts(empty, s, D, load_taxonomy())
        assert f0["launches"]["items"] == [] and "Bu pencerede yeni oyun görülmedi." in (
            digest.render(f0, "tr")
        )
    finally:
        empty.close()


# -- the page ----------------------------------------------------------------------------------


def _warehouse(tmp_path: Path, mutate=None) -> Path:
    path = tmp_path / "w.duckdb"
    con = warehouse.connect(path)
    try:
        store.init_schema(con)
        store.set_params(con, DEFAULT_PARAMS)
        build_launches(con)
        if mutate:
            mutate(con)
        radar_run.run_models(con)
    finally:
        con.close()
    return path


def _client(cfg):
    from fastapi.testclient import TestClient

    from studio_pulse.serve.app import create_app

    return TestClient(create_app(cfg, token="t"), base_url="http://127.0.0.1:8765")


def _csp(resp) -> dict[str, str]:
    out = {}
    for part in resp.headers["content-security-policy"].split(";"):
        if part.strip():
            k, _, v = part.strip().partition(" ")
            out[k] = v
    return out


def test_launches_page_cards_filters_and_policy(tmp_path):
    def hostile(con):  # store text anyone can write
        con.execute(
            "UPDATE raw_mr_app SET name = '<b>Synth</b> \"Pop\" Sort' WHERE app_id = '990100001'"
        )

    _warehouse(tmp_path, hostile)
    cfg = _cfg(tmp_path, publishers=[{"id": SYNTH_PUBLISHER, "name": "Synth Publisher"}])
    c = _client(cfg)
    r = c.get("/radar/yeni")
    assert r.status_code == 200
    page = r.text
    assert "Yeni çıkanlar" in page and page.count('class="ln-card"') == 5
    assert "Synth Month Old" not in page  # 22 days old: outside the default 14-day window
    assert "&lt;b&gt;Synth&lt;/b&gt;" in page and "<b>Synth</b>" not in page
    assert "https://apps.apple.com/tr/app/id990100002" in page
    assert (
        "https://is1-ssl.mzstatic.com/image/thumb/Synthetic/v4/990100002/AppIcon/512x512bb.jpg"
        in page
    )
    assert "evil.example" not in page and "javascript:" not in page
    assert 'href="/radar/yeni" aria-current="page"' in page  # the sidebar entry
    # the policy: the shell's own, plus Apple's image CDN in img-src and nothing else
    csp, shell = _csp(r), _csp(c.get("/radar"))
    assert csp["img-src"].split() == shell["img-src"].split() + CDN_HOSTS
    assert {k: v for k, v in csp.items() if k != "img-src"} == {
        k: v for k, v in shell.items() if k != "img-src"
    }
    assert "'unsafe-inline'" not in csp["script-src"]
    # filters: offered values only, nothing echoed
    month = c.get("/radar/yeni", params={"days": "30"}).text
    assert month.count('class="ln-card"') == 6 and "Synth Month Old" in month
    odd = c.get("/radar/yeni", params={"days": "365", "mechanic": "<script>alert(1)</script>"})
    assert odd.status_code == 200 and odd.text.count('class="ln-card"') == 5
    assert 'value="365"' not in odd.text and "days=365" not in odd.text
    assert "alert(1)" not in odd.text
    jam = c.get("/radar/yeni", params={"mechanic": "jam"}).text
    assert jam.count('class="ln-card"') == 1 and "Synth Block Jam" in jam
    assert '<option value="jam" selected>' in jam
    unknown = c.get("/radar/yeni", params={"mechanic": "merge"}).text
    assert unknown.count('class="ln-card"') == 5 and 'value="merge"' not in unknown
    en = c.get("/radar/yeni", params={"lang": "en"}).text
    assert "New launches" in en and "publisher + chart" in en
    # linked from the radar page
    assert 'href="/radar/yeni"' in c.get("/radar").text


def test_launches_page_without_a_radar_run(tmp_path):
    c = _client(_cfg(tmp_path))
    r = c.get("/radar/yeni")
    assert r.status_code == 200 and "Henüz yeni çıkan yok" in r.text
    assert "radar.publisher_preset: casual" in r.text
