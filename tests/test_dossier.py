"""Game dossier (radar/dossier) and competitor ads (radar/adintel): fixtures only.

Store answers come from tests/fixtures/radar/dossier (synthetic apps, synthetic reviews, tiny
generated screenshots, the public robots.txt rules); the LLM is the fake provider; BigQuery is a
fake client; the Meta Ad Library pages are recorded-shape synthetic fixtures. No test opens a
socket.
"""

from __future__ import annotations

import datetime as dt
import json
import socket
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

from studio_pulse import cli, warehouse
from studio_pulse.config import ConfigError, config_from_dict
from studio_pulse.connectors.replay import ReplaySession
from studio_pulse.llm import FakeProvider, LLMClient
from studio_pulse.radar import run as radar_run
from studio_pulse.radar import store
from studio_pulse.radar.adintel import google_atc
from studio_pulse.radar.adintel.meta_library import MetaAdLibrary, archive_url
from studio_pulse.radar.dossier import collect, deconstruct, peers, render, reviews, rubric
from studio_pulse.radar.dossier.labels import check_labels
from studio_pulse.radar.dossier.replay import DossierReplaySession, synth_reviews_block
from studio_pulse.radar.dossier.run import build_dossier
from studio_pulse.radar.dossier.settings import from_config
from studio_pulse.radar.http import Fetcher
from studio_pulse.radar.replay import synth_play_page
from studio_pulse.radar.settings import DEFAULT_PARAMS
from studio_pulse.radar.taxonomy import load

FIX = Path(__file__).parent / "fixtures" / "radar" / "dossier"
DAY = dt.date(2026, 11, 14)
PEBBLE = "1000000001"
ARENA = "1000000002"


# -- fakes --------------------------------------------------------------------------------------


def fake_support(req):
    """A judge that knows one rule: a claim about 3D needs evidence that mentions 3D."""
    items = json.loads(req.prompt.splitlines()[1])["items"]
    out = []
    for it in items:
        ev = it["evidence"].get("quote") or it["evidence"].get("note") or ""
        ok = "3d" not in it["claim"].lower() or "3d" in ev.lower()
        out.append({"id": it["id"], "supported": ok, "reason": "ok" if ok else "colour, not 3D"})
    return {"judgements": out}


def fake_peer_reviews(req):
    """Labels any review set by keywords, each with a verbatim quote (the peers' reviews)."""
    data = json.loads(req.prompt.split("Reviews (untrusted data, JSON):\n", 1)[1])["reviews"]
    out = []
    for r in data:
        low = r["text"].lower()
        if "ads" in low:
            i = low.index("ads")
            out.append(
                {
                    "review": r["id"],
                    "side": "complaint",
                    "theme": "ads",
                    "quote": r["text"][max(0, i - 8) : i + 3],
                }
            )
        if "relaxing" in low:
            out.append(
                {"review": r["id"], "side": "praise", "theme": "relaxing", "quote": "Relaxing"}
            )
    return {"labels": out}


def fake_llm(req):
    """Schema-valid answers (what a provider with a response schema can return)."""
    if req.purpose == "dossier_support":
        return fake_support(req)
    if req.purpose == "dossier_reviews" and "So relaxing after work" not in req.prompt:
        return fake_peer_reviews(req)
    out = raw_answer(req)
    if req.purpose == "dossier_deconstruct" and out.get("mechanics"):
        props = req.schema["properties"]
        mechs = props["mechanics"]["items"]["properties"]["value"]["enum"]
        sources = props["core_loop"]["items"]["properties"]["evidence"]["items"]["properties"][
            "source"
        ]["enum"]
        out["mechanics"] = [m for m in out["mechanics"] if m["value"] in mechs]
        for sec in ("core_loop", "onboarding"):
            out[sec] = [c for c in out[sec] if all(e["source"] in sources for e in c["evidence"])]
    if req.purpose == "dossier_reviews":
        lab = req.schema["properties"]["labels"]
        ids = lab["items"]["properties"]["review"]["enum"]
        out["labels"] = [x for x in out["labels"] if x["review"] in ids]
    return out


