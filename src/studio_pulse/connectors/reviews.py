"""Store reviews and Android vitals.

Google Play reviews, two ways:
  * monthly CSV exports from the Play Console's Cloud Storage bucket
    (`gs://pubsite_prod_rev_<id>/reviews/reviews_<package>_YYYYMM.csv`, UTF-16),
    the durable source: one file per month, re-importing a month replaces
    exactly that month (source `play_reviews`, `path:` a folder or gs:// URI);
  * the Play Developer API `reviews.list`, which only returns reviews created or
    modified in the last week (source `play_reviews` with `api: true`); good for
    yesterday's reviews before the month's CSV is updated.
App Store Connect: `GET /v1/apps/{id}/customerReviews` (source
`app_store_reviews`), newest first, paged by `links.next` and stopped at the
start of the window.

Reviewer names (Play `authorName`, App Store `reviewerNickname`) are never
stored. Both stores' reviews land in raw_store_reviews; stg_store_reviews keeps
one row per (app, store, review_id), the most recently updated.

Android vitals (source `android_vitals`): Play Developer Reporting API v1beta1
`apps/{package}/{metricSet}:query` with a DAILY timeline (whose only supported
time zone is America/Los_Angeles), metric sets anrRateMetricSet (anrRate,
userPerceivedAnrRate, distinctUsers; checked in the API reference) and
crashRateMetricSet (crashRate, userPerceivedCrashRate, distinctUsers; same
shape, not separately verified).

Credentials: Google sources use $PULSE_PLAY_ACCESS_TOKEN or Application Default
Credentials (scopes androidpublisher / playdeveloperreporting); App Store
Connect uses an API key from the environment (PULSE_ASC_KEY_ID,
PULSE_ASC_ISSUER_ID and PULSE_ASC_PRIVATE_KEY or PULSE_ASC_PRIVATE_KEY_PATH,
signed as an ES256 JWT with pyjwt, extra `stores`) or a ready token in
$PULSE_ASC_TOKEN. Every source can replay recorded responses (`replay:`), which
is how the demo and the tests run; none was verified against a live account.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
import time
import zoneinfo
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import duckdb

from ..warehouse import record_load, replace_slice
from .stores import read_text
from .ua.common import Redactor

PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
ASC_API = "https://api.appstoreconnect.apple.com/v1"
REPORTING_API = "https://playdeveloperreporting.googleapis.com/v1beta1"
VITALS = {
    "anrRateMetricSet": ("anrRate", "userPerceivedAnrRate", "distinctUsers"),
    "crashRateMetricSet": ("crashRate", "userPerceivedCrashRate", "distinctUsers"),
}


class ReviewsError(RuntimeError):
    pass


def _tz(name: str) -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(name or "UTC")


def _clip(text: Any, n: int = 2000) -> str | None:
    s = str(text or "").strip()
    return s[:n] if s else None


# -- Play CSV -----------------------------------------------------------------------


def _review_id(link: str | None, *parts: Any) -> str:
    if link:
        q = parse_qs(urlsplit(link).query)
        if q.get("reviewId"):
            return q["reviewId"][0]
    return "h:" + hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:20]


def _millis(v: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(v or "") / 1000, dt.UTC).replace(tzinfo=None)
    except ValueError:
        return None


def _iso(v: str | None) -> dt.datetime | None:
    if not v:
        return None
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(dt.UTC).replace(tzinfo=None) if t.tzinfo else t


def parse_play_reviews_csv(text: str, package: str | None, tz: str = "UTC") -> list[dict[str, Any]]:
    """Rows of a reviews_<package>_YYYYMM.csv export (Play Console column names)."""
    zone = _tz(tz)
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        if package and row.get("Package Name") and row["Package Name"] != package:
            continue
        submitted = _millis(row.get("Review Submit Millis Since Epoch")) or _iso(
            row.get("Review Submit Date and Time")
        )
        updated = _millis(row.get("Review Last Update Millis Since Epoch")) or _iso(
            row.get("Review Last Update Date and Time")
        )
        when = updated or submitted
        rating = row.get("Star Rating")
        out.append(
            {
                "review_id": _review_id(
                    row.get("Review Link"),
                    row.get("Review Submit Millis Since Epoch"),
                    row.get("Device"),
                    row.get("Review Text"),
                ),
                "submitted_at": submitted,
                "updated_at": updated,
                "date": when.replace(tzinfo=dt.UTC).astimezone(zone).date() if when else None,
                "rating": int(rating) if rating and rating.isdigit() else None,
                "title": _clip(row.get("Review Title"), 300),
                "body": _clip(row.get("Review Text")),
                "language": row.get("Reviewer Language") or None,
                "app_version": row.get("App Version Name") or None,
                "device": row.get("Device") or None,
                "has_reply": bool(row.get("Developer Reply Text")),
                "origin": "play_csv",
            }
        )
    return [r for r in out if r["date"] is not None and r["rating"] is not None]


# -- Play API -----------------------------------------------------------------------


def google_token(scope: str, env: str = "PULSE_PLAY_ACCESS_TOKEN") -> str:
    tok = os.environ.get(env, "").strip()
    if tok:
        return tok
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError as exc:
        raise ReviewsError(f"set {env} or install the gcp extra for ADC") from exc
    try:
        creds, _ = google.auth.default(scopes=[scope])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as exc:
        raise ReviewsError(
            f"no usable Application Default Credentials ({type(exc).__name__})"
        ) from None
    return creds.token


def _get(session: Any, url: str, redactor: Redactor, what: str, **kw: Any) -> Any:
    try:
        r = session.request(kw.pop("method", "GET"), url, timeout=60, **kw)
    except Exception as exc:
        raise ReviewsError(f"{what}: {redactor.clean(exc)}") from None
    if r.status_code >= 400:
        raise ReviewsError(f"{what}: HTTP {r.status_code}: {redactor.clean(r.text[:300])}")
    return r.json()


def fetch_play_api(
    package: str, session: Any, token: str, redactor: Redactor, tz: str = "UTC"
) -> list[dict[str, Any]]:
    zone, out, page = _tz(tz), [], None
    for _ in range(100):
        params: dict[str, Any] = {"maxResults": 100}
        if page:
            params["token"] = page
        body = _get(
            session,
            f"{PLAY_API}/applications/{package}/reviews",
            redactor,
            "Play reviews.list",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        for rv in body.get("reviews", []):
            user = next(
                (c["userComment"] for c in rv.get("comments", []) if "userComment" in c), None
            )
            if not user:
                continue
            reply = any("developerComment" in c for c in rv.get("comments", []))
            secs = (user.get("lastModified") or {}).get("seconds")
            when = dt.datetime.fromtimestamp(int(secs), dt.UTC) if secs else None
            text = user.get("text") or ""
            title, _, rest = text.partition("\t")  # the API joins title and text with a tab
            out.append(
                {
                    "review_id": rv["reviewId"],
                    "submitted_at": None,
                    "updated_at": when.replace(tzinfo=None) if when else None,
                    "date": when.astimezone(zone).date() if when else None,
                    "rating": user.get("starRating"),
                    "title": _clip(title, 300) if rest else None,
                    "body": _clip(rest if rest else text),
                    "language": user.get("reviewerLanguage"),
                    "app_version": user.get("appVersionName"),
                    "device": user.get("device"),
                    "has_reply": reply,
                    "origin": "play_api",
                }
            )
        page = (body.get("tokenPagination") or {}).get("nextPageToken")
        if not page:
            break
    return [r for r in out if r["date"] is not None]


# -- App Store Connect ----------------------------------------------------------------


def asc_token() -> str:
    tok = os.environ.get("PULSE_ASC_TOKEN", "").strip()
    if tok:
        return tok
    kid, iss = os.environ.get("PULSE_ASC_KEY_ID"), os.environ.get("PULSE_ASC_ISSUER_ID")
    key = os.environ.get("PULSE_ASC_PRIVATE_KEY")
    if not key and os.environ.get("PULSE_ASC_PRIVATE_KEY_PATH"):
        key = Path(os.environ["PULSE_ASC_PRIVATE_KEY_PATH"]).read_text(encoding="utf-8")
    if not (kid and iss and key):
        raise ReviewsError(
            "App Store Connect: set PULSE_ASC_KEY_ID, PULSE_ASC_ISSUER_ID and "
            "PULSE_ASC_PRIVATE_KEY(_PATH), or PULSE_ASC_TOKEN"
        )
    try:
        import jwt
    except ImportError as exc:
        raise ReviewsError(
            'signing an App Store Connect token needs: pip install "studio-pulse[stores]"'
        ) from exc
    now = int(time.time())
    return jwt.encode(
        {"iss": iss, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
        key,
        algorithm="ES256",
        headers={"kid": kid, "typ": "JWT"},
    )


def fetch_asc(
    apple_id: str, start: dt.date, session: Any, token: str, redactor: Redactor, tz: str = "UTC"
) -> list[dict[str, Any]]:
    if not str(apple_id).isdigit():
        raise ReviewsError(f"app_store_reviews: apple_id must be digits, got {apple_id!r}")
    zone, out = _tz(tz), []
    url: str | None = f"{ASC_API}/apps/{apple_id}/customerReviews"
    params: dict[str, Any] | None = {
        "sort": "-createdDate",
        "limit": 200,
        "fields[customerReviews]": "rating,title,body,createdDate,territory",
    }
    for _ in range(500):
        if not url:
            break
        body = _get(
            session,
            url,
            redactor,
            "App Store customerReviews",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        params = None  # links.next carries the query
        stop = False
        for item in body.get("data", []):
            a = item.get("attributes") or {}
            created = _iso(a.get("createdDate"))
            if created is None:
                continue
            day = created.replace(tzinfo=dt.UTC).astimezone(zone).date()
            if day < start:
                stop = True
                continue
            out.append(
                {
                    "review_id": str(item.get("id")),
                    "submitted_at": created,
                    "updated_at": created,
                    "date": day,
                    "rating": a.get("rating"),
                    "title": _clip(a.get("title"), 300),
                    "body": _clip(a.get("body")),
                    "territory": a.get("territory"),
                    "origin": "asc_api",
                }
            )
        url = None if stop else (body.get("links") or {}).get("next")
    return out


# -- vitals -----------------------------------------------------------------------


def _date_obj(d: dt.date) -> dict[str, Any]:
    return {
        "year": d.year,
        "month": d.month,
        "day": d.day,
        "timeZone": {"id": "America/Los_Angeles"},
    }


def fetch_vitals(
    package: str, start: dt.date, end: dt.date, session: Any, token: str, redactor: Redactor
) -> list[dict[str, Any]]:
    out = []
    for metric_set, metrics in VITALS.items():
        page = None
        for _ in range(50):
            body: dict[str, Any] = {
                "timelineSpec": {
                    "aggregationPeriod": "DAILY",
                    "startTime": _date_obj(start),
                    "endTime": _date_obj(end + dt.timedelta(days=1)),
                },
                "metrics": list(metrics),
                "pageSize": 1000,
            }
            if page:
                body["pageToken"] = page
            res = _get(
                session,
                f"{REPORTING_API}/apps/{package}/{metric_set}:query",
                redactor,
                f"Play vitals {metric_set}",
                method="POST",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            for row in res.get("rows", []):
                st = row.get("startTime") or {}
                day = dt.date(int(st["year"]), int(st["month"]), int(st["day"]))
                for m in row.get("metrics", []):
                    v = (m.get("decimalValue") or {}).get("value")
                    out.append(
                        {
                            "date": day,
                            "metric_set": metric_set,
                            "metric": m.get("metric"),
                            "value": float(v) if v is not None else None,
                        }
                    )
            page = res.get("nextPageToken")
            if not page:
                break
    return [r for r in out if start <= r["date"] <= end]


# -- ingest -----------------------------------------------------------------------


def _session(src: dict[str, Any], base: Any) -> Any:
    if src.get("replay"):
        from .replay import ReplaySession

        return ReplaySession(src["replay"])
    if base is not None:
        return base
    import requests

    return requests.Session()


def ingest_play(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    src: dict[str, Any],
    path: Path | None,
    start: dt.date,
    end: dt.date,
    timezone: str = "UTC",
    session: Any = None,
    log: Any = print,
) -> int:
    package = src.get("package")
    n = 0
    if path is not None:
        files = [path] if path.is_file() else sorted(path.rglob("reviews_*.csv"))
        for f in files:
            rows = parse_play_reviews_csv(read_text(f), package, timezone)
            n += replace_slice(
                con,
                "raw_store_reviews",
                rows,
                app_id=app_id,
                source=source,
                where="_file = ?",
                params=[f.name],
                constants={"store": "google_play", "_file": f.name},
            )
        log(f"    {source:<13} -> raw_store_reviews      {n:>7} rows from {len(files)} CSV file(s)")
    if src.get("api"):
        if not package:
            raise ReviewsError("play_reviews: api: true needs package")
        redactor = Redactor()
        token = (
            "replay"
            if src.get("replay")
            else google_token("https://www.googleapis.com/auth/androidpublisher")
        )
        redactor.add(token)
        rows = fetch_play_api(package, _session(src, session), token, redactor, timezone)
        week = end - dt.timedelta(days=7)
        rows = [r for r in rows if r["date"] > week]
        k = replace_slice(
            con,
            "raw_store_reviews",
            rows,
            app_id=app_id,
            source=f"{source}:api",
            where="date > ?",
            params=[week],
            constants={"store": "google_play", "_file": "api"},
        )
        log(f"    {source:<13} -> raw_store_reviews      {k:>7} rows from the API (last 7 days)")
        n += k
    record_load(con, app_id=app_id, source=source, table="raw_store_reviews", rows=n)
    return n


def ingest_asc(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    timezone: str = "UTC",
    session: Any = None,
    log: Any = print,
) -> int:
    redactor = Redactor()
    token = "replay" if src.get("replay") else asc_token()
    redactor.add(token)
    rows = fetch_asc(
        str(src.get("apple_id", "")), start, _session(src, session), token, redactor, timezone
    )
    rows = [r for r in rows if start <= r["date"] <= end]
    n = replace_slice(
        con,
        "raw_store_reviews",
        rows,
        app_id=app_id,
        source=source,
        where="date BETWEEN ? AND ?",
        params=[start, end],
        constants={"store": "app_store", "_file": "api"},
    )
    record_load(con, app_id=app_id, source=source, table="raw_store_reviews", rows=n)
    log(f"    {source:<13} -> raw_store_reviews      {n:>7} rows")
    return n


def ingest_vitals(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    session: Any = None,
    log: Any = print,
) -> int:
    package = src.get("package")
    if not package:
        raise ReviewsError("android_vitals: set package")
    redactor = Redactor()
    token = (
        "replay"
        if src.get("replay")
        else google_token("https://www.googleapis.com/auth/playdeveloperreporting")
    )
    redactor.add(token)
    rows = fetch_vitals(package, start, end, _session(src, session), token, redactor)
    n = replace_slice(
        con,
        "raw_android_vitals",
        rows,
        app_id=app_id,
        source=source,
        where="date BETWEEN ? AND ?",
        params=[start, end],
    )
    record_load(con, app_id=app_id, source=source, table="raw_android_vitals", rows=n)
    log(f"    {source:<13} -> raw_android_vitals     {n:>7} rows")
    return n


__all__ = ["ingest_asc", "ingest_play", "ingest_vitals", "parse_play_reviews_csv"]
