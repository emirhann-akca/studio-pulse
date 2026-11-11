"""Store reviews (Play CSV + API, App Store Connect), rating models and Android vitals.

Fixtures: the same Play reviews export as UTF-16 (as Play writes it) and UTF-8,
recorded API responses in the documented shapes. No live call is made.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from conftest import FIXTURES

from studio_pulse import runner, warehouse
from studio_pulse.connectors import reviews
from studio_pulse.connectors.stores import read_text

R = FIXTURES / "reviews"
CSV = "reviews_com.example.game_202610.csv"


def test_utf16_and_utf8_exports_parse_to_identical_rows():
    a = reviews.parse_play_reviews_csv(read_text(R / "play_utf16" / CSV), "com.example.game")
    b = reviews.parse_play_reviews_csv(read_text(R / "play_utf8" / CSV), "com.example.game")
    assert a == b and len(a) == 3  # the other package's row is skipped
    assert [r["review_id"] for r in a] == ["gp:AAA1", "gp:AAA2", "gp:AAA3"]
    assert a[1]["body"] == 'Oyun sürekli çöküyor, "çok" kötü' and a[1]["has_reply"] is True
    assert a[0]["submitted_at"] == dt.datetime(2026, 10, 1, 22, 30)
    assert [r["rating"] for r in a] == [5, 1, 3] and a[2]["title"] == "Ads"


def test_review_day_is_in_the_apps_time_zone():
    rows = reviews.parse_play_reviews_csv(read_text(R / "play_utf8" / CSV), None, "Europe/Istanbul")
    assert rows[0]["date"] == dt.date(2026, 10, 2)  # 22:30 UTC is 01:30 in Istanbul
    rows = reviews.parse_play_reviews_csv(read_text(R / "play_utf8" / CSV), None, "UTC")
    assert rows[0]["date"] == dt.date(2026, 10, 1)


def load_all(con, tmp_path):
    start, end = dt.date(2026, 9, 1), dt.date(2026, 10, 10)
    log: list[str] = []
    reviews.ingest_play(
        con,
        app_id="g",
        source="play_reviews",
        src={"package": "com.example.game", "api": True, "replay": str(R / "play_api")},
        path=R / "play_utf16",
        start=start,
        end=end,
        log=log.append,
    )
    reviews.ingest_asc(
        con,
        app_id="g",
        source="app_store_reviews",
        src={"apple_id": "1234567890", "replay": str(R / "asc")},
        start=start + dt.timedelta(days=29),
        end=end,
        timezone="America/Los_Angeles",
        log=log.append,
    )
    reviews.ingest_vitals(
        con,
        app_id="g",
        source="android_vitals",
        src={"package": "com.example.game", "replay": str(R / "vitals")},
        start=start,
        end=end,
        log=log.append,
    )
    runner.run_models(con)
    return log


def test_csv_and_api_merge_into_one_row_per_review(con, tmp_path):
    load_all(con, tmp_path)
    got = con.execute(
        "SELECT review_id, rating, origin, app_version FROM stg_store_reviews "
        "WHERE store = 'google_play' ORDER BY review_id"
    ).fetchall()
    # AAA2 was edited on Oct 4 (API, 2 stars): the later edit wins over the CSV row
    assert got == [
        ("gp:AAA1", 5, "play_csv", "1.2.0"),
        ("gp:AAA2", 2, "play_api", "1.3.0"),
        ("gp:AAA3", 3, "play_csv", "1.3.0"),
        ("gp:AAA4", 5, "play_api", "1.3.0"),
    ]
    title, body = con.execute(
        "SELECT title, body FROM stg_store_reviews WHERE review_id = 'gp:AAA4'"
    ).fetchone()
    assert (title, body) == ("Great", "Great game, fun levels")  # the API's title<TAB>text


def test_no_reviewer_names_are_stored(con, tmp_path):
    load_all(con, tmp_path)
    rows = con.execute("SELECT * FROM raw_store_reviews").fetchall()
    text = json.dumps([list(map(str, r)) for r in rows], ensure_ascii=False)
    for name in ("Private Person", "Another Person", "nick1", "nick2"):
        assert name not in text


def test_app_store_pages_stop_at_the_window(con, tmp_path):
    load_all(con, tmp_path)
    got = con.execute(
        "SELECT review_id, date, rating, territory FROM stg_store_reviews WHERE store = 'app_store' "
        "ORDER BY date"
    ).fetchall()
    # rev-4 (Sep 20) is before the window start (Sep 30); dates are Los Angeles days
    assert got == [
        ("rev-3", dt.date(2026, 10, 1), 4, "TUR"),
        ("rev-2", dt.date(2026, 10, 5), 2, "DEU"),
        ("rev-1", dt.date(2026, 10, 9), 5, "USA"),
    ]


def test_rating_models_by_hand(con, tmp_path):
    load_all(con, tmp_path)
    day = dict(
        con.execute(
            "SELECT store || ':' || date, avg_rating FROM fct_review_daily WHERE app_id = 'g'"
        ).fetchall()
    )
    assert day["google_play:2026-10-01"] == 5.0
    assert day["all:2026-10-04"] == pytest.approx((2 + 5) / 2)  # AAA2 now dated by its edit
    ver = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT app_version, reviews, avg_rating, low_star_share FROM fct_rating_by_version"
        ).fetchall()
    }
    assert ver["1.2.0"] == (1, 5.0, 0.0)
    assert ver["1.3.0"][0] == 3 and ver["1.3.0"][1] == pytest.approx(10 / 3)
    topics = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT topic, reviews_30d, share_30d FROM fct_review_topics"
        ).fetchall()
    }
    # 7 reviews in the last 30 days: ads (AAA3), crashes (AAA2 "crashing"),
    # performance (rev-2 "Slow loading and lag"), praise (AAA1, AAA4, rev-1, rev-3 "Fun")
    assert topics["ads"][0] == 1 and topics["crashes_bugs"][0] == 1
    assert topics["performance"][0] == 1 and topics["praise"][0] == 4
    assert topics["praise"][1] == pytest.approx(4 / 7)


def test_turkish_keywords_match(con):
    warehouse.replace_slice(
        con,
        "raw_store_reviews",
        [
            {
                "store": "google_play",
                "review_id": "t1",
                "date": dt.date(2026, 10, 1),
                "rating": 1,
                "body": "Oyun sürekli çöküyor ve reklam çok",
            },
            {
                "store": "google_play",
                "review_id": "t2",
                "date": dt.date(2026, 10, 1),
                "rating": 2,
                "body": "Çok pahalı, abonelik iptal",
            },
        ],
        app_id="g",
        source="x",
    )
    runner.run_models(con)
    got = {
        r[0]: r[1]
        for r in con.execute("SELECT topic, reviews_30d FROM fct_review_topics").fetchall()
    }
    assert got == {"crashes_bugs": 1, "ads": 1, "pricing": 1}


def test_vitals_pivot(con, tmp_path):
    load_all(con, tmp_path)
    got = con.execute(
        "SELECT date, crash_rate, user_perceived_crash_rate, anr_rate, user_perceived_anr_rate, "
        "distinct_users FROM fct_android_vitals_daily ORDER BY date"
    ).fetchall()
    assert got == [
        (dt.date(2026, 10, 1), 0.008, 0.0071, 0.0031, 0.0024, 1200.0),
        (dt.date(2026, 10, 2), 0.012, 0.0112, 0.0062, 0.0051, 1180.0),
    ]


def test_reimporting_a_month_replaces_only_that_slice(con, tmp_path):
    d = tmp_path / "play"
    d.mkdir()
    (d / CSV).write_bytes((R / "play_utf8" / CSV).read_bytes())
    sept = (
        (R / "play_utf8" / CSV)
        .read_text(encoding="utf-8-sig")
        .replace("AAA", "SEP")
        .replace("2026-10-0", "2026-09-0")
        .replace("1790", "1788")
    )
    (d / "reviews_com.example.game_202609.csv").write_text(sept, encoding="utf-8")
    kw = dict(
        app_id="g",
        source="play_reviews",
        src={"package": "com.example.game"},
        start=dt.date(2026, 9, 1),
        end=dt.date(2026, 10, 31),
        log=lambda *_: None,
    )
    assert reviews.ingest_play(con, path=d, **kw) == 6
    # October's file is re-exported with one review fewer: only October changes
    lines = (d / CSV).read_text(encoding="utf-8-sig").splitlines()
    (d / CSV).write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
    assert reviews.ingest_play(con, path=d, **kw) == 5
    files = dict(con.execute("SELECT _file, COUNT(*) FROM raw_store_reviews GROUP BY 1").fetchall())
    assert files == {CSV: 2, "reviews_com.example.game_202609.csv": 3}


def test_asc_token_needs_env_and_never_logs_it(con, monkeypatch, tmp_path):
    for k in (
        "PULSE_ASC_TOKEN",
        "PULSE_ASC_KEY_ID",
        "PULSE_ASC_ISSUER_ID",
        "PULSE_ASC_PRIVATE_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(reviews.ReviewsError, match="PULSE_ASC_KEY_ID"):
        reviews.asc_token()
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    monkeypatch.setenv("PULSE_ASC_KEY_ID", "KEY123")
    monkeypatch.setenv("PULSE_ASC_ISSUER_ID", "issuer-uuid")
    monkeypatch.setenv("PULSE_ASC_PRIVATE_KEY", pem)
    tok = reviews.asc_token()
    claims = jwt.decode(tok, key.public_key(), algorithms=["ES256"], audience="appstoreconnect-v1")
    assert claims["iss"] == "issuer-uuid" and claims["exp"] - claims["iat"] == 1200
    assert jwt.get_unverified_header(tok)["kid"] == "KEY123"
    # an API error does not echo the token
    from studio_pulse.connectors.replay import ReplaySession

    s = ReplaySession(
        tmp_path,
        entries=[
            {
                "method": "GET",
                "path": "/v1/apps/1/customerReviews",
                "status": 401,
                "body": {"errors": [{"detail": tok}]},
            }
        ],
    )
    red = reviews.Redactor(tok)
    with pytest.raises(reviews.ReviewsError) as exc:
        reviews.fetch_asc("1", dt.date(2026, 10, 1), s, tok, red)
    assert tok not in str(exc.value) and "401" in str(exc.value)
    assert s.recorded[0].headers["Authorization"] == f"Bearer {tok}"
