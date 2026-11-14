"""Radar industry news: RSS / Atom parsing, robots, conditional GET, redirects, size cap,
dedupe, LLM and keyword tags, the CLI and the radar page. Every answer comes from fixture
feeds (tests/fixtures/radar/news) through a scripted session; nothing leaves the machine."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from studio_pulse import cli, warehouse
from studio_pulse.config import ConfigError, config_from_dict
from studio_pulse.llm import FakeProvider, LLMClient, TransientLLMError
from studio_pulse.radar import news, store
from studio_pulse.radar.http import STORE_HOSTS

FIX = Path(__file__).parent / "fixtures" / "radar" / "news"
DAY = dt.date(2026, 11, 15)
NOW = dt.datetime(2026, 11, 15, 8, 0, 0)
RSS = (FIX / "rss.xml").read_bytes()
ATOM = (FIX / "atom.xml").read_bytes()
ROBOTS_OK = (FIX / "robots_allow.txt").read_bytes()
ROBOTS_NO = (FIX / "robots_disallow.txt").read_bytes()
GAMIGION, MOBILEGAMER, NAAVIK = (news.FEEDS[i] for i in (0, 1, 3))
NOSLEEP = lambda _s: None  # noqa: E731
NOLOG = lambda _m: None  # noqa: E731


class Resp:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.status_code = status
        self.body = body
        self.headers = dict(headers or {})
        self.closed = False

    def iter_content(self, size: int):
        for i in range(0, len(self.body), size):
            yield self.body[i : i + size]

    def close(self) -> None:
        self.closed = True


class Session:
    """Answers by URL (a list is used in order); records every request."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = {k: (list(v) if isinstance(v, list) else v) for k, v in answers.items()}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True, stream=False):
        assert allow_redirects is False and stream is True and timeout
        self.calls.append((url, dict(headers or {})))
        a = self.answers.get(url)
        if a is None:
            raise AssertionError(f"unexpected request to {url}")
        if isinstance(a, list):
            a = a.pop(0)
        return a

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


@pytest.fixture()
def ncon():
    c = warehouse.connect(":memory:")
    store.init_schema(c)
    yield c
    c.close()


def gamigion_answers(feed: Resp) -> dict[str, object]:
    return {
        "https://gamigion.com/robots.txt": Resp(
            301, headers={"Location": "https://www.gamigion.com/robots.txt"}
        ),
        "https://www.gamigion.com/robots.txt": Resp(200, ROBOTS_OK),
        "https://gamigion.com/feed/": Resp(
            301, headers={"Location": "https://www.gamigion.com/feed/"}
        ),
        "https://www.gamigion.com/feed/": feed,
    }


def run(ncon, session, tmp_path, feeds=(GAMIGION,), client=None, **kw):
    return news.run_news(
        ncon,
        day=DAY,
        out_dir=tmp_path / "radar",
        feeds=feeds,
        session=session,
        client=client,
        now=NOW,
        days=7,
        sleep=NOSLEEP,
        log=NOLOG,
        **kw,
    )


def stored(ncon) -> list[dict]:
    cur = ncon.execute("SELECT * FROM raw_mr_news ORDER BY link")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


# -- parsing -------------------------------------------------------------------------------


def test_rss_items_are_plain_text_capped_and_never_the_article():
    items, skipped = news.parse_feed(RSS, "https://www.gamigion.com/feed/")
    assert skipped == 3  # no link, a javascript: link, no title
    by_title = {i["title"]: i for i in items}
    first = by_title["AppLovin changes its ROAS bidding for puzzle games"]
    assert first["link"] == "https://www.gamigion.com/applovin-roas-puzzle/"  # utm_* dropped
    assert first["published_at"] == dt.datetime(2026, 11, 13, 11, 34, 9)
    assert first["summary"].startswith("AppLovin now bids on D7 ROAS for puzzle and sort games &")
    assert "appeared first on" not in first["summary"]  # WordPress boilerplate removed
    seed = by_title["Studio raises $5M seed round to build AI level tools"]
    assert seed["link"] == "https://www.gamigion.com/studio-raises-seed/"  # relative, resolved
    assert "<" not in seed["summary"] and "script" not in seed["summary"]
    apple = by_title["Apple tightens App Store review guidelines"]  # tag removed from the title
    assert apple["link"] == "https://www.gamigion.com/apple-guidelines/"  # fragment dropped
    assert apple["published_at"] is None
    assert len(apple["summary"]) <= news.SUMMARY_CHARS and apple["summary"].endswith("…")
    blob = json.dumps(items, default=str)
    assert "FULL ARTICLE TEXT" not in blob and "<" not in blob and "javascript" not in blob


