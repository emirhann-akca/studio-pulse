"""Unity Ads ad spend: Advertising Statistics API v2, acquisitions report.

GET https://services.api.unity.com/advertise/stats/v2/organizations/{organizationId}/reports/acquisitions
(the v1 host stats.unityads.unity3d.com is deprecated). Read from the official reference
(docs.unity.com/en-us/oas-statistics/2.0.0 and its OpenAPI document, last updated 11 Aug
2026; read 15 Nov 2026):

- Auth: a service account with the "Advertise Stats API Viewer" role; its key ID and secret
  key as HTTP Basic credentials (the service-account page: Base64 of `<key id>:<secret>`,
  which is what requests' HTTPBasicAuth sends).
- `organizationId` is the "Organization core ID" (digits) from the UA dashboard's Settings.
- start (inclusive) and end (exclusive) are ISO 8601 date-times; days split at 00:00 UTC.
  scale=day, metrics=starts,clicks,installs,spend, breakdowns=campaign,creativePack,country,
  platform. `starts` are impressions ("Starts are also known as impressions").
- CSV is the default format; the split columns are "campaign id", "campaign name",
  "creative pack id", "creative pack name". eofMarker=true appends `#__EOF__,rows=N`: a
  response with data but without that row is a partial download and is refused.
- HTTP 204: no data for the filters. 429: rate limit, which is 1 request per second and 30
  requests per 30 minutes per organization and per IP; the Retry-After header is honoured.
- The guide recommends small date ranges for high-cardinality breakdowns such as country:
  the window is read in chunks of `chunk_days` (default 7), at least 1.1 s apart, and a
  window that would need more than 25 requests is refused up front.
- The currency of spend is not documented: taken as USD unless the source sets `currency:`.

Config:

    unity_ads:
      organization_id: "1234567890123"  # Organization core ID
      app_ids: [5fbfb6c7fc928924a8827e97]   # optional: only these advertised apps
      currency: USD
      key_id_env: PULSE_UNITY_ADS_KEY_ID
      secret_env: PULSE_UNITY_ADS_SECRET_KEY
      chunk_days: 7
      lookback_days: 30

Not verified against a live account: tested with recorded responses in the documented CSV
shape only.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import time
from collections.abc import Callable
from typing import Any

import duckdb

from . import common, report_client
from .creative import load_parser

HOST = "https://services.api.unity.com"
PATH = "/advertise/stats/v2/organizations/{org}/reports/acquisitions"
METRICS = ("starts", "clicks", "installs", "spend")
BREAKDOWNS = ("campaign", "creativePack", "country", "platform")
KEY_ID_ENV = "PULSE_UNITY_ADS_KEY_ID"
SECRET_ENV = "PULSE_UNITY_ADS_SECRET_KEY"
MAX_REQUESTS = 25  # of the 30 per 30 minutes the API allows
MIN_INTERVAL_S = 1.1  # 1 request per second
EOF_MARK = "#__EOF__"
_ORG = re.compile(r"^[0-9]+$")


def org_id(v: Any) -> str:
    s = str(v or "").strip()
    if not _ORG.match(s):
        raise common.UAError(
            f"unity_ads: organization_id must be the digits of the Organization core ID, got {v!r}"
        )
    return s


def parse_csv(text: str, what: str) -> list[dict[str, str]]:
    """Data rows of one CSV answer; refuses an answer without its end-of-file marker."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return []
    cols = [h.strip().lower() for h in header]
    missing = [c for c in ("timestamp", "spend") if c not in cols]
    if missing:
        raise common.UAError(f"{what}: CSV without {', '.join(missing)} column(s): {cols}")
    rows: list[dict[str, str]] = []
    marker: int | None = None
    for rec in reader:
        if not rec or not any(x.strip() for x in rec):
            continue
        if rec[0].strip() == EOF_MARK:
            m = re.match(r"rows=(\d+)", (rec[1] if len(rec) > 1 else "").strip())
            marker = int(m.group(1)) if m else -1
            break
        rows.append({c: (rec[i].strip() if i < len(rec) else "") for i, c in enumerate(cols)})
    if rows and marker is None:
        raise common.UAError(f"{what}: partial download (no {EOF_MARK} marker); not loaded")
    if marker is not None and marker != len(rows):
        raise common.UAError(
            f"{what}: the end-of-file marker says {marker} rows but {len(rows)} arrived"
        )
    return rows