def raw_answer(req):
    if req.purpose == "dossier_deconstruct":
        if "Pebble Sort" not in req.prompt:
            return {k: [] for k in req.schema["required"]}
        q = "apple.description"
        return {
            "mechanics": [
                {
                    "value": "color-sort",
                    "evidence": [{"source": q, "quote": "a relaxing color sort puzzle"}],
                },
                {
                    "value": "merge",
                    "evidence": [{"source": q, "quote": "merge pebbles into gems"}],
                },  # not in text
                {
                    "value": "pebble-pour",
                    "evidence": [{"source": q, "quote": "pour the top pebbles"}],
                },  # not in taxonomy
            ],
            "proposed_tags": [
                {
                    "name": "pour-sort",
                    "why": "pouring stacks between tubes",
                    "evidence": [{"source": q, "quote": "pour the top pebbles into another tube"}],
                },
                {
                    "name": "color-sort",
                    "why": "already a mechanic",
                    "evidence": [{"source": q, "quote": "color sort puzzle"}],
                },
            ],
            "core_loop": [
                {
                    "text": "Tap a tube and pour its top pebbles until each tube holds one color.",
                    "text_tr": "Bir tüpe dokunup üstteki çakılları her tüp tek renk olana dek dök.",
                    "evidence": [
                        {
                            "source": q,
                            "quote": "Tap a tube to pour the top pebbles into another tube",
                        },
                        {"source": "s1", "quote": "tubes with colored pebbles"},
                    ],
                },
                {
                    "text": "Levels have 12 tubes.",
                    "text_tr": "Bölümlerde 12 tüp var.",
                    "evidence": [{"source": "s2", "quote": "many tubes"}],
                },  # a number
            ],
            "meta_layer": [
                {
                    "text": "Unlockable themes reward progress.",
                    "text_tr": "Açılan temalar ilerlemeyi ödüllendirir.",
                    "evidence": [{"source": q, "quote": "unlock new themes as you go"}],
                },
            ],
            "monetization": [
                {
                    "kind": "boosters",
                    "text": "Boosters help when stuck.",
                    "text_tr": "Takılınca güçlendiriciler yardım eder.",
                    "evidence": [{"source": q, "quote": "Use boosters when you get stuck"}],
                },
                {
                    "kind": "remove-ads",
                    "text": "A purchase removes ads.",
                    "text_tr": "Bir satın alma reklamları kaldırır.",
                    "evidence": [{"source": q, "quote": "remove ads with a one-time purchase"}],
                },
                {
                    "kind": "battle-pass",
                    "text": "A battle pass is sold.",
                    "text_tr": "Savaş bileti satılıyor.",
                    "evidence": [{"source": q, "quote": "season pass for gold members"}],
                },  # invented
            ],
            "art_style": [
                {
                    "kind": "2d",
                    "text": "Flat bright pebbles on a soft background.",
                    "text_tr": "Yumuşak zemin üzerinde düz parlak çakıllar.",
                    "evidence": [{"source": "s1", "quote": "flat colored pebbles"}],
                },
                {
                    "kind": "stylized-3d",
                    "text": "Stylized 3D pebbles and tubes.",
                    "text_tr": "Stilize 3B çakıllar ve tüpler.",
                    "evidence": [{"source": q, "quote": "a relaxing color sort puzzle"}],
                },  # a colour sentence is no evidence for 3D art: the support check drops it
            ],
            "onboarding": [
                {
                    "text": "The first screen shows a single pour.",
                    "text_tr": "İlk ekran tek bir dökme gösterir.",
                    "evidence": [{"source": "s9", "quote": "a hand pointer"}],
                },  # no such screenshot
            ],
            "liveops": [
                {
                    "kind": "events",
                    "text": "Seasonal events and daily challenges.",
                    "text_tr": "Mevsimlik etkinlikler ve günlük görevler.",
                    "evidence": [
                        {
                            "source": q,
                            "quote": "Daily challenges and seasonal events keep things fresh",
                        }
                    ],
                },
            ],
            "strengths": [
                {
                    "text": "A calm, readable puzzle that works offline.",
                    "text_tr": "Çevrimdışı çalışan sakin, okunaklı bir bulmaca.",
                    "evidence": [{"source": q, "quote": "Play offline, no wifi needed"}],
                },
                {
                    "text": "Rated the best game of the year.",
                    "text_tr": "Yılın oyunu seçildi.",
                    "evidence": [],
                },  # no evidence
            ],
            "unknowns": ["How hard do levels get?", "Are there 3 lives?"],
        }
    if req.purpose == "dossier_reviews":
        L = lambda r, side, t, q: {"review": r, "side": side, "theme": t, "quote": q}  # noqa: E731
        return {
            "labels": [
                L("r1", "praise", "relaxing", "So relaxing after work"),
                L("r1", "praise", "visuals", "The pebble colors look lovely"),
                L("r1", "praise", "relaxing", "So relaxing after work"),  # duplicate
                L("r2", "complaint", "ads", "An ad after every single level ruins it"),
                L("r3", "praise", "level-design", "Great level design"),
                L("r4", "complaint", "bugs-crashes", "Crashes on level start"),
                L("r5", "praise", "fun-gameplay", "Fun puzzle"),
                L(
                    "r5",
                    "complaint",
                    "rewards-economy",
                    "boosters are expensive and coins come slowly",
                ),
                L("r6", "praise", "offline", "works offline on the subway"),
                L("r7", "complaint", "ads", "ads everywhere, unplayable"),  # not in the review
                L("r9", "praise", "relaxing", "relaxing"),  # no such review
                L("r8", "praise", "relaxing", "relaxing game"),
            ]
        }
    raise AssertionError(req.purpose)


class FakeJob:
    def __init__(self, processed: int, billed: int | None, table: pa.Table | None) -> None:
        self.total_bytes_processed = processed
        self.total_bytes_billed = billed
        self._table = table

    def result(self):
        return self

    def to_arrow(self, create_bqstorage_client=False):
        return self._table


class FakeBQ:
    """Answers the schema query and the weekly extract; records every job."""

    def __init__(self, extract_bytes: int = 26_623_000_000, rows: list[dict] | None = None) -> None:
        self.extract_bytes = extract_bytes
        self.rows = EXTRACT_ROWS if rows is None else rows
        self.jobs: list[tuple[str, bool]] = []

    def query(self, sql, job_config=None):
        dry = bool(getattr(job_config, "dry_run", False))
        self.jobs.append((sql, dry))
        if "INFORMATION_SCHEMA" in sql:
            paths = sorted(google_atc.EXPECTED)
            return FakeJob(0, None if dry else 10_485_760, pa.table({"field_path": paths}))
        if dry:
            return FakeJob(self.extract_bytes, None, None)
        return FakeJob(self.extract_bytes, self.extract_bytes, pa.Table.from_pylist(self.rows))


def _region(code, n, first, last):
    return {"region": code, "creatives": n, "first_shown": first, "last_shown": last}


EXTRACT_ROWS = [
    {
        "advertiser_id": "AR000000000000000001",
        "name": "Example Pebbleworks Ltd",
        "creatives": 42,
        "games_creatives": 40,
        "video": 30,
        "image": 12,
        "text": 0,
        "active_30d": 25,
        "first_shown": "2026-10-22",
        "last_shown": "2026-11-13",
        "regions_n": 2,
        "regions": [
            _region("US", 40, "2026-10-22", "2026-11-13"),
            _region("DE", 18, "2026-10-25", "2026-11-12"),
        ],
    },
    {
        "advertiser_id": "AR000000000000000002",
        "name": "Pebbleworks Garden Supplies",  # a namesake with some games-topic ads
        "creatives": 90,
        "games_creatives": 12,
        "video": 0,
        "image": 90,
        "text": 0,
        "active_30d": 3,
        "first_shown": "2024-01-02",
        "last_shown": "2026-10-01",
        "regions_n": 1,
        "regions": [_region("GB", 90, "2024-01-02", "2026-10-01")],
    },
]


def refresh_extract(path: Path, bq: FakeBQ | None = None, **kw):
    con = warehouse.connect(path)
    try:
        return google_atc.refresh(
            con,
            project=None,
            s=google_atc.AdIntelSettings(**kw),
            today=DAY,
            client=bq or FakeBQ(),
            log=lambda m: None,
        )
    finally:
        con.close()


# -- warehouse with radar history -----------------------------------------------------------------


