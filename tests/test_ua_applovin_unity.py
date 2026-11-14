"""AppLovin (advertiser Reporting API) and Unity Ads (Statistics API v2) spend connectors.

Every test answers from recorded responses in the documented shapes
(tests/fixtures/ua/applovin, tests/fixtures/ua/unity_ads) or from a scripted fake session;
nothing leaves the machine and no real credential exists. Expected totals are summed by hand
from those files.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from pathlib import Path

import pytest
import requests
import yaml
from conftest import FIXTURES

from studio_pulse import pipeline, runner, warehouse
from studio_pulse.config import load_config
from studio_pulse.connectors.replay import ReplaySession
from studio_pulse.connectors.ua import applovin, common, report_client, rest, unity_ads

UA = FIXTURES / "ua"
START, END = dt.date(2026, 10, 1), dt.date(2026, 10, 10)
TODAY = dt.date(2026, 10, 12)
AL_KEY = "al-SECRET-report-key-0123456789"
UNITY_ID, UNITY_SECRET = "11111111-2222-3333-4444-555555555555", "unity-SECRET-key-abcdef"
ORG = "1234567890123"
NOLOG = lambda *_: None  # noqa: E731
NOSLEEP = lambda _s: None  # noqa: E731


def rows(con, where: str = "TRUE") -> list[dict]:
    cur = con.execute(
        f"SELECT * FROM raw_ua_spend WHERE {where} ORDER BY date, network, country, ad_id"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _resp(status: int, body: bytes | str = b"", headers: dict | None = None, url: str = ""):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode("utf-8") if isinstance(body, str) else body
    r.headers.update(headers or {})
    r.url = url
    return r


class Scripted:
    """A session that answers from a list, in order, and records every call."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def get(self, url, params=None, auth=None, headers=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False  # a redirect is never followed
        assert timeout is not None
        self.calls.append({"url": url, "params": dict(params or {}), "auth": auth})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def al_ingest(con, session, src=None, log=NOLOG, today=TODAY, sleep=NOSLEEP, start=START):
    return applovin.ingest(
        con,
        app_id="app",
        source="applovin_ads",
        src={"package_names": ["com.example.puzzle", "1234567890"], **(src or {})},
        start=start,
        end=END,
        session=session,
        log=log,
        today=today,
        sleep=sleep,
    )


# -- AppLovin ---------------------------------------------------------------------------


def test_applovin_pages_chunks_filters_and_maps_rows(con, monkeypatch):
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    session, log = ReplaySession(UA / "applovin"), []
    n = al_ingest(con, session, {"chunk_days": 5, "page_size": 2}, log=log.append)
    assert n == 3  # the other package's row is dropped
    r = rows(con)
    assert [x["network"] for x in r] == ["applovin"] * 3
    assert sum(x["spend"] for x in r) == pytest.approx(24.30 + 8.70 + 3.50)
    assert [x["country"] for x in r] == ["US", "DE", "GB"]
    assert [x["platform"] for x in r] == ["ANDROID", "ANDROID", "IOS"]
    assert [x["network_installs"] for x in r] == [30.0, 9.0, 2.0]  # `conversions`
    assert [x["impressions"] for x in r] == [12000, 4000, 1000]
    assert {x["currency"] for x in r} == {"USD"}
    assert r[0]["campaign_id"] == "c-al-1" and r[0]["campaign_name"] == "AL_Puzzle_US"
    assert r[0]["ad_id"] == "cs1" and r[0]["creative_id"] == "6453fdb366"  # creative set name
    assert r[2]["creative_id"] is None
    # two chunks of 5 days; the first chunk has two pages (limit 2, offset 0 then 2)
    q = [req.query for req in session.recorded]
    assert [(x["start"], x["end"], x["offset"]) for x in q] == [
        ("2026-10-01", "2026-10-05", "0"),
        ("2026-10-01", "2026-10-05", "2"),
        ("2026-10-06", "2026-10-10", "0"),
    ]
    first = q[0]
    assert first["report_type"] == "advertiser" and first["format"] == "json"
    assert first["columns"].split(",") == list(applovin.COLUMNS)
    assert "cost" in first["columns"] and "conversions" in first["columns"]
    assert "installs" not in first["columns"].split(",")  # only on /skaReport
    assert first["api_key"] == AL_KEY  # the API's only way to take the key
    assert all(AL_KEY not in line for line in log)
    # re-running the window replaces its slice
    al_ingest(con, ReplaySession(UA / "applovin"), {"chunk_days": 5, "page_size": 2})
    assert len(rows(con)) == 3