def to_row(d: dict[str, str], org: str) -> dict[str, Any]:
    return {
        "date": dt.date.fromisoformat(d["timestamp"][:10]),
        "account_id": org,
        "campaign_id": d.get("campaign id") or None,
        "campaign_name": d.get("campaign name") or None,
        "adset_id": None,
        "adset_name": None,
        "ad_id": d.get("creative pack id") or None,
        "ad_name": d.get("creative pack name") or None,
        "country": d.get("country"),
        "platform": d.get("platform"),
        "spend": common.num(d.get("spend")),
        "currency": None,
        "impressions": common.as_int(d.get("starts")),
        "clicks": common.as_int(d.get("clicks")),
        "network_installs": common.num(d.get("installs")),
    }


def fetch_rows(
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    *,
    session: Any,
    auth: Any,
    redactor: common.Redactor,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    org = org_id(src.get("organization_id"))
    chunk = int(src.get("chunk_days", 7))
    if not 1 <= chunk <= 31:
        raise common.UAError("unity_ads: chunk_days must be between 1 and 31")
    windows = list(common.chunks(start, end, chunk))
    if len(windows) > MAX_REQUESTS:
        raise common.UAError(
            f"unity_ads: {start}..{end} in {chunk}-day chunks needs {len(windows)} requests; "
            f"the API allows 30 per 30 minutes (raise chunk_days or lower lookback_days)"
        )
    url = HOST + PATH.format(org=org)
    hint = (
        f"check ${src.get('key_id_env', KEY_ID_ENV)} / ${src.get('secret_env', SECRET_ENV)} "
        "and that the service account has the Advertise Stats API Viewer role"
    )
    pacer = report_client.Pacer(MIN_INTERVAL_S, sleep=sleep)
    apps = src.get("app_ids") or []
    rows: list[dict[str, Any]] = []
    for since, until in windows:
        params: dict[str, Any] = {
            "start": f"{since.isoformat()}T00:00:00.000Z",
            "end": f"{(until + dt.timedelta(days=1)).isoformat()}T00:00:00.000Z",
            "scale": "day",
            "metrics": ",".join(METRICS),
            "breakdowns": ",".join(BREAKDOWNS),
            "format": "csv",
            "eofMarker": "true",
        }
        if apps:
            params["appIds"] = ",".join(str(a).strip() for a in apps)
        what = f"Unity Ads acquisitions {since}..{until}"
        r = report_client.get(
            session,
            url,
            params=params,
            what=what,
            redactor=redactor,
            auth=auth,
            auth_hint=hint,
            timeout=(15.0, 600.0),  # the API's documented maximum request duration: 10 minutes
            pacer=pacer,
            sleep=sleep,
            max_wait_s=float(src.get("max_wait_s", 120)),
        )
        if r.status_code == 204:  # no data for these filters
            continue
        text = r.content.decode("utf-8-sig", errors="replace")
        for d in parse_csv(text, what):
            if not d.get("timestamp"):
                continue
            row = to_row(d, org)
            if since <= row["date"] <= until:
                rows.append(row)
    return rows


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    session: Any = None,
    creative_parser: str | None = None,
    log: Any = print,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    common.reject_inline_secrets(src, "unity_ads")
    start, end = common.ua_window(start, end, src)
    redactor = common.Redactor()
    if session is None and src.get("replay"):
        from ..replay import ReplaySession

        session, auth = ReplaySession(src["replay"]), ("replay-key-id", "replay-secret")
        sleep = lambda _s: None  # noqa: E731 - nothing is sent in a replay
    else:
        import base64

        import requests
        from requests.auth import HTTPBasicAuth

        key_id = common.env_secret(str(src.get("key_id_env", KEY_ID_ENV)), "unity_ads")
        secret = common.env_secret(str(src.get("secret_env", SECRET_ENV)), "unity_ads")
        basic = base64.b64encode(f"{key_id}:{secret}".encode()).decode("ascii")
        for s in (key_id, secret, basic):
            redactor.add(s)
        auth = HTTPBasicAuth(key_id, secret)
        session = session or requests.Session()
    rows = fetch_rows(src, start, end, session=session, auth=auth, redactor=redactor, sleep=sleep)
    rows = common.finish_rows(
        rows,
        network="unity_ads",
        start=start,
        end=end,
        parser=load_parser(src.get("creative_parser", creative_parser)),
        default_platform=src.get("platform"),
        default_currency=src.get("currency", "USD"),
    )
    return common.load(
        con,
        app_id=app_id,
        source=source,
        rows=rows,
        start=start,
        end=end,
        detail="unity ads acquisitions, creative pack x country x day",
        log=log,
    )
