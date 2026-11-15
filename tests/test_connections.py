"""Connections / data flow (/connections), freshness on every page, the studio's own public
store pages on the app page, and the UI fixes around them.

No network and no real scheduler: the scheduler backend is a fake and the per-user state
directory (schedule records) is a temporary one."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_live_page import HOST, SNAP  # tests/ is on sys.path (no package)

from studio_pulse import pipeline, runstate, schedule, warehouse
from studio_pulse.serve import dataflow, studio_metrics
from studio_pulse.serve.app import create_app

TOKEN = "tok"


class FakeBackend:
    kind = "launchd"

    def __init__(self, registered: bool = True):
        self.registered = registered

    def status(self, name):
        return {"backend": "launchd", "id": f"gui/1/dev.studio-pulse.{name}",
                "registered": self.registered, "state": "not running", "last_exit_code": 0}  # fmt: skip


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    """Schedule records go to a temporary per-user directory; the scheduler is a fake."""
    monkeypatch.setenv("PULSE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(dataflow.schedule, "backend", lambda *a, **k: FakeBackend(False))


@pytest.fixture()
def cfg(demo_build, tmp_path):
    return demo_build.copy(tmp_path / "flow")


def client_for(cfg, lang="tr"):
    app = create_app(cfg, token=TOKEN, live_fetcher=lambda: SNAP)
    return TestClient(app, base_url=HOST, cookies={"ui-lang": lang})


def heartbeat(cfg, **kw):
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    hb = {"status": "success", "last_start": (now - dt.timedelta(minutes=10)).isoformat(),
          "last_end": (now - dt.timedelta(minutes=5)).isoformat(),
          "last_success": (now - dt.timedelta(minutes=5)).isoformat(), "duration_s": 329.35,
          "summary": {}}  # fmt: skip
    hb.update(kw)
    runstate.write_json_atomic(runstate.heartbeat_path(cfg.state_dir), hb)
    return hb


def record(cfg_path, at="07:30", log_file=None):
    rec = {"name": "refresh", "backend": "launchd", "at": at, "config": str(cfg_path),
           "log_file": str(log_file) if log_file else None}  # fmt: skip
    runstate.write_json_atomic(schedule.registry_path("refresh"), rec)


# -- the page ---------------------------------------------------------------------------------


def test_connections_lists_every_app_source_and_studio_feed(cfg):
    heartbeat(cfg)
    c = client_for(cfg)
    r = c.get("/connections")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Bağlantılar</h1>" in html
    assert 'href="/connections" aria-current="page"' in html  # in the sidebar
    for a in cfg.apps:
        assert f'id="app-{a.id}"' in html
        for key in a.sources:
            assert key in html
    # the demo's SDK-only game has no store reports: one step each, pointing at Settings
    assert 'href="/settings#app-demo_lanternleap"' in html
    assert "Google Play&#39;i bağla" in html and "App Store&#39;u bağla" in html
    # no UA source: the step names the new AppLovin and Unity Ads sources and their keys
    assert "applovin_ads (PULSE_APPLOVIN_REPORT_KEY)" in html
    assert "unity_ads (PULSE_UNITY_ADS_KEY_ID + PULSE_UNITY_ADS_SECRET_KEY)" in html
    # studio-wide rows, with the one step for what is off
    for label in ("Pazar radarı", "SDK toplayıcı", "Yapay zekâ (LLM)", "Bildirimler (Slack)"):
        assert label in html
    assert "radar.enabled: true" in html
    # rows dated in the last 7 days are counted per source (demo data ends at as_of)
    assert "Satır (7 gün)" in html
    en = client_for(cfg, "en").get("/connections").text
    assert "<h1>Connections</h1>" in en and "Rows (7 days)" in en


def test_rows_7d_counts_only_this_source(cfg):
    con = warehouse.connect(cfg.warehouse, read_only=True)
    try:
        cols: dict[str, set[str]] = {}
        for t, col in con.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
        ).fetchall():
            cols.setdefault(t, set()).add(col)
        since = cfg.as_of - dt.timedelta(days=7)
        play = dataflow._rows_7d(
            con, cols, "mossy_match", "play_console", ["raw_store_installs"], since
        )
        apple = dataflow._rows_7d(
            con, cols, "mossy_match", "app_store", ["raw_store_installs"], since
        )
        both = con.execute(
            "SELECT COUNT(*) FROM raw_store_installs WHERE app_id = 'mossy_match' AND date >= ?",
            [since],
        ).fetchone()[0]
    finally:
        con.close()
    assert play and apple and play + apple == both  # the shared table split by _source


def test_schedule_for_this_config_shows_the_next_run(cfg, monkeypatch):
    heartbeat(cfg)
    monkeypatch.setattr(dataflow.schedule, "backend", lambda *a, **k: FakeBackend(True))
    record(cfg.path, at="07:30")
    html = client_for(cfg).get("/connections").text
    nxt = dataflow.next_occurrence("07:30")
    assert nxt.strftime("%Y-%m-%d 07:30") in html and "her gün 07:30" in html
    assert "beklemede" in html  # launchd's "not running", in Turkish
    assert "Günlük yenileme kurulu değil" not in html
    health = client_for(cfg).get("/health").text
    assert "kurulu" in health and "beklemede" in health and ">Not<" not in health
    settings = client_for(cfg).get("/settings").text
    assert 'name="at" type="time" value="07:30"' in settings  # the installed time, not 08:30


def test_schedule_of_another_config_is_not_reported_as_ours(cfg, monkeypatch, tmp_path):
    heartbeat(cfg)
    monkeypatch.setattr(dataflow.schedule, "backend", lambda *a, **k: FakeBackend(True))
    other = tmp_path / "elsewhere" / "pulse.yaml"
    record(other)
    html = client_for(cfg).get("/connections").text
    assert "başka bir yapılandırma için kurulu" in html and str(other) in html
    health = client_for(cfg).get("/health").text
    assert "başka yapılandırma için" in health
    assert 'value="08:30"' in client_for(cfg).get("/settings").text


def test_a_registered_entry_without_an_install_record_still_counts(cfg, monkeypatch):
    heartbeat(cfg)
    monkeypatch.setattr(dataflow.schedule, "backend", lambda *a, **k: FakeBackend(True))
    info = dataflow.schedule_info(cfg)
    assert info["for_this_config"] and info["next_run"] is None and not info["other_config"]
    assert "Günlük yenileme kurulu değil" not in client_for(cfg).get("/connections").text


def test_a_scheduled_run_that_failed_before_recording_is_shown(cfg, monkeypatch, tmp_path):
    """A config error in the 07:30 run leaves no run record; the UI reads the job's log."""
    heartbeat(cfg)
    monkeypatch.setattr(dataflow.schedule, "backend", lambda *a, **k: FakeBackend(True))
    log = tmp_path / "pulse.log"
    stamp = (dt.datetime.now().astimezone() - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    log.write_text(
        "=== 2026-01-01T07:30:00+03:00 pulse run --config x (pid 1)\nok\n=== exit 0\n"
        f"=== {stamp} pulse run --config {cfg.path} (pid 2)\n"
        "config error: environment variable PULSE_X is not set <script>alert(1)</script>\n"
        "=== exit 2\n",
        encoding="utf-8",
    )
    record(cfg.path, log_file=log)
    html = client_for(cfg).get("/connections").text
    assert "çıkış kodu 2" in html and "PULSE_X is not set" in html
    assert "<script>alert(1)</script>" not in html  # file content is escaped, never markup
    assert "&lt;script&gt;" in html
    assert "çıkış kodu 2" in client_for(cfg).get("/health").text
    # once the run itself records a result, that record is what the page shows
    runstate._append_history(cfg.state_dir, {"start": runstate.now_iso(), "status": "success"})
    assert "çıkış kodu 2" not in client_for(cfg).get("/connections").text


def test_scheduled_failure_parser_cases(tmp_path):
    assert dataflow.scheduled_failure(None, []) is None
    assert dataflow.scheduled_failure(tmp_path / "missing.log", []) is None
    log = tmp_path / "p.log"
    log.write_text(
        "=== 2026-01-02T07:30:00+00:00 pulse run --config c (pid 9)\nworking\n", encoding="utf-8"
    )
    assert dataflow.scheduled_failure(log, []) is None  # still running: no exit line yet
    log.write_text(
        "=== 2026-01-02T07:30:00+00:00 pulse run --config c (pid 9)\nlocked: another\n=== exit 75\n",
        encoding="utf-8",
    )
    got = dataflow.scheduled_failure(log, [])
    assert got["locked"] and got["rc"] == 75


def test_failed_sources_say_why_and_what_to_do(cfg):
    heartbeat(
        cfg,
        status="failed",
        error="1 source(s) failed to load: mossy_match.meta_ads",
        summary={"source_errors": {"mossy_match.meta_ads": "MissingCredential: meta_ads: set PULSE_META_ACCESS_TOKEN <b>x</b>"}},
    )  # fmt: skip
    c = client_for(cfg)
    html = c.get("/connections").text
    assert "PULSE_META_ACCESS_TOKEN" in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert "Şu ortam değişkenini ekleyin: PULSE_META_ACCESS_TOKEN" in html
    health = c.get("/health").text
    assert "Son yenilemede yüklenemeyen kaynaklar" in health and "mossy_match.meta_ads" in health
    assert "1 kaynak yüklenemedi: mossy_match.meta_ads" in health  # the run error in Turkish
    # every page says the last refresh failed and links to the flow view
    studio = c.get("/studio").text
    assert "Son yenileme başarısız" in studio and 'href="/connections"' in studio


def test_pipeline_keeps_source_errors_short_and_masked():
    long = "x" * 1000
    out = pipeline.source_errors(
        {"a.meta_ads": "HTTPError: 401 for access_token=EAAB1234567890abcdefXYZ", "b.ga4": long}
    )
    assert "EAAB1234567890abcdefXYZ" not in out["a.meta_ads"]
    assert len(out["b.ga4"]) == 400 and out["b.ga4"].endswith("…")


def test_stale_data_is_flagged_on_every_page(cfg):
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=40)).replace(microsecond=0).isoformat()
    heartbeat(cfg, last_success=old, last_start=old, last_end=old)
    c = client_for(cfg)
    for path in ("/studio", "/studio/mossy_match", "/alerts", "/settings"):
        html = c.get(path).text
        assert "Son başarılı yenileme" in html, path
        assert 'class="fresh stale"' in html, path
    html = c.get("/connections").text
    assert "veriler güncel değil" in html


