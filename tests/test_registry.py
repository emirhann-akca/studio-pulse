"""Zero-setup registry: status machine, store listing checks (recorded), config merge, GeoIP."""

from __future__ import annotations

import gzip
import hashlib
import time
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES

from studio_pulse import cli
from studio_pulse.collector import geo
from studio_pulse.collector.registry import StoreChecker, decide, read_registry, refresh, slug
from studio_pulse.collector.store import LocalStore
from studio_pulse.config import load_config
from studio_pulse.connectors.replay import ReplaySession

STORES = FIXTURES / "stores"
NOW = 1_793_000_000_000


def test_status_machine_only_moves_forward():
    kw = {"prod_installs_24h": 0, "min_installs": 20, "store_listed": None}
    assert decide("new", seen_release=False, seen_test_store=False, **kw) == "new"
    assert decide("new", seen_release=True, seen_test_store=False, **kw) == "testing"
    assert decide("new", seen_release=False, seen_test_store=True, **kw) == "testing"
    live = {"prod_installs_24h": 25, "min_installs": 20}
    assert (
        decide("testing", seen_release=True, seen_test_store=False, store_listed=True, **live)
        == "live"
    )
    # installs alone (a closed Play track also reports the Play installer) are not enough
    assert (
        decide("testing", seen_release=True, seen_test_store=False, store_listed=False, **live)
        == "testing"
    )
    assert (
        decide("testing", seen_release=True, seen_test_store=False, store_listed=None, **live)
        == "testing"
    )
    assert decide("live", seen_release=False, seen_test_store=False, **kw) == "live"


def checker(sleeps: list[float] | None = None) -> StoreChecker:
    t = [0.0]
    return StoreChecker(
        ReplaySession(STORES),
        min_interval_s=3.0,
        sleep=lambda s: (
            sleeps.append(s) if sleeps is not None else None,
            t.__setitem__(0, t[0] + s),
        ),
        clock=lambda: t[0],
    )


def test_store_checks_from_recorded_responses():
    sleeps: list[float] = []
    c = checker(sleeps)
    ios = c.check("com.supercell.magic", "ios")
    assert ios.listed is True and ios.name == "Clash of Clans" and ios.version == "18.647.5"
    assert ios.url.startswith("https://apps.apple.com/")
    assert c.check("com.example.studiopulse.notarealapp", "ios").listed is False
    play = c.check("com.supercell.clashofclans", "android")
    assert play.listed is True and play.name == "Clash of Clans"
    assert c.check("com.example.studiopulse.notarealapp", "android").listed is False
    assert c.check("anything", "editor").listed is None
    # polite: one request every 3 s at most (20 per minute)
    assert c.requests == 4 and sleeps == [3.0, 3.0, 3.0]
    q = c.session.recorded[0].query
    assert q == {"bundleId": "com.supercell.magic", "country": "us"}


def test_network_trouble_is_unknown_not_unlisted():
    class Broken:
        def get(self, *a, **k):
            raise ConnectionError("offline")

    assert StoreChecker(Broken(), min_interval_s=0).check("x.y", "ios").listed is None


def _seed(
    data_dir: Path, app: str, platform: str, prod: int, *, release: bool = True, now: int = NOW
) -> None:
    s = LocalStore(data_dir)
    s.register_app(app, platform, now, name=None, genre="game", version="1.0")
    s.touch_app(app, platform, now, version="1.0", release=release, test_store=False,
                store="google_play", genre="game", name=None)  # fmt: skip
    s.add_prod_installs([(app, platform, f"i{i}") for i in range(prod)], now)
    s.close()


def test_refresh_promotes_to_live_with_installs_and_a_listing(tmp_path):
    d = tmp_path / "c"
    _seed(d, "com.supercell.clashofclans", "android", 25)
    _seed(d, "com.example.studiopulse.notarealapp", "android", 25)
    _seed(d, "com.small.game", "android", 3)
    c = checker()
    rows = {
        r["app_id"]: r
        for r in refresh(d, min_installs=20, checker=c, now_ms=NOW + 1000, log=lambda m: None)
    }
    assert rows["com.supercell.clashofclans"]["status"] == "live"
    assert rows["com.supercell.clashofclans"]["display_name"] == "Clash of Clans"
    assert rows["com.example.studiopulse.notarealapp"]["status"] == "testing"
    assert rows["com.example.studiopulse.notarealapp"]["store_listed"] == 0
    assert (
        rows["com.small.game"]["status"] == "testing"
        and rows["com.small.game"]["store_checked_ms"] is None
    )
    assert c.requests == 2
    # a result is reused for 24 h: no new request
    refresh(d, min_installs=20, checker=c, now_ms=NOW + 3_600_000, log=lambda m: None)
    assert c.requests == 2
    # installs older than 24 h do not count
    rows = {
        r["app_id"]: r
        for r in refresh(
            d, min_installs=20, checker=c, now_ms=NOW + 2 * 86_400_000, log=lambda m: None
        )
    }
    assert rows["com.example.studiopulse.notarealapp"]["prod_installs_24h"] == 0


