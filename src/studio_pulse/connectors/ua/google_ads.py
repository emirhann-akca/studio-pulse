"""Google Ads spend: one GAQL report through the REST interface.

POST https://googleads.googleapis.com/{api_version}/customers/{customer_id}/googleAds:search
with the query below; pages follow `nextPageToken` (sent back as `pageToken`
in the body), described as a dlt `rest_api` resource.

Access model. Google retired developer tokens on 9 September 2026; API access
is now granted per Google Cloud project (access levels: Google Ads API
"access levels" page, read 11 Nov 2026), and google-ads-python 32.0.0 dropped
the client-side check that a developer token is configured. So this connector
needs no developer token and dlt's verified `google_ads` source, which still
requires `dev_token`, does not fit. A request carries:

  Authorization: Bearer <OAuth token with the adwords scope>
      from $PULSE_GOOGLE_ADS_ACCESS_TOKEN, otherwise Application Default
      Credentials (`gcloud auth application-default login --scopes=
      https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform`)
  x-goog-user-project: <quota_project>   the Cloud project that holds the API
      access (optional; ADC's quota project is used when unset)
  login-customer-id: <manager account>   when the account is reached through an MCC
  developer-token: $PULSE_GOOGLE_ADS_DEVELOPER_TOKEN, only if that variable
      is set (accounts still on the old model)

Not verified against a live account: tested with recorded responses in the
documented REST shape only.

Config:

    google_ads:
      customer_id: "123-456-7890"
      login_customer_id: "111-222-3333"   # optional (manager account)
      quota_project: my-cloud-project     # optional
      platform: ANDROID                   # optional
      api_version: v26                    # default
      lookback_days: 30

Granularity: ad level per day (`ad_group_ad`). Google Ads cannot segment
`ad_group_ad` by country, so `country` is empty for this network; installs are
`metrics.biddable_app_install_conversions` (App campaigns).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any

import duckdb

from . import common, rest
from .creative import load_parser

HOST = "https://googleads.googleapis.com"
DEFAULT_VERSION = "v26"
SCOPE = "https://www.googleapis.com/auth/adwords"


def gaql(start: dt.date, end: dt.date) -> str:
    return (
        "SELECT segments.date, customer.id, customer.currency_code, campaign.id, campaign.name, "
        "ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.name, "
        "metrics.cost_micros, metrics.impressions, metrics.clicks, "
        "metrics.biddable_app_install_conversions "
        "FROM ad_group_ad "
        f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )


def customer(cid: Any) -> str:
    digits = re.sub(r"[\s-]", "", str(cid or ""))
    if not digits.isdigit():
        raise common.UAError(f"google_ads: customer id must be 10 digits, got {cid!r}")
    return digits


def to_row(r: dict[str, Any]) -> dict[str, Any]:
    seg, cust = r.get("segments") or {}, r.get("customer") or {}
    camp, grp = r.get("campaign") or {}, r.get("adGroup") or {}
    ad = (r.get("adGroupAd") or {}).get("ad") or {}
    m = r.get("metrics") or {}
    micros = common.num(m.get("costMicros"))
    return {
        "date": dt.date.fromisoformat(seg["date"]),
        "account_id": cust.get("id"),
        "currency": cust.get("currencyCode"),
        "campaign_id": camp.get("id"),
        "campaign_name": camp.get("name"),
        "adset_id": grp.get("id"),
        "adset_name": grp.get("name"),
        "ad_id": ad.get("id"),
        "ad_name": ad.get("name"),
        "country": None,
        "spend": micros / 1e6 if micros is not None else None,
        "impressions": common.as_int(m.get("impressions")),
        "clicks": common.as_int(m.get("clicks")),
        "network_installs": common.num(m.get("biddableAppInstallConversions")),
    }


def access_token() -> str:
    tok = os.environ.get("PULSE_GOOGLE_ADS_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError as exc:
        raise common.MissingCredential(
            "google_ads: set PULSE_GOOGLE_ADS_ACCESS_TOKEN or install the gcp extra for "
            "Application Default Credentials"
        ) from exc
    try:
        creds, _ = google.auth.default(scopes=[SCOPE])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as exc:
        raise common.MissingCredential(
            f"google_ads: no usable Application Default Credentials ({type(exc).__name__})"
        ) from None
    return creds.token


def headers(src: dict[str, Any], token: str) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if src.get("login_customer_id"):
        h["login-customer-id"] = customer(src["login_customer_id"])
    if src.get("quota_project"):
        h["x-goog-user-project"] = str(src["quota_project"])
    dev = os.environ.get("PULSE_GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    if dev:
        h["developer-token"] = dev
    return h


def fetch_rows(
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    *,
    session: Any,
    token: str,
    redactor: common.Redactor,
) -> list[dict[str, Any]]:
    from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator

    version = str(src.get("api_version", DEFAULT_VERSION))
    recs = rest.fetch(
        base_url=f"{HOST}/{version}/",
        endpoint={
            "path": f"customers/{customer(src['customer_id'])}/googleAds:search",
            "method": "POST",
            "json": {"query": gaql(start, end)},
            "data_selector": "results",
        },
        paginator=JSONResponseCursorPaginator(
            cursor_path="nextPageToken", cursor_body_path="pageToken"
        ),
        headers=headers(src, token),
        session=session,
        redactor=redactor,
        what=f"Google Ads search {start}..{end}",
    )
    return [to_row(r) for r in recs]


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
    common.reject_inline_secrets(src, "google_ads")
    start, end = common.ua_window(start, end, src)
    redactor = common.Redactor(os.environ.get("PULSE_GOOGLE_ADS_DEVELOPER_TOKEN"))
    if session is None and src.get("replay"):
        from ..replay import ReplaySession

        session, token = ReplaySession(src["replay"]), "replay"
    else:
        token = access_token()
        redactor.add(token)
        session = session or rest.default_session()
    rows = fetch_rows(src, start, end, session=session, token=token, redactor=redactor)
    rows = common.finish_rows(
        rows,
        network="google_ads",
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
        detail="google ads, ad x day",
        log=log,
    )
