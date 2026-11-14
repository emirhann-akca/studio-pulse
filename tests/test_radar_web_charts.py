"""The radar's default Apple chart source: apps.apple.com chart pages (robots-allowed), parsed
from a recorded page; the legacy RSS (robots-disallowed) only in a replay."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from studio_pulse import warehouse
from studio_pulse.config import ConfigError, config_from_dict
from studio_pulse.connectors.replay import ReplaySession
from studio_pulse.radar import charts_web, store
from studio_pulse.radar import run as radar_run
from studio_pulse.radar.http import Fetcher, RobotsDisallowed
from studio_pulse.radar.settings import from_config

WEB = Path(__file__).parent / "fixtures" / "radar" / "web"
DAY = dt.date(2026, 11, 14)
ENTRIES = [
    {"path": "/robots.txt", "file": "robots_apps.txt", "content_type": "text/plain"},
    {"path": "/us/iphone/charts/7012", "file": "us_7012.html", "content_type": "text/html"},
]


@pytest.fixture()
def con():
    c = warehouse.connect(":memory:")
    store.init_schema(c)
    yield c
    c.close()


def _settings(**radar):
    cfg = config_from_dict({"apps": [{"id": "x"}], "radar": radar}, base_dir=Path("."))
    return from_config(cfg)


class Res:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.counts: dict = {}


def test_parse_page_gives_both_lists_to_rank_200():
    lists = charts_web.parse_page((WEB / "us_7012.html").read_text(encoding="utf-8"))
    free, paid = lists["top-free"], lists["top-paid"]
    assert len(free) == 200 and [r["rank"] for r in free] == list(range(1, 201))
    # 7 of the 200 paid positions are app bundles: their ranks stay empty, not renumbered
    assert len(paid) == 193 and paid[-1]["rank"] <= 200 and len({r["rank"] for r in paid}) == 193
    assert [r["rank"] for r in free[:3]] == [1, 2, 3]
    assert free[0] == {
        "app_id": "6761760135",
        "name": "Meowdoku!",
        "bundle_id": "com.oakever.meowdoku",
        "rank": 1,
    }
    assert free[24]["name"] and free[25]["name"] is None  # ranks 26+ are ids only
    assert len({r["app_id"] for r in free}) == 200
    with pytest.raises(charts_web.ChartPageError):
        charts_web.parse_page("<html><body>no data</body></html>")


def test_stage_reads_one_robots_checked_page_per_genre(con):
    s = _settings(
        countries=["us"],
        charts=[
            {"chart": "topfreeapplications", "genre": "7012"},
            {"chart": "toppaidapplications", "genre": "7012", "limit": 50},
            {"chart": "topgrossingapplications", "genre": "6014"},
        ],
    )
    sess = ReplaySession(WEB, entries=ENTRIES)
    f = Fetcher(
        con,
        DAY,
        session=sess,
        run_id="t",
        clock=lambda: 0.0,
        sleep=lambda x: None,
        robots_hosts=("play.google.com", charts_web.HOST),
        budgets={charts_web.HOST: s.apple_web_budget},
    )
    res = Res()
    charts_web.stage(con, f, s, DAY, res, lambda m: None)
    assert [r.path for r in sess.recorded] == ["/robots.txt", "/us/iphone/charts/7012"]
    rows = dict(
        con.execute(
            "SELECT chart, COUNT(*) FROM raw_mr_chart WHERE snapshot_date = ? GROUP BY 1", [DAY]
        ).fetchall()
    )
    assert rows == {"topfreeapplications": 100, "toppaidapplications": 47}  # 3 bundles in 1-50
    assert any("topgrossingapplications:6014" in n for n in res.notes)  # no grossing on the web
    src = con.execute("SELECT DISTINCT source_url FROM raw_mr_chart").fetchall()
    assert src == [("https://apps.apple.com/us/iphone/charts/7012",)]
    ledger = con.execute("SELECT host, purpose FROM raw_mr_request ORDER BY ts").fetchall()
    assert ("apps.apple.com", "web_chart:us:7012") in ledger
    # robots.txt of apps.apple.com disallows the page's own continuation API
    with pytest.raises(RobotsDisallowed):
        f.get("https://apps.apple.com/v1/catalog/us/apps/6771681639?l=en-US", "t")


def test_names_of_ranks_beyond_25_come_from_the_lookup(con):
    store.insert(
        con,
        "raw_mr_chart",
        [
            {
                "snapshot_date": DAY,
                "store": "apple",
                "country": "us",
                "chart": "topfreeapplications",
                "genre_id": "7012",
                "requested_limit": 100,
                "rank": 30,
                "app_id": "6771681639",
                "source_url": "https://apps.apple.com/us/iphone/charts/7012",
            }
        ],
    )
    store.insert(
        con,
        "raw_mr_app",
        [
            {
                "snapshot_date": DAY,
                "store": "apple",
                "country": "us",
                "app_id": "6771681639",
                "name": "Some Puzzle",
                "bundle_id": "com.example.somepuzzle",
                "developer": "Example Studio",
                "source": "lookup",
            }
        ],
    )
    assert charts_web.fill_from_lookup(con, DAY) == 1
    assert con.execute("SELECT name, bundle_id, developer FROM raw_mr_chart").fetchone() == (
        "Some Puzzle",
        "com.example.somepuzzle",
        "Example Studio",
    )


def test_the_legacy_rss_is_refused_outside_a_replay(tmp_path):
    class NoNetwork:
        def get(self, *a, **k):
            raise AssertionError("a request was sent")

    cfg = config_from_dict(
        {
            "apps": [{"id": "x"}],
            "warehouse": "w.duckdb",
            "radar": {"countries": ["us"], "apple": {"chart_source": "rss"}, "out_dir": "o"},
        },
        base_dir=tmp_path,
    )
    with pytest.raises(ConfigError, match="replay"):
        radar_run.run_radar(cfg, session=NoNetwork(), use_llm=False, log=lambda m: None)
    with pytest.raises(ConfigError, match="web or rss"):
        _settings(apple={"chart_source": "itunes"})