def test_atom_entries_use_summary_and_the_alternate_link():
    items, skipped = news.parse_feed(ATOM, "https://mobilegamer.biz/feed/")
    assert skipped == 1
    g = items[0]
    assert g["title"] == "Google Play lowers fees for small developers"
    assert g["link"] == "https://mobilegamer.biz/google-play-fees/"
    assert g["published_at"] == dt.datetime(2026, 11, 14, 8, 30)  # +01:00 -> UTC
    assert "10%" in g["summary"] and "FULL ARTICLE TEXT" not in json.dumps(items, default=str)


@pytest.mark.parametrize(
    "name, message", [("malformed.xml", "not well-formed"), ("entity.xml", "DOCTYPE or entity")]
)
def test_malformed_or_entity_feeds_are_refused(name, message):
    with pytest.raises(news.FeedError, match=message):
        news.parse_feed((FIX / name).read_bytes(), "https://www.gamigion.com/feed/")
    with pytest.raises(news.FeedError, match="not an RSS or Atom"):
        news.parse_feed(b"<html><body>hi</body></html>", "https://www.gamigion.com/feed/")


# -- fetching ------------------------------------------------------------------------------


def test_robots_first_redirect_within_the_site_then_conditional_get(ncon, tmp_path):
    feed = Resp(200, RSS, {"ETag": 'W/"v1"', "Last-Modified": "Fri, 13 Nov 2026 11:52:58 GMT"})
    sess = Session(gamigion_answers(feed))
    res = run(ncon, sess, tmp_path)
    assert [f.status for f in res.feeds] == ["new"] and res.new_items == 3
    assert sess.urls() == [
        "https://gamigion.com/robots.txt",
        "https://www.gamigion.com/robots.txt",
        "https://gamigion.com/feed/",
        "https://www.gamigion.com/feed/",
    ]
    ua = {h["User-Agent"] for _, h in sess.calls}
    assert ua == {news.USER_AGENT} and "studio-pulse-news/" in news.USER_AGENT
    assert feed.closed
    ledger = ncon.execute(
        "SELECT purpose, host, status FROM raw_mr_request ORDER BY ts, path"
    ).fetchall()
    assert ("news", "www.gamigion.com", 200) in ledger and len(ledger) == 4
    # the next day: one conditional request per feed, 304, nothing new
    sess2 = Session(
        {
            **gamigion_answers(Resp(304)),
            "https://gamigion.com/feed/": Resp(
                301, headers={"Location": "https://www.gamigion.com/feed/"}
            ),
        }
    )
    res2 = run(ncon, sess2, tmp_path)
    assert [f.status for f in res2.feeds] == ["not_modified"] and res2.new_items == 0
    sent = dict(sess2.calls)["https://gamigion.com/feed/"]
    assert sent["If-None-Match"] == 'W/"v1"'
    assert sent["If-Modified-Since"] == "Fri, 13 Nov 2026 11:52:58 GMT"
    assert len(stored(ncon)) == 3
    # validators survive a 304 for the day after
    assert news.feed_state(ncon, "gamigion") == {
        "etag": 'W/"v1"',
        "last_modified": "Fri, 13 Nov 2026 11:52:58 GMT",
    }


def test_the_same_link_from_two_feeds_is_stored_once(ncon, tmp_path):
    answers = gamigion_answers(Resp(200, RSS))
    answers |= {
        "https://mobilegamer.biz/robots.txt": Resp(404),  # RFC 9309: no robots.txt, no rules
        "https://mobilegamer.biz/feed/": Resp(200, ATOM),
    }
    res = run(ncon, Session(answers), tmp_path, feeds=(GAMIGION, MOBILEGAMER))
    assert [f.new_items for f in res.feeds] == [3, 1]
    links = [r["link"] for r in stored(ncon)]
    assert len(links) == len(set(links)) == 4
    assert links.count("https://www.gamigion.com/applovin-roas-puzzle/") == 1
    # a re-run of the same answers adds nothing
    res = run(ncon, Session(answers | {
        "https://www.gamigion.com/feed/": Resp(200, RSS),
        "https://mobilegamer.biz/feed/": Resp(200, ATOM),
    }), tmp_path, feeds=(GAMIGION, MOBILEGAMER))  # fmt: skip
    assert res.new_items == 0 and len(stored(ncon)) == 4