def test_applovin_window_is_45_days_and_older_days_are_kept(con, monkeypatch):
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    empty = '{"code": 200, "count": 0, "results": []}'
    # a 60-day lookback ending today: only the last 45 days are requested, in one chunk
    sess, log = Scripted(_resp(200, empty)), []
    applovin.ingest(
        con,
        app_id="app",
        source="applovin_ads",
        src={"lookback_days": 60},
        start=dt.date(2026, 8, 1),
        end=dt.date(2026, 9, 30),
        session=sess,
        today=dt.date(2026, 9, 30),
        sleep=NOSLEEP,
        log=log.append,
    )
    assert [(c["params"]["start"], c["params"]["end"]) for c in sess.calls] == [
        ("2026-08-17", "2026-09-30")
    ]
    assert any("last 45 days" in line for line in log)
    # rows already loaded for days the API no longer serves are not deleted
    al_ingest(con, ReplaySession(UA / "applovin"), {"chunk_days": 5, "page_size": 2})
    assert len(rows(con)) == 3
    sess = Scripted()
    n = al_ingest(con, sess, today=dt.date(2026, 12, 31))
    assert n == 0 and sess.calls == [] and len(rows(con)) == 3


def test_applovin_chunks_never_exceed_45_days():
    assert applovin.WINDOW_DAYS == 45
    sess = Scripted(*[_resp(200, '{"code": 200, "results": []}') for _ in range(3)])
    applovin.fetch_rows(
        {"chunk_days": 100},
        dt.date(2026, 1, 1),
        dt.date(2026, 3, 31),  # 90 days
        session=sess,
        key="k" * 10,
        redactor=common.Redactor(),
        sleep=NOSLEEP,
    )
    spans = [
        (dt.date.fromisoformat(c["params"]["end"]) - dt.date.fromisoformat(c["params"]["start"]))
        for c in sess.calls
    ]
    assert len(sess.calls) == 2 and all(s.days + 1 <= 45 for s in spans)


def test_applovin_empty_report_and_error_code(con, monkeypatch):
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    s = dt.date(2026, 9, 1)
    n = applovin.ingest(
        con,
        app_id="app",
        source="applovin_ads",
        src={"chunk_days": 45},
        start=s,
        end=dt.date(2026, 9, 10),
        session=ReplaySession(UA / "applovin"),
        today=TODAY,
        sleep=NOSLEEP,
        log=NOLOG,
    )
    assert n == 0
    with pytest.raises(common.UAError, match="API code 400: Invalid column") as exc:
        applovin.ingest(
            con,
            app_id="app",
            source="applovin_ads",
            src={},
            start=dt.date(2026, 8, 1),
            end=dt.date(2026, 8, 5),
            session=ReplaySession(UA / "applovin"),
            today=dt.date(2026, 8, 20),
            sleep=NOSLEEP,
            log=NOLOG,
        )
    assert AL_KEY not in str(exc.value)


def test_applovin_retries_429_with_retry_after(con, monkeypatch):
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    ok = (UA / "applovin" / "c2_p1.json").read_bytes()
    sess = Scripted(_resp(429, "slow down", {"Retry-After": "7"}), _resp(503, ""), _resp(200, ok))
    waits: list[float] = []
    n = al_ingest(con, sess, start=dt.date(2026, 10, 6), sleep=waits.append)
    assert n == 1 and len(sess.calls) == 3
    assert 7.0 in waits  # Retry-After honoured
    assert 4.0 in waits  # then exponential backoff (2 s x 2)


def test_applovin_401_names_the_variable_and_never_the_key(con, monkeypatch):
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    echo = (
        f'{{"code": 401, "message": "bad key for https://r.applovin.com/report?api_key={AL_KEY}"}}'
    )
    with pytest.raises(common.UAError) as exc:
        al_ingest(con, Scripted(_resp(401, echo)))
    text = str(exc.value)
    assert "HTTP 401 unauthorized" in text and "PULSE_APPLOVIN_REPORT_KEY" in text
    assert AL_KEY not in text and "***" in text
    assert exc.value.__cause__ is None
    assert len(rows(con)) == 0