def test_fresh_data_shows_last_refresh_without_a_warning(cfg):
    heartbeat(cfg)
    html = client_for(cfg).get("/studio").text
    assert 'class="fresh ok"' in html and "Son yenileme 5 dk önce" in html
    assert "Veri sonu" in html  # the Today header names its date
    assert "Son başarılı yenileme" not in html


# -- the studio's own public store pages --------------------------------------------------------


def _radar_tables(cfg, rows, watch):
    from studio_pulse.radar import store
    from studio_pulse.radar.estimate import stage

    con = warehouse.connect(cfg.warehouse)
    try:
        store.init_schema(con)
        stage.init_schema(con)
        store.insert(con, "raw_mr_app", rows)
        store.insert(con, "raw_mr_watch", watch)
        con.execute("CHECKPOINT")
    finally:
        con.close()


def test_app_page_shows_the_watched_store_listing(cfg):
    cfg.raw["radar"] = {"enabled": True}
    day = cfg.as_of
    base = {"snapshot_date": day, "fetched_at": dt.datetime(2026, 11, 1, 6), "store": "apple",
            "app_id": "9000000001", "bundle_id": "demo.mossymatch", "name": "Mossy <i>Match</i>",
            "version": "2.3.0", "current_version_date": dt.date(2026, 10, 20), "source": "watch",
            "store_url": "javascript:alert(1)"}  # fmt: skip
    rows = [
        {**base, "country": "us", "rating_count": 1, "rating_avg": 1.0},
        {**base, "country": "tr", "rating_count": 4, "rating_avg": 5.0},
        {**base, "store": "play", "app_id": "demo.mossymatch", "country": "us",
         "rating_count": 30, "rating_avg": 4.5, "installs_bucket": "1,000+",
         "version": "Varies with device", "store_url": "https://play.google.com/store/apps/details?id=demo.mossymatch",
         "source": "play_details"},
    ]  # fmt: skip
    _radar_tables(
        cfg,
        rows,
        [{"store": "apple", "app_id": "9000000001", "added_at": dt.datetime(2026, 10, 1)}],
    )
    c = client_for(cfg)
    html = c.get("/studio/mossy_match").text
    assert "Herkese açık mağaza sayfası" in html
    assert "4,20" in html  # (1 x 1.0 + 4 x 5.0) / 5 over the storefronts read that day
    assert "2.3.0" in html and "cihaza göre değişir" in html and "1.000+" in html
    assert "2 ülke" in html and "listelerde yok" in html
    assert 'href="javascript:alert(1)"' not in html  # only https store links become links
    assert 'href="https://play.google.com/store/apps/details?id=demo.mossymatch"' in html
    assert 'href="/radar/oyun/9000000001"' in html
    # the flow view counts the listing as connected, with its last fetch
    flow = c.get("/connections").text
    assert "Herkese açık mağaza sayfası (izleme listesi)" in flow
    assert "&lt;i&gt;" not in flow  # names are not echoed into this table at all