def test_apps_refresh_cli_with_replay(tmp_path, capsys):
    d = tmp_path / "c"
    _seed(d, "com.supercell.magic", "ios", 30, now=time.time_ns() // 1_000_000)
    rc = cli.main(
        ["apps", "refresh", "--data-dir", str(d), "--replay", str(STORES), "--min-installs", "5"]
    )
    assert rc == 0
    assert "live" in capsys.readouterr().out
    assert read_registry(d)[0]["status"] == "live"
    assert cli.main(["apps", "list", "--data-dir", str(d), "--json"]) == 0


def test_registry_apps_join_the_config_without_an_edit(tmp_path):
    d = tmp_path / "collector"
    _seed(d, "com.studio.newgame", "android", 0)
    _seed(d, "com.studio.newgame", "ios", 0)
    _seed(d, "com.studio.claimed", "android", 0)
    _seed(d, "com.studio.debugonly", "android", 0, release=False)
    cfg = {
        "warehouse": "w.duckdb",
        "collector": {"data_dir": "collector"},
        "apps": [
            {"id": "claimed", "sources": {"pulse_sdk": {"bundle_ids": ["com.studio.claimed"]}}}
        ],
    }
    p = tmp_path / "pulse.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    c = load_config(p)
    ids = [a.id for a in c.apps]
    assert ids == ["claimed", "com_studio_debugonly", "com_studio_newgame"]
    new = c.app("com_studio_newgame")
    assert new.platforms == ["ANDROID", "IOS"] and new.genre == "game"
    assert new.sources == {"pulse_sdk": {"bundle_ids": ["com.studio.newgame"], "auto": True}}
    # only apps at a status: hide developer-only builds
    cfg["collector"]["dashboard_min_status"] = "testing"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert "com_studio_debugonly" not in [a.id for a in load_config(p).apps]
    # a config that only has a collector block is valid
    p.write_text(yaml.safe_dump({"collector": {"data_dir": "collector"}}), encoding="utf-8")
    assert len(load_config(p).apps) == 3
    assert slug("Com.Studio-New.Game") == "com_studio_new_game"


# -- GeoIP -----------------------------------------------------------------------------


def test_country_from_header_or_database_and_never_private_addresses():
    class Reader:
        def get(self, ip):
            return {"country": {"iso_code": "TR"}}

    r = geo.CountryResolver(header="CF-IPCountry", reader=Reader())
    assert r.country({"cf-ipcountry": "de"}, "8.8.8.8") == "DE"
    assert r.country({"cf-ipcountry": "XX"}, "8.8.8.8") == "TR"  # Cloudflare's unknown
    assert r.country({}, "192.168.1.5") == "unknown"
    assert geo.CountryResolver().country({"cf-ipcountry": "DE"}, "8.8.8.8") == "unknown"


def test_setup_geoip_verifies_the_published_sha1(tmp_path):
    page = (STORES / "dbip_download_page.html").read_text(encoding="utf-8")
    url, sha = geo.parse_download_page(page)
    assert url.endswith("dbip-country-lite-2026-11.mmdb.gz")
    assert (
        sha == "09eea8433fe3eed71984fd6aac9d8ab3d9ca51f0"
    )  # SHA1 of the uncompressed file (verified)
    payload = b"fake mmdb bytes"
    good_sha = hashlib.sha1(payload).hexdigest()  # noqa: S324
    fetched: list[str] = []

    def fetch(u: str) -> bytes:
        fetched.append(u)
        return (
            page.replace(sha, good_sha).encode() if u == geo.DBIP_PAGE else gzip.compress(payload)
        )

    out = geo.setup_geoip(tmp_path / "geoip", fetch=fetch, verify_open=False, log=lambda m: None)
    assert out.read_bytes() == payload and fetched == [geo.DBIP_PAGE, url]
    attribution = (tmp_path / "geoip" / "ATTRIBUTION.txt").read_text(encoding="utf-8")
    assert "DB-IP" in attribution and "Creative Commons Attribution 4.0" in attribution
    with pytest.raises(geo.GeoError, match="checksum"):
        geo.setup_geoip(tmp_path / "g2", fetch=lambda u: gzip.compress(b"tampered"), url=url,
                        sha1=good_sha, verify_open=False, log=lambda m: None)  # fmt: skip
    assert not (tmp_path / "g2" / "dbip-country-lite.mmdb").exists()


def test_real_mmdb_when_available():
    """Opt-in: PULSE_TEST_MMDB=/path/to/dbip-country-lite.mmdb (never downloaded by tests)."""
    import os

    path = os.environ.get("PULSE_TEST_MMDB")
    if not path:
        pytest.skip("set PULSE_TEST_MMDB to check a real DB-IP database")
    r = geo.CountryResolver(Path(path))
    assert r.country({}, "8.8.8.8") == "US" and r.country({}, "127.0.0.1") == "unknown"
