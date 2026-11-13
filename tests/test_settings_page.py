"""/settings: connect Google Play and the App Store without editing YAML; the daily refresh.

No network and no real scheduler here: storage, App Store Connect and the launchd/Task
Scheduler backend are fakes."""

from __future__ import annotations

import datetime as dt
import gzip
import os
import stat
import sys

import pytest
import yaml
from fastapi.testclient import TestClient
from test_live_page import HOST, SNAP  # tests/ is on sys.path (no package)

from studio_pulse.config import SETTINGS_ENV, SETTINGS_FILE, apply_settings, load_config
from studio_pulse.connectors import revenuecat, stores
from studio_pulse.serve import settings_page
from studio_pulse.serve.app import create_app

TOKEN = "tok"


@pytest.fixture()
def cfg(demo_build, tmp_path):
    return demo_build.copy(tmp_path / "set")


@pytest.fixture()
def client(cfg):
    return TestClient(create_app(cfg, token=TOKEN, live_fetcher=lambda: SNAP), base_url=HOST)


def post(client, path, data, token=TOKEN):
    return client.post(path, data=data, headers={"X-Pulse-Token": token})


def test_settings_page_lists_sources_and_forms_in_turkish(client, cfg):
    html = client.get("/settings").text
    assert "Google Play&#39;i bağla" in html and "App Store&#39;u bağla" in html
    assert "Günlük yenileme" in html and "PULSE_ASC_KEY_ID" in html
    for a in cfg.apps:
        assert f'id="app-{a.id}"' in html
    assert "Google Play raporları" in html and "veri geliyor" in html  # the demo's play_console
    assert 'href="/settings" aria-current="page"' in html  # in the sidebar
    assert "Connect Google Play" in client.get("/settings", params={"lang": "en"}).text


def test_writes_need_the_token(client):
    r = post(
        client,
        "/settings/play/mossy_match",
        {"bucket": "pubsite_prod_rev_123456", "package": "a.b"},
        token="",
    )
    assert r.status_code == 403
    r = post(client, "/settings/schedule", {"at": "08:30"}, token="wrong")
    assert r.status_code == 403


