"""AppLovin (AppDiscovery / Axon) ad spend: the advertiser Reporting API.

GET https://r.applovin.com/report with report_type=advertiser, the Report Key as `api_key`,
start / end (YYYY-MM-DD), `columns`, format=json, and limit / offset for pages. Read from
the official page (developers.axon.ai/en/app-discovery/api/reporting-api, which now
redirects to support.applovin.com/en/app-discovery/api/reporting-api; read 15 Nov 2026):

- "This API has a request window of 45 days. Make sure the date parameters are in the last
  45 days." The window is requested in chunks of at most 45 days, and days older than 45
  days before today (UTC) are not requested at all: those keep the rows of earlier runs.
- "All data in the reporting API is in UTC."
- Columns used (advertiser table): day, campaign, campaign_id_external,
  campaign_package_name, campaign_store_id, platform, country, creative_set,
  creative_set_id, impressions, clicks, conversions ("Number of conversions (installs)"),
  cost ("Advertiser spend"). The current page's advertiser table lacks `impressions` but its
  own advertiser example request (and the legacy page's table) has it. There is no ad id:
  the creative set (`creative_set_id`, stable across renames) is the ad of this network,
  as in the CSV import; `installs` exists only on the separate SKAdNetwork report.
- The page shows no response body. The JSON envelope `{"code": 200, "count": n,
  "results": [...]}` is the one AppLovin documents for its sibling /maxReport endpoint; a
  `code` other than 200 is raised. No currency is documented: spend is taken as USD unless
  the source sets `currency:`. No rate limit is documented: pages are spaced by 1 s and a
  429 / 5xx is retried with backoff.

The key travels in the query string because that is the API's only way to take it. The URL
is never logged or put into an error (report_client.get), urllib3's DEBUG line has the key
masked, and the key is redacted from any text the API sends back.

Config (an app's `sources:`):

    applovin_ads:
      package_names: [com.example.puzzle, "1234567890"]  # campaign_package_name or
                                                        # campaign_store_id to keep (a key
                                                        # covers every app of the account)
      currency: USD                   # optional; the API does not say
      token_env: PULSE_APPLOVIN_REPORT_KEY
      lookback_days: 30
      page_size: 10000

Credential: the account's Report Key (dashboard: Account > Keys), in the environment variable
named by token_env. Not verified against a live account: tested with recorded responses in
the documented shape only.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any

import duckdb

from . import common, report_client
from .creative import load_parser

HOST = "https://r.applovin.com"
URL = f"{HOST}/report"
WINDOW_DAYS = 45
COLUMNS = (
    "day",
    "campaign",
    "campaign_id_external",
    "campaign_package_name",
    "campaign_store_id",
    "platform",
    "country",
    "creative_set",
    "creative_set_id",
    "impressions",
    "clicks",
    "conversions",
    "cost",
)
TOKEN_ENV = "PULSE_APPLOVIN_REPORT_KEY"


def key_hint(src: dict[str, Any]) -> str:
    env = str(src.get("token_env", TOKEN_ENV))
    return f"check the Report Key in ${env} (AppLovin dashboard: Account > Keys)"


def to_row(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": dt.date.fromisoformat(str(d["day"])[:10]),
        "account_id": None,
        "campaign_id": d.get("campaign_id_external") or None,
        "campaign_name": d.get("campaign") or None,
        "adset_id": None,
        "adset_name": None,
        "ad_id": d.get("creative_set_id") or None,
        "ad_name": d.get("creative_set") or None,
        "country": d.get("country"),
        "platform": d.get("platform"),
        "spend": common.num(d.get("cost")),
        "currency": None,
        "impressions": common.as_int(d.get("impressions")),
        "clicks": common.as_int(d.get("clicks")),
        "network_installs": common.num(d.get("conversions")),
    }


def packages(src: dict[str, Any]) -> set[str]:
    raw = src.get("package_names", src.get("package_name"))
    if raw in (None, "", []):
        return set()
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return {str(x).strip().lower() for x in items if str(x).strip()}


def keep(d: dict[str, Any], wanted: set[str]) -> bool:
    if not wanted:
        return True
    have = {
        str(d.get(k) or "").strip().lower() for k in ("campaign_package_name", "campaign_store_id")
    }
    return bool(have & wanted)


def clamp(start: dt.date, end: dt.date, today: dt.date) -> tuple[dt.date, dt.date] | None:
    """The part of [start, end] inside the API's last 45 days, or None."""
    first = today - dt.timedelta(days=WINDOW_DAYS - 1)
    lo = max(start, first)
    return (lo, end) if lo <= end else None