def test_robots_disallow_means_the_feed_is_never_requested(ncon, tmp_path):
    sess = Session({"https://naavik.co/robots.txt": Resp(200, ROBOTS_NO)})
    res = run(ncon, sess, tmp_path, feeds=(NAAVIK,))
    assert res.feeds[0].status == "robots_disallowed"
    assert sess.urls() == ["https://naavik.co/robots.txt"]
    assert any("naavik" in n and "robots.txt" in n for n in res.notes)
    # an unreachable robots.txt (5xx) is a full disallow too
    sess = Session({"https://naavik.co/robots.txt": Resp(503)})
    assert run(ncon, sess, tmp_path, feeds=(NAAVIK,)).feeds[0].status == "robots_disallowed"


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/feed/",
        "https://mobilegamer.biz/feed/",  # another news host, but not this feed's site
        "http://www.gamigion.com/feed/",  # not https
        "https://www.gamigion.com:8443/feed/",
    ],
)
def test_a_redirect_off_the_feeds_site_is_refused(ncon, tmp_path, location):
    answers = gamigion_answers(Resp(200, RSS))
    answers["https://gamigion.com/feed/"] = Resp(302, headers={"Location": location})
    sess = Session(answers)
    res = run(ncon, sess, tmp_path)
    assert res.feeds[0].status == "error" and "refused" in res.feeds[0].error
    assert location not in sess.urls() and stored(ncon) == []


def test_size_cap(ncon, tmp_path):
    big = Resp(200, RSS, {"Content-Length": str(10_000_000)})
    res = run(ncon, Session(gamigion_answers(big)), tmp_path)
    assert "cap" in res.feeds[0].error
    streamed = Resp(200, RSS * 3)  # no Content-Length: counted while reading
    res = run(ncon, Session(gamigion_answers(streamed)), tmp_path, max_bytes=len(RSS))
    assert "cap" in res.feeds[0].error and stored(ncon) == []


def test_news_hosts_are_their_own_allow_list():
    assert not (news.NEWS_HOSTS & STORE_HOSTS)
    assert STORE_HOSTS == frozenset(
        {"itunes.apple.com", "apps.apple.com", "play.google.com"}
        | {f"is{i}-ssl.mzstatic.com" for i in range(1, 6)}
    )
    assert all("*" not in h for h in news.NEWS_HOSTS)
    for f in news.FEEDS:
        assert f.url.startswith("https://") and f.hosts <= news.NEWS_HOSTS
    with pytest.raises(news.HostNotAllowed):
        news.NewsReader.allowed_url("https://evil.example/feed/", news.NEWS_HOSTS)
    assert all(w not in news.USER_AGENT.lower() for w in ("mozilla", "chrome", "@"))


# -- tags ----------------------------------------------------------------------------------


def test_without_an_llm_keyword_tags_are_used(ncon, tmp_path):
    run(ncon, Session(gamigion_answers(Resp(200, RSS))), tmp_path)
    rows = {r["title"]: r for r in stored(ncon)}
    a = rows["AppLovin changes its ROAS bidding for puzzle games"]
    assert a["tag_source"] == "keyword" and a["why"] is None
    assert set(json.loads(a["topics"])) >= {"ua"} and a["relevance"] >= 3
    s = rows["Studio raises $5M seed round to build AI level tools"]
    assert set(json.loads(s["topics"])) >= {"funding", "ai_tools"}
    assert all(1 <= r["relevance"] <= 5 for r in rows.values())