def _chart(con, day, cc, ranks, chart="topfreeapplications", genre="7012", dev=None):
    store.insert(
        con,
        "raw_mr_chart",
        [
            {
                "snapshot_date": day,
                "fetched_at": dt.datetime(day.year, day.month, day.day, 9),
                "store": "apple",
                "country": cc,
                "chart": chart,
                "genre_id": genre,
                "requested_limit": 100,
                "n_rows": len(ranks),
                "rank": r,
                "app_id": a,
                "bundle_id": f"b.{a}",
                "name": f"Game {a}",
                "developer": (dev or {}).get(a, f"Dev {a}"),
                "category_id": "6014",
                "feed_updated": dt.datetime(day.year, day.month, day.day, 8),
            }
            for a, r in ranks.items()
        ],
    )


def build_warehouse(path: Path) -> None:
    con = warehouse.connect(path)
    store.init_schema(con)
    store.set_params(con, DEFAULT_PARAMS)
    store.set_config(
        con,
        {
            "as_of": DAY.isoformat(),
            "country": ["us", "gb"],
            "trend_list": "topfreeapplications:7012",
        },
    )
    sorters = [f"S{i}" for i in range(6)]
    devs = {
        "S0": "Big Sort Co",
        "S1": "Big Sort Co",
        "S2": "Big Sort Co",
        "S3": "Tiny Dev",
        "S4": "Other",
        "S5": "Other 2",
    }
    for back in (2, 1, 0):
        day = DAY - dt.timedelta(days=back)
        ranks = {a: i + 5 for i, a in enumerate(sorters)}
        ranks[PEBBLE] = 11 + back * 10  # climbing: 31, 21, 11
        _chart(con, day, "us", ranks, dev=devs)
        _chart(con, day, "gb", {PEBBLE: 60, "S0": 3})
        _chart(
            con,
            day,
            "us",
            {"S0": 30, "S1": 70},
            chart="topgrossingapplications",
            genre="6014",
            dev=devs,
        )
        store.insert(
            con,
            "raw_mr_app",
            [
                {
                    "snapshot_date": day,
                    "fetched_at": dt.datetime(day.year, day.month, day.day, 9),
                    "store": "apple",
                    "country": "us",
                    "app_id": PEBBLE,
                    "name": "Pebble Sort: Color Puzzle",
                    "release_date": dt.date(2026, 10, 20),
                    "current_version_date": dt.date(2026, 11, 10)
                    if back == 0
                    else dt.date(2026, 11, 3),
                    "version": "1.0.6" if back == 0 else "1.0.5",
                    "rating_count": 800 + (2 - back) * 20,
                    "source": "lookup",
                }
            ],
        )
    # the peers' Lookup reads: they last updated 20 to 45 days ago and gain fewer ratings
    for i, a in enumerate(sorters):
        for back in (1, 0):
            day = DAY - dt.timedelta(days=back)
            store.insert(
                con,
                "raw_mr_app",
                [
                    {
                        "snapshot_date": day,
                        "fetched_at": dt.datetime(day.year, day.month, day.day, 9),
                        "store": "apple",
                        "country": "us",
                        "app_id": a,
                        "name": f"Game {a}",
                        "release_date": dt.date(2025, 6, 1),
                        "current_version_date": DAY - dt.timedelta(days=20 + 5 * i),
                        "version": "2.0",
                        "rating_count": 50_000 + i * 1000 + (1 - back) * 3,
                        "rating_avg": 4.3,
                        "source": "lookup",
                    }
                ],
            )
    store.insert(
        con,
        "raw_mr_xref",
        [
            {
                "apple_id": "S0",
                "bundle_id": "com.example.sorter0",
                "play_id": "com.example.sorter0",
                "method": "bundle_id",
                "status": "mapped",
                "verified_on": DAY,
            }
        ],
    )
    store.insert(
        con,
        "raw_mr_profile",
        [
            {
                "app_key": f"apple:{a}",
                "profile_id": f"keyword:{a}",
                "source": "keyword",
                "analyzed_at": dt.datetime(2026, 11, 1),
                "input_sha256": "x",
                "dimension": "mechanic_primary",
                "value": "color-sort",
                "field": "apple.name",
                "quote": "color sort",
                "visual": False,
            }
            for a in [*sorters, PEBBLE]
        ],
    )
    radar_run.run_models(con)
    con.close()


def make_cfg(tmp_path: Path, **radar_extra) -> object:
    wh = tmp_path / "wh.duckdb"
    if not wh.exists():
        build_warehouse(wh)
    radar = {
        "countries": ["us", "gb"],
        "out_dir": str(tmp_path / "radar"),
        "creative_forge_library": str(tmp_path / "forge"),
        "dossier": {
            "studio": {"strengths": ["color-sort"], "genres": ["puzzle"], "max_cpi_usd": 5.0},
            "countries": ["us"],
        },
    }
    radar.update(radar_extra)
    return config_from_dict(
        {
            "apps": [{"id": "x"}],
            "warehouse": str(wh),
            "state_dir": str(tmp_path / "state"),
            "radar": radar,
        },
        base_dir=tmp_path,
    )


def run(tmp_path, target=PEBBLE, *, client=True, extract=True, meta=None, **kw):
    cfg = make_cfg(tmp_path)
    if extract:
        refresh_extract(cfg.warehouse)
    prov = FakeProvider(fake_llm)
    llm = LLMClient(prov, cache_dir=tmp_path / "llmcache", budget_usd=0.5) if client else None
    res = build_dossier(
        cfg,
        target,
        session=DossierReplaySession(FIX),
        day=DAY,
        llm_client=llm,
        use_llm=client,
        meta=meta or MetaAdLibrary(token="", today=DAY),
        sleep=lambda _s: None,
        log=lambda m: None,
        **kw,
    )
    return cfg, res, prov


# -- targets and parsers ---------------------------------------------------------------------------


def test_parse_target_accepts_ids_urls_and_packages():
    assert collect.parse_target("6812166638").apple_id == "6812166638"
    assert collect.parse_target("id6812166638").key == "apple-6812166638"
    assert (
        collect.parse_target("https://apps.apple.com/us/app/x/id6812166638?l=en").apple_id
        == "6812166638"
    )
    t = collect.parse_target("https://play.google.com/store/apps/details?id=com.example.game&hl=en")
    assert t.play_package == "com.example.game" and t.key == "play-com.example.game"
    with pytest.raises(collect.TargetError):
        collect.parse_target("../../etc/passwd")