def test_app_page_tells_the_one_step_when_not_watched(cfg):
    c = client_for(cfg)
    html = c.get("/studio/mossy_match").text
    assert "radar.enabled: true" in html  # no radar block in the demo config
    cfg.raw["radar"] = {"enabled": True}
    html = client_for(cfg).get("/studio/mossy_match").text
    assert "pulse radar watch add demo.mossymatch" in html


def test_radar_page_links_only_games_it_has_pages_for(cfg, demo_build):
    import shutil

    shutil.copytree(demo_build.work / "radar", cfg.warehouse.parent / "radar")
    html = client_for(cfg).get("/radar").text
    assert "Teacup Tumble" in html  # the demo facts file
    assert 'href="/radar/oyun/' not in html  # the demo warehouse has no radar tables: no 404 links
    _radar_tables(cfg, [{"snapshot_date": cfg.as_of, "store": "apple", "country": "us",
                         "app_id": "1", "name": "Teacup Tumble"}], [])  # fmt: skip
    html = client_for(cfg).get("/radar").text
    assert 'href="/radar/oyun/1"' in html and 'href="/radar/oyun/2"' not in html
    assert client_for(cfg).get("/radar/oyun/1").status_code == 200


# -- wording and number formats -------------------------------------------------------------------


def test_check_and_run_messages_in_turkish():
    assert dataflow.check_message("3 failing row(s)", "tr") == "3 satır kuralı bozuyor"
    assert dataflow.check_message("3 failing row(s)", "en") == "3 failing row(s)"
    msg = "1 source(s) stale: z/crash data through 2026-11-07 (8 d, limit 3)"
    assert dataflow.check_message(msg, "tr") == (
        "1 kaynak bayat: z/crash veri sonu 2026-11-07 (8 gün, sınır 3)"
    )
    assert dataflow.check_message("something new", "tr") == "something new"
    err = "1 source(s) failed to load: a.b; 2 data-quality check(s) failed: x, y"
    assert dataflow.run_error(err, "tr") == (
        "1 kaynak yüklenemedi: a.b; 2 veri kalitesi denetimi başarısız: x, y"
    )