def _llm_answer(req):
    assert "untrusted" in req.system.lower()
    payload, _ = json.JSONDecoder().raw_decode(req.prompt.split("untrusted text):\n", 1)[1])
    assert {"id", "source", "title", "summary"} == set(payload[0])  # no link, no article
    out = []
    for it in payload:
        if it["title"].startswith("AppLovin"):
            out.append(
                {"id": it["id"], "topics": ["ua", "monetization", "made-up"], "relevance": 5,
                 "why": "ROAS bidding for puzzle games changes how a sort game buys users."}
            )  # fmt: skip
        elif it["title"].startswith("Studio raises"):
            # a number that is not in the item: the line is dropped, the tags are kept
            out.append({"id": it["id"], "topics": ["funding"], "relevance": 2,
                        "why": "A $50M round shows investor appetite."})  # fmt: skip
        else:
            out.append({"id": it["id"], "topics": ["platform"], "relevance": 4,
                        "why": "See https://evil.example for details."})  # fmt: skip
    out.append({"id": "n99", "topics": ["ua"], "relevance": 5, "why": "not sent"})
    return {"items": out}


def test_llm_tags_are_kept_only_when_grounded(ncon, tmp_path):
    prov = FakeProvider(_llm_answer)
    client = LLMClient(prov, cache_dir=tmp_path / "llm")
    res = run(ncon, Session(gamigion_answers(Resp(200, RSS))), tmp_path, client=client)
    assert res.tagged["llm"] == 3 and res.tagged["keyword"] == 0 and len(prov.calls) == 1
    rows = {r["title"]: r for r in stored(ncon)}
    a = rows["AppLovin changes its ROAS bidding for puzzle games"]
    assert json.loads(a["topics"]) == ["ua", "monetization"] and a["relevance"] == 5
    assert a["why"].startswith("ROAS bidding") and a["tag_source"] == "llm"
    assert rows["Studio raises $5M seed round to build AI level tools"]["why"] is None
    assert rows["Apple tightens App Store review guidelines"]["why"] is None  # had a link
    assert a["prompt_version"] == news.PROMPT_VERSION and a["tag_model"] == "fake-1"
    doc = json.loads((tmp_path / "radar" / "news.json").read_text(encoding="utf-8"))
    assert doc["items"][0]["relevance"] == 5  # most relevant first


def test_an_llm_error_falls_back_to_keywords(ncon, tmp_path):
    prov = FakeProvider(errors=[TransientLLMError("HTTP 500 upstream")] * 10)
    client = LLMClient(prov, cache_dir=tmp_path / "llm", max_retries=0)
    res = run(ncon, Session(gamigion_answers(Resp(200, RSS))), tmp_path, client=client)
    assert res.tagged["keyword"] == 3 and res.tagged["llm"] == 0
    assert {r["tag_source"] for r in stored(ncon)} == {"keyword"}


def test_verify_why_rules():
    text = "Google Play cuts its fee to 10% for developers under $1M"
    assert (
        news.verify_why("A 10% fee helps small studios.", text) == "A 10% fee helps small studios."
    )
    assert news.verify_why("A 15% fee hurts.", text) == ""
    assert news.verify_why("<b>bold</b>", text) == "" and news.verify_why(None, text) == ""
    assert news.verify_why("javascript:alert(1)", text) == ""


# -- settings, CLI, page ---------------------------------------------------------------------


def test_unknown_feed_ids_are_refused(tmp_path):
    from studio_pulse.radar.settings import from_config

    cfg = config_from_dict(
        {"apps": [{"id": "x"}], "radar": {"news": {"feeds": ["gamigion", "evil"]}}},
        base_dir=tmp_path,
    )
    with pytest.raises(ConfigError, match="unknown feed"):
        from_config(cfg)
    ok = config_from_dict(
        {"apps": [{"id": "x"}], "radar": {"news": {"feeds": ["naavik"], "days": 3}}},
        base_dir=tmp_path,
    )
    s = from_config(ok)
    assert s.news_feeds == ["naavik"] and s.news_days == 3 and s.news_enabled


def _cfg_file(tmp_path: Path) -> Path:
    import yaml

    p = tmp_path / "pulse.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "apps": [{"id": "x"}],
                "warehouse": str(tmp_path / "w.duckdb"),
                "state_dir": str(tmp_path / "state"),
                "radar": {"out_dir": str(tmp_path / "radar"), "news": {"feeds": ["gamigion"]}},
            }
        ),
        encoding="utf-8",
    )
    return p


