"""Meta (Facebook / Instagram) ad spend: Marketing API Insights at ad level.

GET https://graph.facebook.com/{api_version}/act_{account_id}/insights with
level=ad, time_increment=1, breakdowns=country and a time_range, the
synchronous form of the request dlt's verified `facebook_ads` source sends
through the facebook-business SDK (same endpoint, level, daily increment and
fields). It is described here as a dlt `rest_api` resource instead, so the
connector needs no SDK, sends the token only as an `Authorization: Bearer`
header, and runs against recorded responses in the tests and the demo.

Installs are the first of `mobile_app_install`, `app_install`,
`omni_app_install` found in `actions` (the three overlap and are never
summed). Pages follow `paging.next`, with the `access_token` Meta writes into
that link removed. The window is requested in chunks of `chunk_days` (default
7) so a large account does not hit the synchronous request time limit; each
chunk keeps only the rows of its own days.

Config (an app's `sources:`):

    meta_ads:
      account_id: "1234567890"        # the ad account (act_ prefix optional)
      platform: ANDROID               # optional: the platform this account buys
      api_version: v27.0              # default
      token_env: PULSE_META_ACCESS_TOKEN
      lookback_days: 30               # re-read window (attribution revisions)

Credential: a system-user access token with ads_read, in the environment
variable named by token_env. Not verified against a live account: tested with
recorded responses in the documented shape only.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import duckdb

from . import common, rest
from .creative import load_parser

HOST = "https://graph.facebook.com"
DEFAULT_VERSION = "v27.0"
FIELDS = (
    "account_id,account_currency,campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,"
    "impressions,clicks,spend,actions,date_start,date_stop"
)
INSTALL_ACTIONS = ("mobile_app_install", "app_install", "omni_app_install")


def installs(actions: list[dict[str, Any]] | None) -> float | None:
    for kind in INSTALL_ACTIONS:
        for a in actions or []:
            if a.get("action_type") == kind:
                return common.num(a.get("value"))
    return None


def to_row(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": dt.date.fromisoformat(str(d["date_start"])[:10]),
        "account_id": d.get("account_id"),
        "campaign_id": d.get("campaign_id"),
        "campaign_name": d.get("campaign_name"),
        "adset_id": d.get("adset_id"),
        "adset_name": d.get("adset_name"),
        "ad_id": d.get("ad_id"),
        "ad_name": d.get("ad_name"),
        "country": d.get("country"),
        "spend": common.num(d.get("spend")),
        "currency": d.get("account_currency"),
        "impressions": common.as_int(d.get("impressions")),
        "clicks": common.as_int(d.get("clicks")),
        "network_installs": installs(d.get("actions")),
    }


def account_path(account_id: str) -> str:
    acc = str(account_id).strip()
    acc = acc[4:] if acc.startswith("act_") else acc
    if not acc.isdigit():
        raise common.UAError(
            f"meta_ads: account_id must be digits (act_123...), got {account_id!r}"
        )
    return f"act_{acc}"


def fetch_rows(
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    *,
    session: Any,
    token: str,
    redactor: common.Redactor,
) -> list[dict[str, Any]]:
    version = str(src.get("api_version", DEFAULT_VERSION))
    path = f"{account_path(src['account_id'])}/insights"
    rows: list[dict[str, Any]] = []
    for since, until in common.chunks(start, end, int(src.get("chunk_days", 7))):
        params = {
            "level": "ad",
            "time_increment": 1,
            "breakdowns": "country",
            "fields": FIELDS,
            "time_range": rest.literal(
                json.dumps({"since": since.isoformat(), "until": until.isoformat()})
            ),
            "limit": int(src.get("page_size", 500)),
        }
        recs = rest.fetch(
            base_url=f"{HOST}/{version}/",
            endpoint={"path": path, "params": params, "data_selector": "data"},
            paginator=rest.link_paginator("paging.next", strip="access_token"),
            headers={"Authorization": f"Bearer {token}"},
            session=session,
            redactor=redactor,
            what=f"Meta Insights {since}..{until}",
        )
        rows += [r for r in map(to_row, recs) if since <= r["date"] <= until]
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
    common.reject_inline_secrets(src, "meta_ads")
    start, end = common.ua_window(start, end, src)
    redactor = common.Redactor()
    if session is None and src.get("replay"):
        from ..replay import ReplaySession

        session, token = ReplaySession(src["replay"]), "replay"
    else:
        token = common.env_secret(str(src.get("token_env", "PULSE_META_ACCESS_TOKEN")), "meta_ads")
        redactor.add(token)
        session = session or rest.default_session()
    rows = fetch_rows(src, start, end, session=session, token=token, redactor=redactor)
    rows = common.finish_rows(
        rows,
        network="meta",
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
        detail="meta insights, ad x country x day",
        log=log,
    )