def test_durations_numbers_and_signs():
    assert dataflow.duration(329.35, "tr") == "5 dk 29 sn"
    assert dataflow.duration(5.24, "en") == "5 s"
    assert dataflow.num(12345, "tr") == "12.345" and dataflow.num(12345, "en") == "12,345"
    assert studio_metrics._signed(-0.0002, "tr") == "%0,0"  # never "−%0,0"
    assert studio_metrics._signed(-0.022, "tr") == "−%2,2"
    assert dataflow.launchd_state("not running", "tr") == "beklemede"


def test_alert_titles_wrap_on_a_phone(cfg):
    html = client_for(cfg).get("/alerts").text
    assert ".finding .ttl { flex-wrap: wrap;" in html  # long rule ids no longer push the page wide
    assert "Quartz Merge ·" in html  # app names, not ids


def test_connections_needs_no_writer_and_survives_an_empty_warehouse(tmp_path):
    from studio_pulse.config import load_config

    (tmp_path / "pulse.yaml").write_text(
        "title: t\nwarehouse: wh.duckdb\napps:\n  - id: one\n    name: One\n    sources:\n"
        "      applovin_ads: {package_names: [com.example.one]}\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "pulse.yaml")
    html = client_for(cfg).get("/connections").text
    assert "PULSE_APPLOVIN_REPORT_KEY" in html  # a configured source without its key
    assert "Henüz yenileme yok" in html


def test_static_studio_has_no_freshness_or_flow_links(demo_build, tmp_path):
    from studio_pulse.serve.studio import export_static

    cfg = demo_build.copy(tmp_path / "static")
    pages = export_static(cfg, tmp_path / "out", live=json.loads(json.dumps(SNAP)))
    text = Path(pages[0]).read_text(encoding="utf-8")
    assert 'class="fresh' not in text and 'href="/connections"' not in text
