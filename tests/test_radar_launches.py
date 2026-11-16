"""New launches: icon URLs, the curated publisher preset, fct_mr_launches, the digest section,
the local page /radar/yeni and the shareable deck (`pulse radar launches --share`).

Everything runs on invented data (tests/radar_synth.py build_launches): nothing is fetched,
and the PDF step never starts a real browser."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml
from radar_synth import LAUNCH_DAY

from studio_pulse import warehouse
from studio_pulse.config import ConfigError, config_from_dict
from studio_pulse.radar import apple, publishers, store
from studio_pulse.radar.settings import from_config

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
