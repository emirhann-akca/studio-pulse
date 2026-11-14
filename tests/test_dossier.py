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
from studio_pulse.radar.dossier import collect, deconstruct, reviews, rubric
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


def fake_llm(req):
    """Schema-valid answers (what a provider with a response schema can return)."""
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
    """Answers the schema query and the advertiser query; records every job."""

    def __init__(self, advertiser_bytes: int, rows: list[dict] | None = None) -> None:
        self.advertiser_bytes = advertiser_bytes
        self.rows = rows or []
        self.jobs: list[tuple[str, bool]] = []

    def query(self, sql, job_config=None):
        dry = bool(getattr(job_config, "dry_run", False))
        self.jobs.append((sql, dry))
        if "INFORMATION_SCHEMA" in sql:
            paths = sorted(google_atc.EXPECTED | {"topic"})
            return FakeJob(0, 10_485_760, pa.table({"field_path": paths}))
        if dry:
            return FakeJob(self.advertiser_bytes, None, None)
        return FakeJob(
            self.advertiser_bytes, self.advertiser_bytes, pa.Table.from_pylist(self.rows)
        )


ATC_ROWS = [
    {
        "advertiser_id": "AR000000000000000001",
        "name": "Example Pebbleworks Ltd",
        "creatives": 42,
        "video": 30,
        "image": 12,
        "text": 0,
        "region_code": "US",
        "region_creatives": 40,
        "first_shown": "2026-10-22",
        "last_shown": "2026-11-13",
    },
    {
        "advertiser_id": "AR000000000000000001",
        "name": "Example Pebbleworks Ltd",
        "creatives": 42,
        "video": 30,
        "image": 12,
        "text": 0,
        "region_code": "DE",
        "region_creatives": 18,
        "first_shown": "2026-10-25",
        "last_shown": "2026-11-12",
    },
]


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


def run(tmp_path, target=PEBBLE, *, client=True, bq=None, meta=None, **kw):
    cfg = make_cfg(tmp_path)
    prov = FakeProvider(fake_llm)
    llm = LLMClient(prov, cache_dir=tmp_path / "llmcache", budget_usd=0.5) if client else None
    s = from_config(cfg)
    atc = google_atc.GoogleAdsTransparency(
        project=None,
        cap_bytes=s.ads_max_bytes,
        client=bq or FakeBQ(9_100_000_000),
        cache_dir=tmp_path / "atc",
        log=lambda m: None,
        today=DAY,
    )
    res = build_dossier(
        cfg,
        target,
        session=DossierReplaySession(FIX),
        day=DAY,
        llm_client=llm,
        use_llm=client,
        atc=atc,
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
    # ads: BigQuery refused by the dry run (nothing run), Meta not configured
    g = d["ads"]["google_atc"]
    assert g["status"] == "refused" and g["estimate_gb"] == 9.1 and g["patterns"] == ["pebbleworks"]
    assert d["ads"]["meta"]["status"] == "not_configured"
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
    assert "Rakip reklamları" in tr and "9,10 GB" in tr and "9.10 GB" in en
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


def test_google_atc_patterns_are_sanitised_and_batched():
    assert google_atc.name_pattern("Oakever Games Ltd.") == "oakever"
    assert google_atc.name_pattern("Dream Games Teknoloji Anonim Şirketi") == "dream"
    assert google_atc.name_pattern("Games Studio Ltd") is None
    assert google_atc.name_pattern("Example Person de Lima") == "example person"  # contiguous
    assert google_atc.name_pattern("x'); DROP TABLE t; --") == "drop table"
    sql = google_atc.build_query(["oakever", "dream"])
    assert sql.count("LIKE") == 2 and "creative_stats" in sql and "TABLESAMPLE" not in sql
    with pytest.raises(ValueError):
        google_atc.build_query(["bad'quote"])


def test_google_atc_dry_runs_first_and_refuses_over_the_cap(tmp_path):
    bq = FakeBQ(advertiser_bytes=23_570_000_000)
    atc = google_atc.GoogleAdsTransparency(
        project=None,
        cap_bytes=2_000_000_000,
        client=bq,
        cache_dir=tmp_path,
        log=lambda m: None,
        today=DAY,
    )
    res = atc.lookup(["Example Pebbleworks Ltd"])
    assert res.status == "refused" and res.estimate_bytes == 23_570_000_000
    adv = [
        (sql, dry)
        for sql, dry in bq.jobs
        if "creative_stats" in sql and "INFORMATION_SCHEMA" not in sql
    ]
    assert adv == [(adv[0][0], True)]  # the advertiser query was only dry-run
    assert atc.billed_bytes() == 10_485_760  # only the schema check ran (BigQuery's 10 MB minimum)


def test_google_atc_runs_under_the_cap_and_caches(tmp_path):
    bq = FakeBQ(advertiser_bytes=1_500_000_000, rows=ATC_ROWS)
    kw = dict(
        project=None, cap_bytes=2_000_000_000, cache_dir=tmp_path, log=lambda m: None, today=DAY
    )
    res = google_atc.GoogleAdsTransparency(client=bq, **kw).lookup(["Example Pebbleworks Ltd"])
    assert res.status == "ok" and res.billed_bytes == 1_500_000_000
    a = res.advertisers[0]
    assert (
        a["creatives"] == 42
        and a["formats"]["video"] == 30
        and [r["region"] for r in a["regions"]] == ["US", "DE"]
    )
    assert (
        a["first_shown"] == "2026-10-22"
        and a["last_shown"] == "2026-11-13"
        and a["days_since_last_shown"] == 1
    )
    real = [j for j in bq.jobs if not j[1] and "INFORMATION_SCHEMA" not in j[0]]
    assert len(real) == 1
    bq2 = FakeBQ(advertiser_bytes=1_500_000_000, rows=ATC_ROWS)
    again = google_atc.GoogleAdsTransparency(client=bq2, **kw).lookup(["Example Pebbleworks Ltd"])
    assert again.cached and again.advertisers == res.advertisers and bq2.jobs == []


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