def test_applovin_transport_error_never_shows_the_url(con, monkeypatch):
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    boom = requests.ConnectionError(
        f"HTTPSConnectionPool(host='r.applovin.com'): Max retries exceeded with url: "
        f"/report?api_key={AL_KEY}&report_type=advertiser"
    )
    with pytest.raises(common.UAError, match="ConnectionError from r.applovin.com") as exc:
        al_ingest(con, Scripted(*[boom] * 5))
    assert AL_KEY not in str(exc.value) and "api_key" not in str(exc.value)
    assert exc.value.__suppress_context__ and exc.value.__cause__ is None


def test_applovin_redirect_is_refused_not_followed(con, monkeypatch):
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    sess = Scripted(_resp(302, "", {"Location": "https://collector.example/x"}))
    with pytest.raises(common.UAError, match="redirect from r.applovin.com refused"):
        al_ingest(con, sess)
    assert len(sess.calls) == 1


def test_applovin_key_comes_from_the_environment_only(con, monkeypatch):
    monkeypatch.delenv("PULSE_APPLOVIN_REPORT_KEY", raising=False)
    with pytest.raises(common.MissingCredential, match="PULSE_APPLOVIN_REPORT_KEY"):
        al_ingest(con, None)
    for bad in ({"api_key": "x" * 20}, {"report_key": "x" * 20}):
        with pytest.raises(common.UAError, match="must not be in the config"):
            al_ingest(con, None, bad)


def test_urllib3_debug_line_has_the_key_masked():
    rec = logging.LogRecord(
        "urllib3.connectionpool",
        logging.DEBUG,
        __file__,
        1,
        '%s://%s:%s "%s %s %s" %s %s',
        (
            "https",
            "r.applovin.com",
            443,
            "GET",
            f"/report?api_key={AL_KEY}&x=1",
            "HTTP/1.1",
            200,
            9,
        ),
        None,
    )
    report_client.QueryKeyFilter().filter(rec)
    assert AL_KEY not in rec.getMessage() and "api_key=***&x=1" in rec.getMessage()


# -- Unity Ads --------------------------------------------------------------------------


def unity_ingest(con, session, src=None, start=START, sleep=NOSLEEP, log=NOLOG):
    return unity_ads.ingest(
        con,
        app_id="app",
        source="unity_ads",
        src={"organization_id": ORG, "chunk_days": 5, **(src or {})},
        start=start,
        end=END,
        session=session,
        log=log,
        sleep=sleep,
    )


@pytest.fixture()
def unity_env(monkeypatch):
    monkeypatch.setenv("PULSE_UNITY_ADS_KEY_ID", UNITY_ID)
    monkeypatch.setenv("PULSE_UNITY_ADS_SECRET_KEY", UNITY_SECRET)


def test_unity_csv_report_in_chunks_with_basic_auth(con, unity_env):
    session, log, waits = ReplaySession(UA / "unity_ads"), [], []
    n = unity_ingest(con, session, {"currency": "eur"}, sleep=waits.append, log=log.append)
    assert n == 3
    r = rows(con)
    assert [x["network"] for x in r] == ["unity_ads"] * 3
    assert sum(x["spend"] for x in r) == pytest.approx(10.10 + 2.02 + 7.5)
    assert [x["impressions"] for x in r] == [9000, 2000, 3000]  # starts
    assert [x["network_installs"] for x in r] == [20.0, 4.0, 5.0]
    assert [x["platform"] for x in r] == ["ANDROID", "ANDROID", "IOS"]
    assert {x["currency"] for x in r} == {"EUR"} and {x["account_id"] for x in r} == {ORG}
    assert r[0]["creative_id"] == "91c97d00dc" and r[1]["ad_name"] == "Pack, with comma"
    assert r[0]["campaign_id"] == "5f0000000000000000000001"
    first, second = session.recorded
    assert first.query["start"] == "2026-10-01T00:00:00.000Z"
    assert first.query["end"] == "2026-10-06T00:00:00.000Z"  # exclusive
    assert second.query["start"] == "2026-10-06T00:00:00.000Z"
    assert first.query["scale"] == "day" and first.query["format"] == "csv"
    assert first.query["eofMarker"] == "true"
    assert first.query["breakdowns"] == "campaign,creativePack,country,platform"
    assert first.query["metrics"] == "starts,clicks,installs,spend"
    want = "Basic " + base64.b64encode(f"{UNITY_ID}:{UNITY_SECRET}".encode()).decode()
    assert all(req.headers["Authorization"] == want for req in session.recorded)
    assert all(UNITY_SECRET not in req.url for req in session.recorded)
    assert any(w >= 1.0 for w in waits)  # 1 request per second
    assert all(UNITY_SECRET not in line for line in log)