def test_play_reviews_are_parsed_without_reviewer_identity():
    revs = json.loads(
        (FIX / "play" / "com.example.pebblesort_reviews.json").read_text(encoding="utf-8")
    )
    fields = json.loads((FIX / "play" / "com.example.pebblesort.json").read_text(encoding="utf-8"))
    html = synth_play_page(fields).replace("</body>", synth_reviews_block(revs) + "</body>")
    got = reviews.parse_play_reviews(html)
    assert len(got) == 8 and got[1]["score"] == 2 and got[1]["replied"] and not got[0]["replied"]
    assert got[0]["version"] == "1.0.4" and got[0]["date"] == "2026-10-24"
    blob = json.dumps(got)
    assert "Example Player" not in blob and "syn-1" not in blob  # no names, ids hashed
    assert reviews.versions_from_reviews(got)[0] == {
        "version": "1.0.4",
        "first_review": "2026-10-24",
        "n": 1,
    }


def test_apple_reviews_feed_is_refused_by_robots_before_any_request(tmp_path):
    con = warehouse.connect(":memory:")
    store.init_schema(con)
    sess = DossierReplaySession(FIX)
    f = Fetcher(
        con, DAY, session=sess, run_id="t", robots_hosts=("itunes.apple.com",), sleep=lambda s: None
    )
    out = reviews.fetch_apple_reviews(f, PEBBLE, "us")
    assert out["status"] == "refused" and "robots.txt" in out["reason"]
    assert [r.path for r in sess.recorded] == ["/robots.txt"]  # the feed itself was never asked
    # Lookup (/lookup?id=...) stays allowed: `Disallow: /*/lookup?` needs a path segment first
    f.get(f"https://itunes.apple.com/lookup?id={PEBBLE}&country=us", "t")
    # the parser still works on the feed's recorded shape (author fields dropped)
    feed = {
        "feed": {
            "entry": [
                {
                    "author": {"name": {"label": "Someone"}},
                    "im:rating": {"label": "4"},
                    "im:version": {"label": "1.0.6"},
                    "title": {"label": "Nice"},
                    "content": {"label": "Calm game."},
                    "id": {"label": "123"},
                    "updated": {"label": "2026-11-01T10:00:00-07:00"},
                }
            ]
        }
    }
    got = reviews.parse_apple_reviews(json.dumps(feed).encode())
    assert (
        got[0]["text"] == "Nice. Calm game."
        and got[0]["score"] == 4
        and "Someone" not in json.dumps(got)
    )
    con.close()


# -- grounding --------------------------------------------------------------------------------------


def test_deconstruction_keeps_only_grounded_claims():
    tax = load()
    pool = json.loads((FIX / "apple" / "lookup_pool.json").read_text(encoding="utf-8"))
    fields = {"apple.description": pool["results"][0]["description"]}
    req = deconstruct.build_request(tax, fields, [])
    out = raw_answer(
        type(
            "R",
            (),
            {"purpose": "dossier_deconstruct", "prompt": "Pebble Sort", "schema": req.schema},
        )()
    )
    res = deconstruct.verify(out, fields, 3, tax)
    assert [m["value"] for m in res["mechanics"]] == ["color-sort"]
    assert [p["name"] for p in res["proposed_tags"]] == ["pour-sort"]
    assert len(res["core_loop"]) == 1 and res["core_loop"][0]["evidence"][1]["kind"] == "screenshot"
    assert res["art_style"][0]["visual_only"] and res["art_style"][0]["kind"] == "2d"
    assert {c["kind"] for c in res["monetization"]} == {"boosters", "remove-ads"}
    assert res["onboarding"] == [] and len(res["strengths"]) == 1
    assert res["unknowns"] == ["How hard do levels get?"]  # the one with a number is dropped
    reasons = {(r["section"], r["reason"].split(";")[0]) for r in res["rejected"]}
    assert ("mechanics", "apple.description: quote not found in the field") in reasons
    assert ("mechanics", "not in taxonomy") in reasons
    assert ("core_loop", "claim contains a number") in reasons
    assert ("onboarding", "screenshot s9 was not sent") in reasons
    assert ("strengths", "no evidence") in reasons
    assert ("proposed_tags", "already in the taxonomy") in reasons
    # the schema the model sees only allows taxonomy mechanics and the sent sources
    assert req.schema["properties"]["mechanics"]["items"]["properties"]["value"][
        "enum"
    ] == tax.values("mechanics")


def test_review_labels_are_checked_and_counted_by_code():
    revs = json.loads(
        (FIX / "play" / "com.example.pebblesort_reviews.json").read_text(encoding="utf-8")
    )
    themes = from_config(config_from_dict({"apps": [{"id": "x"}]}, base_dir=Path("."))).benchmarks[
        "review_themes"
    ]
    out = raw_answer(type("R", (), {"purpose": "dossier_reviews"})())
    kept, dropped = reviews.verify_labels(out, revs, themes)
    assert {d["reason"] for d in dropped} == {"quote not found in the review", "unknown review"}
    assert len(kept) == 9  # the duplicate (r1, praise, relaxing) is counted once
    counts = reviews.theme_counts(kept, themes, len(revs))
    praise = {r["theme"]: r["count"] for r in counts["praise"]}
    assert praise["relaxing"] == 2 and praise["visuals"] == 1
    # each theme shows its own quote, not another label's
    vis = next(r for r in counts["praise"] if r["theme"] == "visuals")
    assert vis["quotes"][0]["text"] == "The pebble colors look lovely"
    assert {r["theme"]: r["count"] for r in counts["complaints"]}[
        "ads"
    ] == 1  # r7's invented quote dropped
    assert all(r["basis"] == "llm" and r["of"] == 8 for r in counts["praise"])


# -- the whole dossier --------------------------------------------------------------------------------


