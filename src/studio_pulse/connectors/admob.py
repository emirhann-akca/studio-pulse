"""AdMob Reporting API connector (network report).

POST https://admob.googleapis.com/v1/accounts/{publisher_id}/networkReport:generate
with dimensions DATE x APP and metrics ESTIMATED_EARNINGS, IMPRESSIONS,
CLICKS, AD_REQUESTS, MATCHED_REQUESTS. The response is a JSON array: a header
element, one element per row, and a footer. Earnings come back in micros of
the report currency (we request USD via localizationSettings).

Auth: an OAuth access token with the admob.readonly scope, taken from
$PULSE_ADMOB_ACCESS_TOKEN or from Application Default Credentials. A recorded
response file can be replayed instead (`response_file`), which is how the demo
and the tests run without network access.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import duckdb

from ..warehouse import record_load, replace_slice

API_ROOT = "https://admob.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/admob.readonly"
METRICS = ["ESTIMATED_EARNINGS", "IMPRESSIONS", "CLICKS", "AD_REQUESTS", "MATCHED_REQUESTS"]


def _date_obj(d: dt.date) -> dict[str, int]:
    return {"year": d.year, "month": d.month, "day": d.day}


def report_request(
    start: dt.date, end: dt.date, app_ids: list[str] | None = None
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "dateRange": {"startDate": _date_obj(start), "endDate": _date_obj(end)},
        "dimensions": ["DATE", "APP"],
        "metrics": METRICS,
        "localizationSettings": {"currencyCode": "USD"},
    }
    if app_ids:
        spec["dimensionFilters"] = [{"dimension": "APP", "matchesAny": {"values": app_ids}}]
    return {"reportSpec": spec}


def _access_token() -> str:
    tok = os.environ.get("PULSE_ADMOB_ACCESS_TOKEN")
    if tok:
        return tok
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=[SCOPE])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def fetch_report(
    publisher_id: str,
    start: dt.date,
    end: dt.date,
    app_ids: list[str] | None = None,
    session: Any = None,
    token: str | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    if session is None:
        import requests

        session = requests.Session()
    if not publisher_id.startswith("pub-"):
        raise ValueError("AdMob publisher id must look like pub-XXXXXXXXXXXXXXXX")
    url = f"{API_ROOT}/accounts/{publisher_id}/networkReport:generate"
    resp = session.post(
        url,
        headers={
            "Authorization": f"Bearer {token or _access_token()}",
            "Content-Type": "application/json",
        },
        data=json.dumps(report_request(start, end, app_ids)),
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"AdMob API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _integer(metrics: dict[str, Any], name: str) -> int | None:
    v = metrics.get(name, {}).get("integerValue")
    return int(v) if v is not None else None


def parse_report(elements: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Turn the streamed report into flat daily rows. Returns (rows, currency)."""
    currency = "USD"
    rows: list[dict[str, Any]] = []
    for el in elements:
        if "header" in el:
            currency = el["header"].get("localizationSettings", {}).get("currencyCode", currency)
        row = el.get("row")
        if not row:
            continue
        dims, mets = row.get("dimensionValues", {}), row.get("metricValues", {})
        day = dims["DATE"]["value"]

        micros = mets.get("ESTIMATED_EARNINGS", {}).get("microsValue")
        rows.append(
            {
                "date": dt.date(int(day[:4]), int(day[4:6]), int(day[6:8])),
                "admob_app_id": dims.get("APP", {}).get("value"),
                "earnings": int(micros) / 1e6 if micros is not None else None,
                "currency": currency,
                "impressions": _integer(mets, "IMPRESSIONS"),
                "clicks": _integer(mets, "CLICKS"),
                "ad_requests": _integer(mets, "AD_REQUESTS"),
                "matched_requests": _integer(mets, "MATCHED_REQUESTS"),
            }
        )
    return rows, currency


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    start: dt.date,
    end: dt.date,
    publisher_id: str | None = None,
    app_ids: list[str] | None = None,
    response_file: str | Path | None = None,
    session: Any = None,
    token: str | None = None,
    log: Any = print,
) -> int:
    if response_file:
        elements = json.loads(Path(response_file).read_text(encoding="utf-8"))
    else:
        if not publisher_id:
            raise ValueError("admob source needs publisher_id or response_file")
        elements = fetch_report(publisher_id, start, end, app_ids, session=session, token=token)
    rows, _ = parse_report(elements)
    if app_ids:
        rows = [r for r in rows if r["admob_app_id"] in app_ids]
    rows = [r for r in rows if start <= r["date"] <= end]
    n = replace_slice(
        con,
        "raw_admob_daily",
        rows,
        app_id=app_id,
        source=source,
        where="date BETWEEN ? AND ?",
        params=[start, end],
    )
    record_load(con, app_id=app_id, source=source, table="raw_admob_daily", rows=n)
    log(f"    admob         -> raw_admob_daily        {n:>7} rows")
    return n