def test_unity_204_is_no_data(con, unity_env):
    n = unity_ingest(con, ReplaySession(UA / "unity_ads"), {"organization_id": "1111"})
    assert n == 0 and rows(con) == []


@pytest.mark.parametrize(
    "org, message",
    [("2222", "partial download"), ("3333", "says 3 rows but 2 arrived")],
)
def test_unity_incomplete_csv_is_refused(con, unity_env, org, message):
    with pytest.raises(common.UAError, match=message):
        unity_ingest(con, ReplaySession(UA / "unity_ads"), {"organization_id": org})
    assert rows(con) == []


def test_unity_401_is_clear_and_carries_no_secret(con, unity_env):
    with pytest.raises(common.UAError) as exc:
        unity_ingest(con, ReplaySession(UA / "unity_ads"), {"organization_id": "4444"})
    text = str(exc.value)
    assert "HTTP 401 unauthorized" in text and "Invalid credentials" in text
    assert "PULSE_UNITY_ADS_KEY_ID" in text and "Advertise Stats API Viewer" in text
    basic = base64.b64encode(f"{UNITY_ID}:{UNITY_SECRET}".encode()).decode()
    assert UNITY_SECRET not in text and UNITY_ID not in text and basic not in text


def test_unity_rate_limit_retries_then_gives_up_on_a_long_wait(con, unity_env):
    ok = (UA / "unity_ads" / "c2.csv").read_bytes()
    problem = '{"title": "Too Many Requests", "status": 429, "code": 50}'
    sess, waits = Scripted(_resp(429, problem, {"Retry-After": "3"}), _resp(200, ok)), []
    n = unity_ingest(con, sess, {"chunk_days": 7}, start=dt.date(2026, 10, 4), sleep=waits.append)
    assert n == 1 and 3.0 in waits and len(sess.calls) == 2
    sess = Scripted(_resp(429, problem, {"Retry-After": "1500"}))
    with pytest.raises(common.UAError, match="asks to wait 1500 s") as exc:
        unity_ingest(con, sess, {"chunk_days": 7}, start=dt.date(2026, 10, 4))
    assert "Too Many Requests" in str(exc.value) and len(sess.calls) == 1


def test_unity_refuses_a_window_over_the_rate_limit_before_sending(con, unity_env):
    sess = Scripted()
    with pytest.raises(common.UAError, match="30 per 30 minutes"):
        unity_ingest(con, sess, {"chunk_days": 1, "lookback_days": 30}, start=dt.date(2026, 9, 1))
    assert sess.calls == []
    with pytest.raises(common.UAError, match="Organization core ID"):
        unity_ingest(con, sess, {"organization_id": "../../x"})


def test_unity_credentials_come_from_the_environment_only(con, monkeypatch):
    monkeypatch.setenv("PULSE_UNITY_ADS_KEY_ID", UNITY_ID)
    monkeypatch.delenv("PULSE_UNITY_ADS_SECRET_KEY", raising=False)
    with pytest.raises(common.MissingCredential, match="PULSE_UNITY_ADS_SECRET_KEY"):
        unity_ingest(con, None)
    for bad in ({"secret": "x" * 20}, {"key_id": "abc"}, {"secret_key": "y" * 20}):
        with pytest.raises(common.UAError, match="must not be in the config"):
            unity_ingest(con, None, bad)


# -- hosts --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://r.applovin.com/report",
        "https://r.applovin.com.evil.example/report",
        "https://r.applovin.com:8443/report",
        "https://user:pw@services.api.unity.com/x",
        "https://stats.unityads.unity3d.com/stats/v1",  # the deprecated v1 host
        "https://169.254.169.254/latest/meta-data/",
    ],
)
def test_ua_requests_only_go_to_the_api_hosts(url):
    sess = Scripted(_resp(200, "{}"))
    with pytest.raises(common.UAError, match="refused a request"):
        report_client.get(sess, url, params={}, what="t", redactor=common.Redactor())
    assert sess.calls == []
    with pytest.raises(common.UAError, match="refused a request"):
        rest.fetch(
            base_url=url,
            endpoint={"path": "x"},
            paginator=None,
            headers={},
            session=sess,
            redactor=common.Redactor(),
            what="t",
        )


def test_ua_host_allowlist_is_exact():
    assert {"r.applovin.com", "services.api.unity.com"} <= common.UA_HOSTS
    assert not any("*" in h for h in common.UA_HOSTS)
    for h in common.UA_HOSTS:
        assert common.check_host(f"https://{h}/x", "t")