def test_dossier_end_to_end_without_network(tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("a dossier test opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    cfg, res, prov = run(tmp_path, forge=True)
    d = res.facts
    assert res.ok and res.key == f"apple-{PEBBLE}" and check_labels(d) == []
    assert d["app"]["play_package"] == "com.example.pebblesort"
    assert d["mechanic"] == {
        "value": "color-sort",
        "source": "llm",
        "evidence": d["mechanic"]["evidence"],
    }
    # store data and the radar's history
    assert (
        d["store"]["play"]["real_installs"] == 254321
        and d["store"]["play"]["basis"] == "store_reported"
    )
    assert (
        d["warehouse"]["ranks_today"][0]["rank"] == 11
        and d["warehouse"]["chart_history"]["days_charted"] == 3
    )
    # cadence: versions from store fields, snapshots and reviews; only a lower bound is claimed
    assert [v["version"] for v in d["cadence"]["versions_known"]] == ["1.0.4", "1.0.5", "1.0.6"]
    assert d["cadence"]["versions_90d_min"] == 3 and d["cadence"]["days_since_update"] == 4
    sat = d["market"]["saturation"]
    assert sat["similar_apps"] == 6 and sat["similar_in_grossing"] == 2 and sat["top3_share"] > 0.5
    # screenshots: cached locally and sent to the model as images
    assert [s["id"] for s in d["screenshots"]] == ["s1", "s2", "s3"]
    assert all(Path(s["file"]).is_file() for s in d["screenshots"])
    dec_call = next(c for c in prov.calls if c.purpose == "dossier_deconstruct")
    assert len(dec_call.images) == 3 and dec_call.images[0].mime == "image/png"
    # injected instructions in the store text and reviews are passed as data, marked untrusted
    assert '"untrusted": true' in dec_call.prompt and "IGNORE ALL PREVIOUS" in dec_call.prompt
    # reviews: Play's page reviews mined; Apple's feed refused by robots.txt
    rv = d["reviews"]
    assert rv["n"] == 8 and rv["sources"]["apple"]["status"] == "refused"
    assert rv["themes"]["praise"][0]["theme"] == "relaxing"
    # ads: read from the local weekly extract (no BigQuery call in a dossier); Meta not set up
    g = d["ads"]["google_atc"]
    assert g["status"] == "ok" and g["patterns"] == ["pebbleworks"]
    assert [(a["name"], a["match"]) for a in g["advertisers"]] == [
        ("Example Pebbleworks Ltd", "name"),  # the developer's whole name: shown
        ("Pebbleworks Garden Supplies", "partial"),  # a namesake: not shown when a name matches
    ]
    top = g["advertisers"][0]
    assert top["creatives"] == 42 and top["active_30d"] == 25 and top["days_since_last_shown"] == 1
    assert [r["region"] for r in top["regions"]] == ["US", "DE"]
    assert g["extract"]["extract_date"] == "2026-11-14" and d["meta"]["bigquery_billed_gb"] == 0
    assert d["ads"]["meta"]["status"] == "not_configured"
    # the support check: one LLM judge call, the colour sentence offered for 3D art is dropped
    dec = d["deconstruction"]
    assert dec["support"]["status"] == "ok" and dec["support"]["unsupported_items"] == 1
    assert [c["kind"] for c in dec["art_style"]] == ["2d"]
    assert any(
        r["section"] == "art_style" and r["reason"].startswith("evidence does not support")
        for r in dec["rejected"]
    )
    sup_call = next(c for c in prov.calls if c.purpose == "dossier_support")
    assert len(sup_call.images) == 3 and "a relaxing color sort puzzle" in sup_call.prompt
    # peers: the six colour-sort games in today's charts, with numbers
    pe = d["peers"]
    assert pe["status"] == "ok" and pe["peer_set"] == "mechanic" and pe["n_peers"] == 6
    items = {i["metric"]: i for i in pe["items"]}
    upd = items["days_since_update"]
    assert upd["value"] == 4 and upd["peer_median"] == 32.5 and upd["better"] is True
    # rank 11 against a peers' median of 7.5: 47 % worse but under 5 places, so only context
    assert items["best_rank"]["better"] is None
    ads_cmp = items["review_complaints:ads"]
    assert (ads_cmp["value"], ads_cmp["of"], ads_cmp["peer_value"], ads_cmp["peer_of"]) == (
        1,
        8,
        4,
        6,
    )
    assert ads_cmp["better"] is True and ads_cmp["basis"] == "llm"
    assert pe["review_peers"] == [{"app_id": "S0", "package": "com.example.sorter0", "reviews": 6}]
    # decision: a colour sort game for a studio that ships colour sort
    dc = d["decision"]
    assert dc["verdict"] in ("YAP", "BEKLE") and len(dc["reasons"]) == 3
    assert {f["key"] for f in dc["factors"]} == set(rubric.FACTORS)
    ft = d["first_test"]
    assert ft["geo"] == "br" and ft["cpi_reference"]["value"] == 0.49
    assert ft["budget_usd"]["value"] == 100 and ft["budget_usd"]["low"] == 100  # 4 x 25 USD floor
    # files: TR and EN digests, facts, market_trends.yaml (and merged into the forge library)
    tr = Path(res.files["tr"]).read_text(encoding="utf-8")
    en = Path(res.files["en"]).read_text(encoding="utf-8")
    assert (
        "# Oyun dosyası · Pebble Sort" in tr
        and "Yapmalı mıyız?" in tr
        and "Neyi doğru yapıyor?" in tr
    )
    assert "Çekirdek döngü" in tr and "Bir tüpe dokunup" in tr and "robots.txt yasaklıyor" in tr
    assert "# Game dossier · Pebble Sort" in en and "What it does right" in en and "(LLM" in en
    assert "Rakip reklamları" in tr and "Example Pebbleworks Ltd: 42 reklam" in tr
    assert "Pebbleworks Garden Supplies" not in tr
    assert "son 30 günde gösterilen 25" in tr and "shown in the last 30 days 25" in en
    assert "Akranlara göre" in tr and "son güncellemeden bu yana gün: bu oyun 4" in tr
    assert "akranların medyanı 32,5 (n=6)" in tr and "Kanıt-destek denetimi" in tr
    assert "reklamlar şikâyet: bu oyunda 1/8 yorum" in tr
    doc = yaml.safe_load(Path(res.files["market_trends"]).read_text(encoding="utf-8"))
    les = doc["lessons"][0]
    assert doc["schema"] == "studio-pulse/market_trends/v1" and les["kind"] == "market_trend"
    assert les["id"] == f"DT-2026-11-14-apple-{PEBBLE}" and les["signal"] == "dossier_first_test"
    assert les["mechanic_shown"] == "color-sort" and les["test"]["ipm_gate"] == 8
    forge = yaml.safe_load(Path(res.files["creative_forge"]).read_text(encoding="utf-8"))
    assert [x["id"] for x in forge["lessons"]] == [les["id"]]
    # nothing personal: no developer e-mail or address, no reviewer names
    blob = "".join(
        Path(p).read_text(encoding="utf-8") for k, p in res.files.items() if k != "creative_forge"
    )
    for s in ("contact@example.invalid", "Example Street", "Example Player"):
        assert s not in blob
    # every Play and iTunes request was robots-checked first, nothing disallowed was sent
    con = warehouse.connect(cfg.warehouse, read_only=True)
    paths = [
        r[0] for r in con.execute("SELECT host || path FROM raw_mr_request ORDER BY ts").fetchall()
    ]
    con.close()
    assert "itunes.apple.com/robots.txt" in paths and "play.google.com/robots.txt" in paths
    assert not any("/rss/" in p for p in paths)


def test_second_run_reuses_caches(tmp_path):
    run(tmp_path)
    cfg, res, prov = run(tmp_path)
    assert prov.calls == []  # both LLM answers came from the content-hash cache
    assert res.facts["deconstruction"]["cached"]


def test_pvp_game_is_a_no_and_an_unknown_mechanic_waits(tmp_path):
    def arena_llm(req):
        if req.purpose != "dossier_deconstruct":
            return {"labels": []}
        q = "apple.description"
        return {
            **{k: [] for k in req.schema["required"]},
            "proposed_tags": [
                {
                    "name": "squad-pvp",
                    "why": "real-time squad battles",
                    "evidence": [{"source": q, "quote": "real-time multiplayer battles"}],
                }
            ],
            "art_style": [
                {
                    "kind": "realistic-3d",
                    "text": "Realistic 3D arenas.",
                    "text_tr": "Gerçekçi 3B arenalar.",
                    "evidence": [{"source": q, "quote": "realistic 3D arenas"}],
                }
            ],
        }

    cfg = make_cfg(
        tmp_path, dossier={"studio": {"avoid": ["squad-pvp", "pvp-race"]}, "countries": ["us"]}
    )
    client = LLMClient(FakeProvider(arena_llm), cache_dir=tmp_path / "c")
    res = build_dossier(
        cfg,
        ARENA,
        session=DossierReplaySession(FIX),
        day=DAY,
        llm_client=client,
        ads=False,
        sleep=lambda s: None,
        log=lambda m: None,
    )
    d = res.facts
    assert d["app"]["play_package"] is None and d["mechanic"]["value"] is None
    assert d["decision"]["verdict"] == "YAPMA" and d["decision"]["reasons"][0]["kind"] == "gate"
    assert any("squad-pvp" in g["en"] for g in d["decision"]["gates"])
    assert d["screenshots"] == [] and check_labels(d) == []
    assert "Karar YAPMA: bu test önerilmiyor" in Path(res.files["tr"]).read_text(encoding="utf-8")
    # the same game with no avoid list and no LLM: the mechanic is unknown, so at most BEKLE
    cfg2 = make_cfg(tmp_path, dossier={"countries": ["us"]})
    res2 = build_dossier(
        cfg2,
        ARENA,
        session=DossierReplaySession(FIX),
        day=DAY,
        use_llm=False,
        ads=False,
        sleep=lambda s: None,
        log=lambda m: None,
    )
    assert res2.facts["decision"]["verdict"] in ("BEKLE", "YAPMA")
    assert res2.facts["decision"]["capped"]


def test_rubric_weights_and_thresholds_come_from_the_config(tmp_path):
    cfg = make_cfg(
        tmp_path,
        dossier={
            "rubric": {
                "weights": {
                    "capability_fit": 1.0,
                    "momentum": 0,
                    "saturation": 0,
                    "monetization_fit": 0,
                    "production": 0,
                    "cpi_risk": 0,
                },
                "yap_min": 0.9,
            },
            "studio": {"strengths": ["color-sort"], "max_cpi_usd": 5.0},
            "countries": ["us"],
        },
    )
    client = LLMClient(FakeProvider(fake_llm), cache_dir=tmp_path / "c")
    d = build_dossier(
        cfg,
        PEBBLE,
        session=DossierReplaySession(FIX),
        day=DAY,
        llm_client=client,
        ads=False,
        sleep=lambda s: None,
        log=lambda m: None,
    ).facts
    assert d["decision"]["score"]["value"] == 1.0 and d["decision"]["verdict"] == "YAP"
    # a benchmark CPI over twice what we can carry caps the verdict at BEKLE
    cfg2 = make_cfg(
        tmp_path,
        dossier={
            "rubric": {
                "weights": {
                    "capability_fit": 1.0,
                    "momentum": 0,
                    "saturation": 0,
                    "monetization_fit": 0,
                    "production": 0,
                    "cpi_risk": 0,
                }
            },
            "studio": {"strengths": ["color-sort"], "max_cpi_usd": 2.0},
            "countries": ["us"],
        },
    )
    d2 = build_dossier(
        cfg2,
        PEBBLE,
        session=DossierReplaySession(FIX),
        day=DAY,
        llm_client=client,
        ads=False,
        sleep=lambda s: None,
        log=lambda m: None,
    ).facts
    assert d2["decision"]["verdict"] == "BEKLE" and "2.4x" in d2["decision"]["capped"][0]["en"]
    with pytest.raises(ConfigError, match="unknown keys"):
        from_config(make_cfg(tmp_path, dossier={"rubric": {"weights": {"vibes": 1}}}))


def test_out_dir_inside_the_public_demo_is_refused(tmp_path):
    cfg = make_cfg(tmp_path, out_dir="docs/demo/radar")
    with pytest.raises(ConfigError, match="docs/demo"):
        build_dossier(
            cfg, PEBBLE, session=ReplaySession(FIX, entries=[]), day=DAY, use_llm=False, ads=False
        )


# -- ads --------------------------------------------------------------------------------------------


def test_google_atc_patterns_are_sanitised():
    assert google_atc.name_pattern("Oakever Games Ltd.") == "oakever"
    assert google_atc.name_pattern("Dream Games Teknoloji Anonim Şirketi") == "dream"
    assert google_atc.name_pattern("Games Studio Ltd") is None
    assert google_atc.name_pattern("Example Person de Lima") == "example person"  # contiguous
    assert google_atc.name_pattern("x'); DROP TABLE t; --") == "drop table"
    assert google_atc.fold("Şirketi İstanbul Oyun A.Ş.") == "sirketi istanbul oyun a s"
    sql = google_atc.build_extract_sql(["oakever", "big sort"], DAY)
    assert "topic = 'Hobbies, Games & Leisure' OR REGEXP_CONTAINS(" in sql
    assert "r' (?:oakever|big sort) '" in sql and "'2026-10-15'" in sql  # 30 days back
    assert "TABLESAMPLE" not in sql and "advertiser_legal_name" not in sql
    with pytest.raises(ValueError):
        google_atc.build_extract_sql(["bad'quote"], DAY)
    with pytest.raises(ValueError):
        google_atc.build_extract_sql([], DAY, topic="x' OR 1=1 --")


def test_weekly_extract_dry_runs_first_stores_locally_and_logs_bytes(tmp_path):
    cfg = make_cfg(tmp_path)
    con = warehouse.connect(cfg.warehouse)
    try:
        pats = google_atc.developer_patterns(con)
        assert "big sort" in pats and "tiny dev" in pats and "other" not in pats  # specific only
    finally:
        con.close()
    bq = FakeBQ()
    res = refresh_extract(cfg.warehouse, bq)
    assert res["status"] == "ok" and res["advertisers"] == 2 and res["cap_gb"] == 30
    assert res["billed_gb"] == round((26_623_000_000 + 10_485_760) / 1e9, 3)
    # every job was dry-run before it ran; the extract scan ran once
    extract_jobs = [
        dry for sql, dry in bq.jobs if "creative_stats" in sql and "INFORMATION" not in sql
    ]
    assert extract_jobs == [True, False]
    con = warehouse.connect(cfg.warehouse)
    try:
        run_row = con.execute(
            "SELECT status, estimated_bytes, billed_bytes, cap_bytes, advertisers "
            "FROM raw_mr_adintel_run"
        ).fetchall()
        assert run_row == [("ok", 26_623_000_000, 26_633_485_760, 30_000_000_000, 2)]
        assert con.execute("SELECT COUNT(*) FROM raw_mr_adv_region").fetchone()[0] == 3
        # a week does not pass in a day: the next refresh is skipped, nothing is sent
        bq2 = FakeBQ()
        again = google_atc.refresh(
            con,
            project=None,
            s=google_atc.AdIntelSettings(),
            today=DAY,
            client=bq2,
            log=lambda m: None,
        )
        assert again["status"] == "fresh" and bq2.jobs == []
        found = google_atc.lookup_local(con, ["Example Pebbleworks Ltd", "Pebbleworks"], DAY)
        assert (
            found["patterns"] == ["pebbleworks"]
            and found["advertisers"][0]["games_creatives"] == 40
        )
        none = google_atc.lookup_local(con, ["Nobody Here Ltd"], DAY)
        assert none["status"] == "ok" and none["advertisers"] == []
    finally:
        con.close()


def test_weekly_extract_over_the_cap_is_refused_and_nothing_stored(tmp_path):
    cfg = make_cfg(tmp_path)
    bq = FakeBQ(extract_bytes=26_623_000_000)
    res = refresh_extract(cfg.warehouse, bq, cap_bytes=2_000_000_000)
    assert res["status"] == "refused" and res["billed_gb"] == 0.01  # the schema check only
    assert [dry for sql, dry in bq.jobs if "INFORMATION" not in sql] == [True]  # dry run only
    con = warehouse.connect(cfg.warehouse)
    try:
        assert google_atc.lookup_local(con, ["Pebbleworks"], DAY)["status"] == "no_extract"
    finally:
        con.close()
    with pytest.raises(ConfigError, match="max_gb"):
        google_atc.settings(make_cfg(tmp_path, adintel={"max_gb": 200}))


def test_pulse_run_hook_refreshes_weekly_only_when_enabled(tmp_path, monkeypatch):
    from studio_pulse import pipeline
    from studio_pulse.pipeline import IngestReport
    from studio_pulse.radar.dossier import run as dossier_run

    calls = []
    monkeypatch.setattr(radar_run, "run_in_pipeline", lambda con, cfg, log: [])
    # no credential lookup in a test (google.auth.default() may probe a metadata server)
    monkeypatch.setattr(dossier_run, "default_project", lambda: "synthetic-project")
    monkeypatch.setattr(
        google_atc, "refresh", lambda con, **kw: calls.append(kw) or {"status": "ok"}
    )
    cfg_off = make_cfg(tmp_path, enabled=True)
    con = warehouse.connect(cfg_off.warehouse)
    try:
        pipeline._radar(con, cfg_off, IngestReport(), log=lambda m: None)
        assert calls == []
        cfg_on = make_cfg(tmp_path, enabled=True, adintel={"enabled": True, "max_gb": 30})
        report = IngestReport()
        pipeline._radar(con, cfg_on, report, log=lambda m: None)
        assert len(calls) == 1 and calls[0]["s"].cap_bytes == 30_000_000_000
        assert calls[0]["project"] == "synthetic-project"
        monkeypatch.setattr(
            google_atc, "refresh", lambda con, **kw: {"status": "refused", "message": "cap"}
        )
        pipeline._radar(con, cfg_on, report, log=lambda m: None)
        assert "adintel" in report.errors  # a refused extract is visible, not silent
        # a fresh extract is not refreshed and no project is looked up
        con.execute("DELETE FROM raw_mr_adintel_run")
        store.insert(
            con,
            "raw_mr_adintel_run",
            [
                {
                    "run_at": dt.datetime(2026, 11, 13, 9),
                    "extract_date": DAY - dt.timedelta(days=1),
                    "status": "ok",
                }
            ],
        )
        monkeypatch.setattr(dossier_run, "default_project", lambda: pytest.fail("looked up"))
        report = IngestReport()
        pipeline._radar(con, cfg_on, report, log=lambda m: None)
        assert "adintel" not in report.errors
    finally:
        con.close()


def test_meta_ad_library_recorded_shape_not_run_live(tmp_path):
    assert (
        MetaAdLibrary(token="", today=DAY).search(search_terms="Pebble Sort")["status"]
        == "not_configured"
    )
    url = archive_url("v24.0", countries=["de", "fr"], search_terms="Pebble Sort")
    assert "ad_reached_countries=%5B%22DE%22%2C+%22FR%22%5D" in url and "access_token" not in url
    entries = [
        {
            "path": "/v24.0/ads_archive",
            "query": {"search_terms": "Pebble Sort", "ad_type": "ALL"},
            "file": "meta/ads_archive_p1.json",
        },
        {
            "path": "/v24.0/ads_archive",
            "query": {"after": "c2"},
            "file": "meta/ads_archive_p2.json",
        },
    ]
    sess = ReplaySession(FIX, entries=entries)
    out = MetaAdLibrary(token="SYNTHETIC-TOKEN-123", session=sess, today=DAY).search(
        search_terms="Pebble Sort"
    )
    assert (
        out["status"] == "ok"
        and out["ads"] == 3
        and out["pages_read"] == 2
        and not out["verified_live"]
    )
    p = out["pages"][0]
    assert (
        p["ads"] == 3
        and p["active"] == 2
        and p["platforms"]["instagram"] == 2
        and p["eu_reach"] == 43300
    )
    assert p["first_start"] == "2026-10-25" and p["last_start"] == "2026-11-09"
    assert "SYNTHETIC-TOKEN-123" not in json.dumps(out)
    assert all("access_token=SYNTHETIC-TOKEN-123" in r.url for r in sess.recorded)
    bad = ReplaySession(
        FIX,
        entries=[
            {
                "path": "/v24.0/ads_archive",
                "status": 400,
                "body": {"error": {"message": "Invalid OAuth access_token=SYNTHETIC-TOKEN-123"}},
            }
        ],
    )
    err = MetaAdLibrary(token="SYNTHETIC-TOKEN-123", session=bad, today=DAY).search(
        search_terms="x"
    )
    assert err["status"] == "error" and "SYNTHETIC-TOKEN-123" not in err["message"]


# -- page and CLI --------------------------------------------------------------------------------------


def test_a_failed_support_check_keeps_the_claims_and_says_so(tmp_path):
    from studio_pulse.llm import LLMError

    def flaky(req):
        if req.purpose == "dossier_support":
            raise LLMError("HTTP 400: synthetic failure")
        return fake_llm(req)

    cfg = make_cfg(tmp_path)
    client = LLMClient(FakeProvider(flaky), cache_dir=tmp_path / "c")
    d = build_dossier(
        cfg,
        PEBBLE,
        session=DossierReplaySession(FIX),
        day=DAY,
        llm_client=client,
        ads=False,
        sleep=lambda s: None,
        log=lambda m: None,
    ).facts
    dec = d["deconstruction"]
    assert dec["support"]["status"] == "error" and len(dec["art_style"]) == 2
    tr = render.render(d, "tr")
    assert "Kanıt-destek denetimi yapılamadı" in tr


def test_studio_profile_defaults_fit_a_small_casual_puzzle_studio(tmp_path):
    cfg = config_from_dict({"apps": [{"id": "x"}]}, base_dir=tmp_path)
    st = from_config(cfg).studio
    assert st["team_size"] == 2 and st["engine"] == "unity" and st["genres"] == ["puzzle", "casual"]
    assert {"color-sort", "goods-sort", "hexa-sort", "block-puzzle", "merge"} <= set(
        st["strengths"]
    )
    # max CPI: the hyper-casual benchmark of the first target market, not a guess
    assert st["max_cpi_usd"] == 1.55 and st["max_cpi_source"].startswith(
        "auto: hyper-casual CPI US"
    )
    own = from_config(make_cfg(tmp_path, dossier={"studio": {"max_cpi_usd": 2.5}})).studio
    assert own["max_cpi_usd"] == 2.5 and own["max_cpi_source"] == "config"


def test_peer_review_themes_compare_shares():
    themes = from_config(config_from_dict({"apps": [{"id": "x"}]}, base_dir=Path("."))).benchmarks[
        "review_themes"
    ]
    target = {
        "complaints": [{"theme": "ads", "count": 1}],
        "praise": [{"theme": "relaxing", "count": 3}],
    }
    pooled = {
        "complaints": [{"theme": "ads", "count": 9}],
        "praise": [{"theme": "relaxing", "count": 4}],
    }
    out = peers.compare_themes(target, 10, pooled, 30, themes)
    assert [(o["metric"], o["better"]) for o in out] == [
        ("review_complaints:ads", True),  # 10 % against 30 %
        ("review_praise:relaxing", True),  # 30 % against 13 %
    ]
    assert peers.compare_themes(target, 10, pooled, 0, themes) == []


def test_dossier_pages_in_pulse_serve(tmp_path):
    from fastapi.testclient import TestClient

    from studio_pulse.serve.app import create_app

    cfg, res, _ = run(tmp_path)
    client = TestClient(create_app(cfg, token="t"), base_url="http://127.0.0.1:8765")
    idx = client.get("/radar/dosya").text
    assert (
        "Oyun dosyaları" in idx and f"/radar/dosya/apple-{PEBBLE}" in idx and "Pebble Sort" in idx
    )
    page = client.get(f"/radar/dosya/apple-{PEBBLE}").text
    assert "Yapmalı mıyız?" in page and "Neyi doğru yapıyor?" in page and "(KURAL PUANI" in page
    assert "Bir tüpe dokunup" in page and "rahatlatıcı" in page and "(MAĞAZA VERİSİ)" in page
    en = client.get(f"/radar/dosya/apple-{PEBBLE}?lang=en").text
    assert "Should we make it?" in en and "What it does right" in en and "relaxing" in en
    shot = client.get(f"/radar/dosya/apple-{PEBBLE}/shot/s1")
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"
    assert client.get(f"/radar/dosya/apple-{PEBBLE}/shot/s7").status_code == 404
    assert client.get("/radar/dosya/apple-999999").status_code == 404
    assert client.get("/radar/dosya/..%2F..%2Fetc").status_code == 404
    assert "/radar/dosya" in client.get("/radar").text
    demo = Path(__file__).resolve().parents[1] / "docs" / "demo"
    assert not any(
        "dossier" in p.name.lower() or "dosya" in p.name.lower() for p in demo.rglob("*")
    )


def test_cli_prints_the_dossier_in_both_languages(tmp_path, capsys):
    make_cfg(tmp_path)
    cfg_path = tmp_path / "pulse.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "apps": [{"id": "x"}],
                "warehouse": str(tmp_path / "wh.duckdb"),
                "state_dir": str(tmp_path / "state"),
                "radar": {"countries": ["us"], "out_dir": str(tmp_path / "radar")},
                "llm": {"provider": "none"},
            }
        ),
        encoding="utf-8",
    )
    args = [
        "radar",
        "dossier",
        PEBBLE,
        "-c",
        str(cfg_path),
        "--replay",
        str(FIX),
        "--no-ads",
        "--date",
        "2026-11-14",
    ]
    assert cli.main([*args, "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Game dossier · Pebble Sort") and "No LLM deconstruction" in out
    assert "keyword" in out  # the mechanic falls back to the radar's keyword profile
    assert cli.main(args) == 0
    assert capsys.readouterr().out.startswith("# Oyun dosyası · Pebble Sort")
    assert cli.main(["radar", "dossier", "not a target", "-c", str(cfg_path)]) == 2
