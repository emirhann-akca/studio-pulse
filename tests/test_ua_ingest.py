"""UA spend ingestion: Meta, Google Ads, TikTok (dlt rest_api) and CSV exports.

Every API test replays recorded responses (tests/fixtures/ua/<network>/,
documented response shapes; no live account was available), so no request
leaves the machine. Expected totals are summed by hand from those files.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES

from studio_pulse import pipeline, runner, warehouse
from studio_pulse.config import load_config
from studio_pulse.connectors.replay import ReplayError, ReplaySession
from studio_pulse.connectors.ua import common, creative, csv_import, google_ads, meta, rest, tiktok

pytest.importorskip("dlt")

UA = FIXTURES / "ua"
START, END = dt.date(2026, 10, 1), dt.date(2026, 10, 10)
CF_A = "cf1.puzzle-ads.dust-blower.main.main.main.9x16.en.6453fdb366"
CF_B = "cf1.puzzle-ads.dust-blower.blower.lvl21.fill.9x16.tr.91c97d00dc"
META_TOKEN = "tok-SECRET-meta-0123456789"


def rows(con, where: str = "TRUE") -> list[dict]:
    cur = con.execute(
        f"SELECT * FROM raw_ua_spend WHERE {where} ORDER BY date, network, country, ad_id"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


# -- creative ids ------------------------------------------------------------------


def test_cf1_ids_match_creative_forge():
    # Ids computed with creative-forge's own naming.CreativeIds (elite-creative branch).
    assert creative.cf1_id("puzzle-ads", "dust-blower", "main", "main", "main", "9x16", "en") == (
        "6453fdb366"
    )
    assert creative.cf1_name("merge-ads", "coin-rain", "fast", "lvl3", "tap", "4x5", "pt-br") == (
        "cf1.merge-ads.coin-rain.fast.lvl3.tap.4x5.pt-br.e49be7e60a"
    )
    hit = creative.parse_cf1(f"Copy of {CF_A.upper()} - Copy (2)")
    assert hit and hit["creative_id"] == "6453fdb366" and hit["concept"] == "dust-blower"
    # a hand-edited name whose id no longer matches its parts is not ours
    assert creative.parse_cf1(CF_A.replace(".en.", ".tr.")) is None
    assert creative.parse_cf1("Summer promo v3") is None and creative.parse_cf1("") is None


def test_parser_is_pluggable():
    assert creative.load_parser(None) is creative.parse_cf1
    assert creative.load_parser("none") is None
    rx = creative.load_parser(r"regex:^(?P<concept>[a-z]+)_(?P<creative_id>c\d+)")
    assert rx("hero_c12_v2") == {"concept": "hero", "creative_id": "c12"}
    assert rx("nothing here") is None
    fn = creative.load_parser("studio_pulse.connectors.ua.creative:parse_cf1")
    assert fn(CF_B)["creative_id"] == "91c97d00dc"
    with pytest.raises(ValueError, match="creative_id"):
        creative.load_parser("regex:(?P<id>x)")
    with pytest.raises(ValueError):
        creative.load_parser("not-a-spec")
    data = [{"ad_name": CF_A}, {"ad_name": "other"}, {"ad_name": None}]
    creative.annotate(data, creative.parse_cf1)
    assert [d["creative_id"] for d in data] == ["6453fdb366", None, None]
    assert data[0]["creative_concept"] == "dust-blower"


# -- Meta --------------------------------------------------------------------------


def test_meta_paginates_strips_token_and_maps_rows(con, monkeypatch):
    monkeypatch.setenv("PULSE_META_ACCESS_TOKEN", META_TOKEN)
    session, log = ReplaySession(UA / "meta"), []
    src = {"account_id": "act_1234567890", "platform": "android"}
    n = meta.ingest(
        con,
        app_id="app",
        source="meta_ads",
        src=src,
        start=START,
        end=END,
        session=session,
        log=log.append,
    )
    assert n == 3  # the 2026-11-05 row is outside the window
    r = rows(con)
    assert [x["spend"] for x in r] == [7.25, 12.5, 30.0]
    assert sum(x["spend"] for x in r) == pytest.approx(49.75)
    assert {x["currency"] for x in r} == {"EUR"} and {x["platform"] for x in r} == {"ANDROID"}
    # installs: mobile_app_install wins over app_install; omni only when nothing else
    assert [x["network_installs"] for x in r] == [4.0, 10.0, 18.0]
    assert [x["country"] for x in r] == ["DE", "US", "US"]
    assert r[0]["creative_id"] == "6453fdb366" and r[2]["creative_id"] is None
    # two 7-day chunks x two pages; the token travels only as a header
    assert len(session.recorded) == 4
    for req in session.recorded:
        assert META_TOKEN not in req.url and "access_token" not in req.url
        assert req.headers["Authorization"] == f"Bearer {META_TOKEN}"
    assert session.recorded[1].query["after"] == "c2"
    assert all(META_TOKEN not in line for line in log)
    # re-running the window replaces its slice
    meta.ingest(
        con,
        app_id="app",
        source="meta_ads",
        src=src,
        start=START,
        end=END,
        session=ReplaySession(UA / "meta"),
        log=log.append,
    )
    assert len(rows(con)) == 3


def test_meta_error_is_redacted(con, monkeypatch):
    monkeypatch.setenv("PULSE_META_ACCESS_TOKEN", META_TOKEN)
    with pytest.raises(common.UAError) as exc:
        meta.ingest(
            con,
            app_id="app",
            source="meta_ads",
            src={"account_id": "999"},
            start=START,
            end=END,
            session=ReplaySession(UA / "meta"),
            log=lambda *_: None,
        )
    text = str(exc.value) + repr(exc.value.__cause__) + repr(exc.value.__context__)
    assert "400" in text and META_TOKEN not in text and "***" in text
    assert exc.value.__suppress_context__  # the original exception (with the URL) is not chained


def test_credentials_come_from_env_only(con, monkeypatch):
    monkeypatch.delenv("PULSE_META_ACCESS_TOKEN", raising=False)
    with pytest.raises(common.MissingCredential, match="PULSE_META_ACCESS_TOKEN"):
        meta.ingest(
            con,
            app_id="app",
            source="meta_ads",
            src={"account_id": "1"},
            start=START,
            end=END,
            log=lambda *_: None,
        )
    for kind, mod, extra in (
        ("meta_ads", meta, {"account_id": "1"}),
        ("tiktok_ads", tiktok, {"advertiser_id": "1"}),
        ("google_ads", google_ads, {"customer_id": "1"}),
    ):
        with pytest.raises(common.UAError, match="must not be in the config"):
            mod.ingest(
                con,
                app_id="app",
                source=kind,
                src={**extra, "access_token": "x" * 20},
                start=START,
                end=END,
                log=lambda *_: None,
            )


# -- Google Ads --------------------------------------------------------------------


def test_google_ads_gaql_pages_and_headers(con, monkeypatch):
    monkeypatch.setenv("PULSE_GOOGLE_ADS_ACCESS_TOKEN", "ya29.SECRET-google-token")
    monkeypatch.delenv("PULSE_GOOGLE_ADS_DEVELOPER_TOKEN", raising=False)
    session, log = ReplaySession(UA / "google_ads"), []
    src = {
        "customer_id": "123-456-7890",
        "login_customer_id": "111-222-3333",
        "quota_project": "studio-ads-project",
        "platform": "ANDROID",
    }
    n = google_ads.ingest(
        con,
        app_id="app",
        source="google_ads",
        src=src,
        start=START,
        end=END,
        session=session,
        log=log.append,
    )
    assert n == 3
    r = rows(con)
    assert sum(x["spend"] for x in r) == pytest.approx(15 + 4.56 + 1.234567)
    assert [x["network_installs"] for x in r] == [12.0, 5.5, 1.0]
    assert {x["country"] for x in r} == {None}  # ad_group_ad has no country segment
    assert r[0]["creative_id"] == "91c97d00dc" and r[1]["ad_name"] == ""
    first, second = session.recorded
    assert first.method == "POST" and "pageToken" not in first.body
    assert "FROM ad_group_ad" in first.body["query"]
    assert "segments.date BETWEEN '2026-10-01' AND '2026-10-10'" in first.body["query"]
    assert second.body["pageToken"] == "t2"
    h = first.headers
    assert h["Authorization"] == "Bearer ya29.SECRET-google-token"
    assert (
        h["login-customer-id"] == "1112223333" and h["x-goog-user-project"] == "studio-ads-project"
    )
    assert "developer-token" not in h  # retired 9 Sep 2026; sent only when the env var is set
    assert all("SECRET" not in line for line in log)


def test_google_ads_sends_developer_token_only_from_env(monkeypatch):
    monkeypatch.setenv("PULSE_GOOGLE_ADS_DEVELOPER_TOKEN", "dev-tok-123")
    assert google_ads.headers({}, "t")["developer-token"] == "dev-tok-123"
    with pytest.raises(common.UAError, match="10 digits"):
        google_ads.customer("12a")


# -- TikTok ------------------------------------------------------------------------


def test_tiktok_pages_by_page_number(con, monkeypatch):
    monkeypatch.setenv("PULSE_TIKTOK_ACCESS_TOKEN", "tt-SECRET-access-token")
    session = ReplaySession(UA / "tiktok")
    n = tiktok.ingest(
        con,
        app_id="app",
        source="tiktok_ads",
        src={"advertiser_id": "7000000000000000001"},
        start=START,
        end=END,
        session=session,
        log=lambda *_: None,
    )
    assert n == 3
    r = rows(con)
    assert sum(x["spend"] for x in r) == pytest.approx(36.61)
    assert [x["country"] for x in r] == ["BR", "US", "US"]
    assert {x["creative_id"] for x in r} == {"0371245c35", "e49be7e60a"}
    q = session.recorded[0].query
    assert q["report_type"] == "AUDIENCE" and q["data_level"] == "AUCTION_AD"
    assert q["dimensions"] == '["ad_id", "stat_time_day", "country_code"]'
    assert session.recorded[1].query["page"] == "2"
    assert all(req.headers["Access-Token"] == "tt-SECRET-access-token" for req in session.recorded)
    assert all("SECRET" not in req.url for req in session.recorded)


def test_tiktok_error_code_in_a_200_response_fails(con, monkeypatch):
    monkeypatch.setenv("PULSE_TIKTOK_ACCESS_TOKEN", "tt-SECRET-access-token")
    with pytest.raises(common.UAError, match="40105") as exc:
        tiktok.ingest(
            con,
            app_id="app",
            source="tiktok_ads",
            src={"advertiser_id": "7000000000000000099"},
            start=START,
            end=END,
            session=ReplaySession(UA / "tiktok"),
            log=lambda *_: None,
        )
    assert "SECRET" not in str(exc.value)
    assert len(rows(con)) == 0


def test_replay_never_falls_through_to_the_network():
    s = ReplaySession(UA / "tiktok")
    with pytest.raises(ReplayError):
        s.get("https://business-api.tiktok.com/open_api/v1.3/other/")


# -- CSV ---------------------------------------------------------------------------


def test_csv_exports_of_three_networks(con):
    log: list[str] = []
    n = csv_import.ingest(
        con,
        app_id="app",
        source="ua_csv",
        src={"network": "applovin"},
        path=UA / "csv" / "applovin_202610.csv",
        log=log.append,
    )
    assert n == 2  # the Total line is skipped
    al = rows(con, "network = 'applovin'")
    assert sum(x["spend"] for x in al) == pytest.approx(33.0)
    assert al[0]["country"] == "US" and al[0]["platform"] == "ANDROID"
    assert al[0]["creative_id"] == "6453fdb366" and al[0]["ad_id"] == "cs1"
    csv_import.ingest(
        con,
        app_id="app",
        source="unity_csv",
        src={"network": "unity_ads"},
        path=UA / "csv" / "unity_ads_202610.csv",
        log=log.append,
    )
    un = rows(con, "network = 'unity_ads'")
    assert [x["impressions"] for x in un] == [9000, 2000]  # "Starts" = ad starts
    assert un[0]["ad_name"] == "Pack Ş-01"  # UTF-16 decoded
    csv_import.ingest(
        con,
        app_id="app",
        source="mg_csv",
        src={"network": "mintegral", "date_format": "%m/%d/%Y", "currency": "usd"},
        path=UA / "csv" / "mintegral_202610.csv",
        log=log.append,
    )
    mg = rows(con, "network = 'mintegral'")
    assert mg[0]["date"] == dt.date(2026, 10, 1) and mg[0]["spend"] == 6.66
    assert mg[0]["currency"] == "USD" and mg[0]["campaign_name"] == "MG_Puzzle"
    # re-importing a file replaces exactly its rows
    csv_import.ingest(
        con,
        app_id="app",
        source="ua_csv",
        src={"network": "applovin"},
        path=UA / "csv" / "applovin_202610.csv",
        log=log.append,
    )
    assert len(rows(con)) == 5


def test_csv_column_errors_are_explained(tmp_path: Path):
    f = tmp_path / "x.csv"
    f.write_text("When,Money\n2026-10-01,3\n", encoding="utf-8")
    with pytest.raises(common.UAError, match="no column for date, spend"):
        csv_import.parse_csv(f.read_text(encoding="utf-8"), {})
    got = csv_import.parse_csv(
        f.read_text(encoding="utf-8"), {"columns": {"date": "When", "spend": "Money"}}
    )
    assert got[0]["spend"] == 3.0
    with pytest.raises(common.UAError, match="date_format"):
        csv_import.parse_csv("date,spend\n10/01/2026,1\n", {})


# -- through the pipeline: FX, idempotency, dry run, missing extra --------------------


def ua_config(tmp_path: Path) -> Path:
    cfg = {
        "warehouse": "wh.duckdb",
        "lookback_days": 10,
        "end_date": END.isoformat(),
        "fx_rates": {"EUR": 1.08},
        "apps": [
            {
                "id": "app",
                "sources": {
                    "meta_ads": {"account_id": "1234567890", "replay": str(UA / "meta")},
                    "google_ads": {"customer_id": "1234567890", "replay": str(UA / "google_ads")},
                    "tiktok_ads": {
                        "advertiser_id": "7000000000000000001",
                        "replay": str(UA / "tiktok"),
                    },
                    "ua_csv": {
                        "network": "applovin",
                        "path": str(UA / "csv" / "applovin_202610.csv"),
                    },
                },
            }
        ],
    }
    p = tmp_path / "pulse.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_pipeline_spend_totals_to_the_cent_and_idempotent(tmp_path):
    cfg = load_config(ua_config(tmp_path))
    con = warehouse.connect(cfg.warehouse)
    try:
        rep = pipeline.ingest(con, cfg, log=lambda *_: None)
        assert not rep.errors, rep.errors
        first = con.execute("SELECT COUNT(*) FROM raw_ua_spend").fetchone()[0]
        pipeline.ingest(con, cfg, log=lambda *_: None)
        assert con.execute("SELECT COUNT(*) FROM raw_ua_spend").fetchone()[0] == first == 11
        runner.run_models(con)
        got = dict(
            con.execute(
                "SELECT network, ROUND(SUM(spend_usd), 2) FROM stg_ua_spend GROUP BY 1"
            ).fetchall()
        )
        # Meta 49.75 EUR x 1.08; Google 20.794567; TikTok 36.61; AppLovin 33.00
        assert got == {"meta": 53.73, "google_ads": 20.79, "tiktok": 36.61, "applovin": 33.0}
        nets = con.execute(
            "SELECT network, attribution, ga4_sources FROM meta_ua_networks ORDER BY 1"
        ).fetchall()
        assert nets == [
            ("applovin", "network_installs", []),
            ("google_ads", "ga4", ["google"]),
            ("meta", "network_installs", []),
            ("tiktok", "network_installs", []),
        ]
    finally:
        con.close()


def test_dry_run_sends_nothing(tmp_path):
    cfg = load_config(ua_config(tmp_path))
    con = warehouse.connect(cfg.warehouse)
    try:
        rep = pipeline.ingest(con, cfg, dry_run=True, log=lambda *_: None)
        assert set(rep.loaded.values()) == {"skipped (dry run)"}
        assert con.execute("SELECT COUNT(*) FROM raw_ua_spend").fetchone()[0] == 0
    finally:
        con.close()


def test_missing_dlt_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rest, "dlt_available", lambda: False)
    cfg = load_config(ua_config(tmp_path))
    con = warehouse.connect(cfg.warehouse)
    try:
        rep = pipeline.ingest(con, cfg, log=lambda *_: None)
    finally:
        con.close()
    assert set(rep.errors) == {"app.meta_ads", "app.google_ads", "app.tiktok_ads"}
    assert all("studio-pulse[ua]" in e for e in rep.errors.values())
    assert rep.loaded["app.ua_csv"] == 2  # CSV needs no extra


def test_second_source_of_a_type_with_a_suffix(tmp_path):
    p = ua_config(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    srcs = data["apps"][0]["sources"]
    srcs["ua_csv@unity"] = {
        "network": "unity_ads",
        "path": str(UA / "csv" / "unity_ads_202610.csv"),
    }
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_config(p)
    con = warehouse.connect(cfg.warehouse)
    try:
        rep = pipeline.ingest(con, cfg, sources=["ua_csv"], log=lambda *_: None)
        assert set(rep.loaded) == {"app.ua_csv", "app.ua_csv@unity"}
        assert con.execute(
            "SELECT _source, COUNT(*) FROM raw_ua_spend GROUP BY 1 ORDER BY 1"
        ).fetchall() == [("ua_csv", 2), ("ua_csv@unity", 2)]
    finally:
        con.close()