def test_retry_after_accepts_seconds_and_dates():
    now = dt.datetime(2026, 11, 15, 12, 0, 0, tzinfo=dt.UTC)
    assert report_client.retry_after("12") == 12.0
    assert report_client.retry_after("Sun, 15 Nov 2026 12:00:30 GMT", now) == 30.0
    assert report_client.retry_after("soon") is None and report_client.retry_after(None) is None


# -- through the pipeline: the same tables as Meta, no dlt needed --------------------------


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
                    "applovin_ads": {
                        "package_names": ["com.example.puzzle", "1234567890"],
                        "chunk_days": 5,
                        "page_size": 2,
                        "replay": str(UA / "applovin"),
                    },
                    "unity_ads": {
                        "organization_id": ORG,
                        "chunk_days": 5,
                        "currency": "EUR",
                        "replay": str(UA / "unity_ads"),
                    },
                },
            }
        ],
    }
    p = tmp_path / "pulse.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_pipeline_lands_applovin_and_unity_next_to_meta(tmp_path, monkeypatch):
    for v in ("PULSE_APPLOVIN_REPORT_KEY", "PULSE_UNITY_ADS_KEY_ID", "PULSE_UNITY_ADS_SECRET_KEY"):
        monkeypatch.delenv(v, raising=False)  # a replay uses no credential
    cfg = load_config(ua_config(tmp_path))
    con = warehouse.connect(cfg.warehouse)
    try:
        rep = pipeline.ingest(con, cfg, log=NOLOG)
        assert not rep.errors, rep.errors
        assert rep.loaded == {"app.meta_ads": 3, "app.applovin_ads": 3, "app.unity_ads": 3}
        pipeline.ingest(con, cfg, log=NOLOG)  # idempotent
        assert con.execute("SELECT COUNT(*) FROM raw_ua_spend").fetchone()[0] == 9
        runner.run_models(con)
        got = dict(
            con.execute(
                "SELECT network, ROUND(SUM(spend_usd), 2) FROM stg_ua_spend GROUP BY 1"
            ).fetchall()
        )
        # Meta 49.75 EUR x 1.08; AppLovin 36.50 USD; Unity 19.62 EUR x 1.08
        assert got == {"meta": 53.73, "applovin": 36.5, "unity_ads": 21.19}
        daily = {r[0] for r in con.execute("SELECT DISTINCT network FROM fct_ua_daily").fetchall()}
        assert {"meta", "applovin", "unity_ads"} <= daily
        nets = con.execute(
            "SELECT network, attribution FROM meta_ua_networks ORDER BY 1"
        ).fetchall()
        assert nets == [
            ("applovin", "network_installs"),
            ("meta", "network_installs"),
            ("unity_ads", "network_installs"),
        ]
    finally:
        con.close()


def test_direct_sources_do_not_need_dlt(tmp_path, monkeypatch):
    monkeypatch.setattr(rest, "dlt_available", lambda: False)
    cfg = load_config(ua_config(tmp_path))
    con = warehouse.connect(cfg.warehouse)
    try:
        rep = pipeline.ingest(con, cfg, log=NOLOG)
    finally:
        con.close()
    assert set(rep.errors) == {"app.meta_ads"}
    assert rep.loaded["app.applovin_ads"] == 3 and rep.loaded["app.unity_ads"] == 3


def test_doctor_needs_both_unity_variables(tmp_path, monkeypatch):
    from studio_pulse import doctor

    p = ua_config(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    for k in ("applovin_ads", "unity_ads"):
        data["apps"][0]["sources"][k].pop("replay")
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("PULSE_APPLOVIN_REPORT_KEY", AL_KEY)
    monkeypatch.setenv("PULSE_UNITY_ADS_KEY_ID", UNITY_ID)
    monkeypatch.delenv("PULSE_UNITY_ADS_SECRET_KEY", raising=False)
    res = {r.name: r for r in doctor.check_api_sources(load_config(p))}
    assert res["app.applovin_ads"].status == "ok"
    assert res["app.unity_ads"].status == "fail"
    assert "PULSE_UNITY_ADS_SECRET_KEY" in res["app.unity_ads"].detail
    monkeypatch.setenv("PULSE_UNITY_ADS_SECRET_KEY", UNITY_SECRET)
    res = {r.name: r for r in doctor.check_api_sources(load_config(p))}
    assert res["app.unity_ads"].status == "ok"
    assert all(AL_KEY not in r.detail and UNITY_SECRET not in r.detail for r in res.values())