def test_cli_news_fetches_and_lists(tmp_path, monkeypatch, capsys):
    sess = Session(gamigion_answers(Resp(200, RSS)))
    monkeypatch.setattr(news.requests, "Session", lambda: sess)
    monkeypatch.setenv("PULSE_LLM_PROVIDER", "none")
    p = _cfg_file(tmp_path)
    assert cli.main(["radar", "news", "-c", str(p), "--days", "30", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert len(items) == 3 and {i["tag_source"] for i in items} == {"keyword"}
    assert all(set(i) >= {"title", "link", "published_at", "summary", "source"} for i in items)
    # --no-fetch lists what is stored and sends nothing
    monkeypatch.setattr(news.requests, "Session", lambda: pytest.fail("a request was made"))
    assert cli.main(["radar", "news", "-c", str(p), "--days", "30", "--no-fetch"]) == 0
    out = capsys.readouterr().out
    assert "AppLovin changes its ROAS bidding" in out and "https://www.gamigion.com/" in out


def test_radar_page_shows_news_escaped(tmp_path):
    from fastapi.testclient import TestClient

    from studio_pulse.serve.app import create_app

    out = tmp_path / "radar"
    out.mkdir()
    payload = '"><img src=x onerror=alert(document.domain)>'
    doc = {
        "days": 7,
        "feeds": [{"feed": "naavik", "status": "error", "error": f"HTTP 500 {payload}"}],
        "items": [
            {"title": f"Fees {payload}", "link": "https://mobilegamer.biz/a/",
             "source_name": "Mobilegamer.biz", "published_at": "2026-11-14T08:30:00",
             "summary": f"summary {payload}", "topics": ["platform", "<script>"],
             "relevance": 4, "why": f"why {payload}", "tag_source": "llm"},
            {"title": "Bad link", "link": "javascript:alert(1)", "source_name": "X",
             "topics": [], "relevance": 1, "why": None, "tag_source": "keyword"},
        ],
    }  # fmt: skip
    (out / "news.json").write_text(json.dumps(doc), encoding="utf-8")
    cfg = config_from_dict(
        {"apps": [{"id": "x"}], "warehouse": str(tmp_path / "w.duckdb"),
         "radar": {"out_dir": str(out)}},
        base_dir=tmp_path,
    )  # fmt: skip
    client = TestClient(create_app(cfg, token="t"), base_url="http://127.0.0.1:8765")
    page = client.get("/radar").text
    assert "Sektör haberleri" in page and 'href="https://mobilegamer.biz/a/"' in page
    assert "<img src=x" not in page and "onerror=alert(document.domain)>" not in page
    assert page.count("&lt;img src=x onerror=alert(document.domain)&gt;") >= 4
    assert "javascript:alert" not in page and "&lt;script&gt;" not in page  # unknown topic
    assert "Platform / politika" in page
    en = client.get("/radar?lang=en").text
    assert "Industry news" in en and "Why it matters" in en
    empty = config_from_dict(
        {"apps": [{"id": "x"}], "radar": {"out_dir": str(tmp_path / "none")}}, base_dir=tmp_path
    )
    page = TestClient(create_app(empty, token="t"), base_url="http://127.0.0.1:8765").get("/radar")
    assert "pulse radar news" in page.text


def test_radar_run_reads_news_and_a_replay_skips_it(tmp_path):
    from studio_pulse.radar import run as radar_run
    from studio_pulse.radar.cli import _replay_config
    from studio_pulse.radar.replay import RadarReplaySession

    fix = Path(__file__).parent / "fixtures" / "radar" / "2026-11-12"
    cfg = _replay_config(fix, tmp_path / "w")
    res = radar_run.run_radar(
        cfg, session=RadarReplaySession(fix), sleep=NOSLEEP, log=NOLOG, use_llm=False
    )
    assert res.ok and "news: not read in a replay (no recorded feeds)" in res.notes
    sess = Session(gamigion_answers(Resp(200, RSS)))
    cfg.raw["radar"]["news"] = {"feeds": ["gamigion"], "days": 30}
    res = radar_run.run_radar(
        cfg,
        session=RadarReplaySession(fix),
        news_session=sess,
        sleep=NOSLEEP,
        log=NOLOG,
        use_llm=False,
    )
    assert res.ok, res.errors
    assert res.counts["news"]["new_items"] == 3 and Path(res.files["news"]).is_file()
    assert "https://www.gamigion.com/feed/" in sess.urls()
    names = {c.name: c.status for c in res.checks}
    assert names.get("radar_news_link_unique") == "pass"
