"""TikTok ad spend: TikTok API for Business synchronous report.

GET https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/ with
the token in the `Access-Token` header. Path, host, header and parameter names
(report_type, data_level, dimensions, metrics, start_date, end_date, page,
page_size) were checked against TikTok's official Python SDK
(github.com/tiktok/tiktok-business-api-sdk, `reporting_api.py`,
`report_integrated_get`; PyPI `tiktok-business-api-sdk-official`, MIT). The SDK
is not used: the endpoint is a dlt `rest_api` resource like the other networks,
which keeps one HTTP stack and lets the tests replay recorded responses. (Do not
install the unrelated PyPI name `tiktok-business-api-sdk`.)

Request: report_type AUDIENCE (country is an audience dimension), data_level
AUCTION_AD, dimensions ["ad_id", "stat_time_day", "country_code"], attribute
and basic metrics below, JSON-encoded lists. Day-level reports are requested
in chunks of at most 30 days; pages follow data.page_info.total_page. TikTok
answers errors with HTTP 200 and a non-zero `code`, which is raised.

Config:

    tiktok_ads:
      advertiser_id: "7000000000000000000"
      platform: ANDROID            # optional
      token_env: PULSE_TIKTOK_ACCESS_TOKEN
      lookback_days: 30

Not verified against a live account (no token was available): the metric
names follow TikTok's reporting documentation as quoted by the SDK's docs
links, and the tests use recorded responses in that shape.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import duckdb

from . import common, rest
from .creative import load_parser

HOST = "https://business-api.tiktok.com"
API = "open_api/v1.3"
DIMENSIONS = ["ad_id", "stat_time_day", "country_code"]
METRICS = [
    "campaign_id",
    "campaign_name",
    "adgroup_id",
    "adgroup_name",
    "ad_name",
    "currency",
    "spend",
    "impressions",
    "clicks",
    "app_install",
]


def to_row(item: dict[str, Any], advertiser_id: str) -> dict[str, Any]:
    d, m = item.get("dimensions") or {}, item.get("metrics") or {}
    return {
        "date": dt.date.fromisoformat(str(d["stat_time_day"])[:10]),
        "account_id": advertiser_id,
        "campaign_id": m.get("campaign_id"),
        "campaign_name": m.get("campaign_name"),
        "adset_id": m.get("adgroup_id"),
        "adset_name": m.get("adgroup_name"),
        "ad_id": d.get("ad_id"),
        "ad_name": m.get("ad_name"),
        "country": d.get("country_code"),
        "spend": common.num(m.get("spend")),
        "currency": m.get("currency"),
        "impressions": common.as_int(m.get("impressions")),
        "clicks": common.as_int(m.get("clicks")),
        "network_installs": common.num(m.get("app_install")),
    }


def raise_on_error_code(response: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        body = response.json()
    except ValueError:
        return response
    code = body.get("code") if isinstance(body, dict) else None
    if code not in (0, None):
        raise common.UAError(f"TikTok API code {code}: {str(body.get('message', ''))[:200]}")
    return response


def fetch_rows(
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    *,
    session: Any,
    token: str,
    redactor: common.Redactor,
) -> list[dict[str, Any]]:
    from dlt.sources.helpers.rest_client.paginators import PageNumberPaginator

    adv = str(src["advertiser_id"]).strip()
    if not adv.isdigit():
        raise common.UAError(f"tiktok_ads: advertiser_id must be digits, got {adv!r}")
    rows: list[dict[str, Any]] = []
    for since, until in common.chunks(start, end, min(30, int(src.get("chunk_days", 30)))):
        params = {
            "advertiser_id": adv,
            "report_type": "AUDIENCE",
            "data_level": "AUCTION_AD",
            "dimensions": json.dumps(DIMENSIONS),
            "metrics": json.dumps(METRICS),
            "start_date": since.isoformat(),
            "end_date": until.isoformat(),
            "page_size": int(src.get("page_size", 1000)),
        }
        recs = rest.fetch(
            base_url=f"{HOST}/{API}/",
            endpoint={
                "path": "report/integrated/get/",
                "params": params,
                "data_selector": "data.list",
            },
            paginator=PageNumberPaginator(
                base_page=1, page_param="page", total_path="data.page_info.total_page"
            ),
            headers={"Access-Token": token},
            session=session,
            redactor=redactor,
            what=f"TikTok report {since}..{until}",
            response_actions=[raise_on_error_code],
        )
        rows += [r for r in (to_row(x, adv) for x in recs) if since <= r["date"] <= until]
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
) -> int:
    common.reject_inline_secrets(src, "tiktok_ads")
    start, end = common.ua_window(start, end, src)
    redactor = common.Redactor()
    if session is None and src.get("replay"):
        from ..replay import ReplaySession

        session, token = ReplaySession(src["replay"]), "replay"
    else:
        token = common.env_secret(
            str(src.get("token_env", "PULSE_TIKTOK_ACCESS_TOKEN")), "tiktok_ads"
        )
        redactor.add(token)
        session = session or rest.default_session()
    rows = fetch_rows(src, start, end, session=session, token=token, redactor=redactor)
    rows = common.finish_rows(
        rows,
        network="tiktok",
        start=start,
        end=end,
        parser=load_parser(src.get("creative_parser", creative_parser)),
        default_platform=src.get("platform"),
    )
    return common.load(
        con,
        app_id=app_id,
        source=source,
        rows=rows,
        start=start,
        end=end,
        detail="tiktok audience report, ad x country x day",
        log=log,
    )