def test_connect_google_play_writes_the_overlay_not_the_config(client, cfg):
    before = cfg.path.read_text(encoding="utf-8")
    r = post(client, "/settings/play/mossy_match", {"bucket": "nope", "package": "com.x.y"})
    assert "pubsite_prod_rev_" in r.text and not (cfg.base_dir / SETTINGS_FILE).exists()
    r = post(
        client,
        "/settings/play/mossy_match",
        {"bucket": "gs://pubsite_prod_rev_01234567890/", "package": "bad name"},
    )
    assert "com.ornek.oyun" in r.text
    r = post(client, "/settings/play/mossy_match",
             {"bucket": "gs://pubsite_prod_rev_01234567890/stats/", "package": "com.example.mossy"})  # fmt: skip
    assert r.status_code == 200 and "Kaydedildi" in r.text
    assert cfg.path.read_text(encoding="utf-8") == before  # pulse.yaml untouched
    over = yaml.safe_load((cfg.base_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
    src = over["apps"]["mossy_match"]["sources"]
    assert src["play_console"] == {
        "path": "gs://pubsite_prod_rev_01234567890/stats/",
        "package": "com.example.mossy",
    }
    assert src["play_reviews"]["path"] == "gs://pubsite_prod_rev_01234567890/reviews/"
    reloaded = load_config(cfg.path)
    mossy = next(a for a in reloaded.apps if a.id == "mossy_match")
    assert mossy.sources["play_console"]["path"].startswith("gs://pubsite_prod_rev_01234567890")
    page = client.get("/settings").text
    assert 'value="pubsite_prod_rev_01234567890"' in page and "Ayarlar&#39;dan" in page


def test_connect_app_store_keeps_keys_out_of_pages_and_config(client, cfg, tmp_path):
    r = post(client, "/settings/appstore/mossy_match", {"apple_id": "12a", "vendor": "123456"})
    assert "yalnız rakamlardan" in r.text
    keyfile = tmp_path / "AuthKey_ABCDE12345.p8"
    keyfile.write_text("not a real key", encoding="utf-8")
    form = {"apple_id": "1234567890", "vendor": "87654321", "key_id": "ABCDE12345",
            "issuer_id": "0a1b2c3d-1111-2222-3333-444455556666", "key_path": str(tmp_path / "missing.p8")}  # fmt: skip
    assert ".p8" in post(client, "/settings/appstore/mossy_match", form).text
    form["key_path"] = str(keyfile)
    for k in ("PULSE_ASC_KEY_ID", "PULSE_ASC_ISSUER_ID", "PULSE_ASC_PRIVATE_KEY_PATH"):
        os.environ.pop(k, None)
    try:
        r = post(client, "/settings/appstore/mossy_match", form)
        assert "Kaydedildi" in r.text
        env = cfg.base_dir / SETTINGS_ENV
        text = env.read_text(encoding="utf-8")
        assert "PULSE_ASC_ISSUER_ID=0a1b2c3d-1111-2222-3333-444455556666" in text
        assert "not a real key" not in text  # the path is stored, never the key
        if sys.platform != "win32":
            assert stat.S_IMODE(env.stat().st_mode) == 0o600
        over = yaml.safe_load((cfg.base_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
        assert over["apps"]["mossy_match"]["sources"]["app_store"] == {
            "vendor_number": "87654321",
            "apple_id": "1234567890",
        }
        page = client.get("/settings").text
        assert "0a1b2c3d-1111" not in page and "ABCDE12345" not in page  # values never shown
        assert page.count(">ayarlı<") == 3
    finally:
        for k in ("PULSE_ASC_KEY_ID", "PULSE_ASC_ISSUER_ID", "PULSE_ASC_PRIVATE_KEY_PATH"):
            os.environ.pop(k, None)


def test_test_buttons_report_in_plain_language(client, monkeypatch):
    assert "Önce bu kaynağı kaydedin" in post(client, "/settings/test/pacer_pal/play", {}).text
    post(
        client,
        "/settings/play/pacer_pal",
        {"bucket": "pubsite_prod_rev_0123456", "package": "com.x.pacer"},
    )
    monkeypatch.setattr(settings_page, "test_play", lambda b, p: (True, "4"))
    assert "4 kurulum raporu" in post(client, "/settings/test/pacer_pal/play", {}).text
    monkeypatch.setattr(settings_page, "test_play", lambda b, p: (False, "0"))
    assert "paket adını kontrol edin" in post(client, "/settings/test/pacer_pal/play", {}).text
    monkeypatch.setattr(settings_page, "test_play", lambda b, p: (False, "Forbidden: 403"))
    assert "Bağlanamadı: Forbidden: 403" in post(client, "/settings/test/pacer_pal/play", {}).text


def test_schedule_button_installs_the_daily_refresh(client, cfg, monkeypatch):
    installed = []

    class Fake:
        kind = "fake"

        def install(self, job):
            installed.append(job)

        def status(self, name):
            return {"registered": bool(installed)}

    monkeypatch.setattr(settings_page.schedule, "backend", lambda *a, **k: Fake())
    monkeypatch.setattr(settings_page.schedule, "save_record", lambda job, kind: None)
    assert "HH:MM" in post(client, "/settings/schedule", {"at": "25:00"}).text
    r = post(client, "/settings/schedule", {"at": "07:15"})
    assert "her gün 07:15" in r.text
    job = installed[0]
    assert (job.hour, job.minute) == (7, 15) and job.args[:3] == ["run", "--config", str(cfg.path)]


def test_overlay_merge_adds_replaces_and_removes(tmp_path):
    (tmp_path / SETTINGS_FILE).write_text(
        "apps:\n  a:\n    sources:\n      play_console: {path: gs://x/stats/, package: p.q}\n      ga4_bigquery: null\n",
        encoding="utf-8",
    )
    data = {
        "apps": [
            {"id": "a", "sources": {"ga4_bigquery": {"dataset": "d"}, "admob": {}}},
            {"id": "b"},
        ]
    }
    out = apply_settings(data, tmp_path)
    assert out["apps"][0]["sources"] == {
        "admob": {},
        "play_console": {"path": "gs://x/stats/", "package": "p.q"},
    }
    assert "sources" not in out["apps"][1]


def test_play_bucket_sync_keeps_only_this_package(tmp_path):
    class Blob:
        def __init__(self, name):
            self.name, self.size = name, 3

        def download_to_filename(self, target):
            open(target, "w").write("abc")

    class Client:
        def list_blobs(self, bucket, prefix=None):
            assert (bucket, prefix) == ("pubsite_prod_rev_1", "stats/")
            return [Blob("stats/installs/installs_com.a.game_202611_overview.csv"),
                    Blob("stats/installs/installs_com.other_202611_overview.csv"),
                    Blob("stats/installs/installs_com.a.game_202611_country.csv")]  # fmt: skip

    root = revenuecat.sync_gcs("gs://pubsite_prod_rev_1/stats/", tmp_path / "c", client=Client(),
                               suffixes=("overview.csv",), contains="com.a.game", log=lambda m: None)  # fmt: skip
    files = sorted(p.name for p in root.rglob("*.csv"))
    assert files == ["installs_com.a.game_202611_overview.csv"]


class Resp:
    def __init__(self, status, content=b"", body=None):
        self.status_code, self.content, self._body = status, content, body

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def test_app_store_sales_are_fetched_once_per_day(tmp_path):
    calls = []
    report = gzip.compress(b"Provider\tSKU\n")

    class Session:
        def get(self, url, params, headers, timeout):
            calls.append(params["filter[reportDate]"])
            assert headers["Authorization"] == "Bearer T" and params["filter[version]"] == "1_0"
            day = params["filter[reportDate]"]
            return Resp(404) if day in ("2026-11-02", "2026-11-11") else Resp(200, report)

    today = dt.date(2026, 11, 13)
    n = stores.fetch_appstore_sales(tmp_path, "8765", dt.date(2026, 11, 1), dt.date(2026, 11, 20),
                                    token=lambda: "T", session=Session(), today=today, log=lambda m: None)  # fmt: skip
    assert n == 10 and len(calls) == 12  # 1..12 Nov: up to yesterday only
    assert (tmp_path / "S_D_8765_20261101.txt.gz").read_bytes() == report
    assert (tmp_path / "S_D_8765_20261102.none").exists()  # old enough: no report that day
    assert not (tmp_path / "S_D_8765_20261111.none").exists()  # too recent: ask again later
    calls.clear()
    stores.fetch_appstore_sales(tmp_path, "8765", dt.date(2026, 11, 1), dt.date(2026, 11, 12),
                                token=lambda: "T", session=Session(), today=today, log=lambda m: None)  # fmt: skip
    assert calls == ["2026-11-11"]


def test_app_store_errors_name_the_day_not_the_token(tmp_path):
    class Session:
        def get(self, url, params, headers, timeout):
            return Resp(
                401, body={"errors": [{"detail": "Authentication credentials are missing"}]}
            )

    with pytest.raises(RuntimeError) as e:
        stores.fetch_appstore_sales(tmp_path, "1", dt.date(2026, 11, 1), dt.date(2026, 11, 1),
                                    token=lambda: "SECRET", session=Session(),
                                    today=dt.date(2026, 11, 13), log=lambda m: None)  # fmt: skip
    assert "2026-11-01" in str(e.value) and "401" in str(e.value) and "SECRET" not in str(e.value)