def fetch_rows(
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    *,
    session: Any,
    key: str,
    redactor: common.Redactor,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    page = int(src.get("page_size", 10000))
    if page < 1:
        raise common.UAError("applovin_ads: page_size must be at least 1")
    wanted = packages(src)
    pacer = report_client.Pacer(float(src.get("min_interval_s", 1.0)), sleep=sleep)
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    chunk = max(1, min(WINDOW_DAYS, int(src.get("chunk_days", WINDOW_DAYS))))
    for since, until in common.chunks(start, end, chunk):
        offset = 0
        while True:
            params = {
                "api_key": key,
                "report_type": "advertiser",
                "start": since.isoformat(),
                "end": until.isoformat(),
                "columns": ",".join(COLUMNS),
                "format": "json",
                "not_zero": 1,
                "sort_day": "ASC",
                "limit": page,
                "offset": offset,
            }
            what = f"AppLovin report {since}..{until} (offset {offset})"
            r = report_client.get(
                session,
                URL,
                params=params,
                what=what,
                redactor=redactor,
                auth_hint=key_hint(src),
                pacer=pacer,
                sleep=sleep,
            )
            body = report_client.json_body(r, what, redactor)
            if not isinstance(body, dict):
                raise common.UAError(f"{what}: unexpected answer (not an object)")
            code = body.get("code", 200)
            if str(code) != "200":
                msg = report_client.api_message(r, redactor)
                raise common.UAError(f"{what}: API code {code}: {msg}")
            results = body.get("results") or []
            if not isinstance(results, list):
                raise common.UAError(f"{what}: `results` is not a list")
            for d in results:
                if not isinstance(d, dict) or not d.get("day") or not keep(d, wanted):
                    continue
                row = to_row(d)
                if not (since <= row["date"] <= until):
                    continue
                k = tuple(
                    row[c] for c in ("date", "campaign_id", "campaign_name", "ad_id", "ad_name")
                ) + (str(row["country"] or "").upper(), str(row["platform"] or "").upper())
                by_key[k] = row  # a row repeated across two pages is kept once
            if len(results) < page:
                break
            offset += page
    return list(by_key.values())


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
    today: dt.date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    common.reject_inline_secrets(src, "applovin_ads")
    start, end = common.ua_window(start, end, src)
    redactor = common.Redactor()
    replay = session is None and bool(src.get("replay"))
    if not replay:  # a replay answers from recorded days, so the live window does not apply
        today = today or dt.datetime.now(dt.UTC).date()
        window = clamp(start, end, today)
        if window is None:
            log(
                f"    {source:<13} -> raw_ua_spend           skipped ({start}..{end} is older "
                f"than AppLovin's {WINDOW_DAYS}-day request window; earlier rows kept)"
            )
            return 0
        if window[0] > start:
            log(
                f"    {source:<13}    AppLovin serves the last {WINDOW_DAYS} days only: reading "
                f"{window[0]}..{end}, earlier days keep their rows"
            )
        start, end = window
    if replay:
        from ..replay import ReplaySession

        session, key = ReplaySession(src["replay"]), "replay-key"
        sleep = lambda _s: None  # noqa: E731 - nothing is sent in a replay
    else:
        key = common.env_secret(str(src.get("token_env", TOKEN_ENV)), "applovin_ads")
        redactor.add(key)
        report_client.mask_urllib3_logs()
        if session is None:
            import requests

            session = requests.Session()
    rows = fetch_rows(src, start, end, session=session, key=key, redactor=redactor, sleep=sleep)
    rows = common.finish_rows(
        rows,
        network="applovin",
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
        detail="applovin advertiser report, creative set x country x day",
        log=log,
    )
