"""The raw UA spend contract shared by every ad-network connector.

Every network lands in one table, `raw_ua_spend`, one row per (date, network,
campaign, ad set, ad, country, platform):

  date, network, account_id, campaign_id, campaign_name, adset_id, adset_name,
  ad_id, ad_name, creative_id, creative_concept, country, platform,
  spend, currency, impressions, clicks, network_installs

Amounts stay in the account currency; `stg_ua_spend` converts them to USD with
the configured FX table. `network_installs` is what the network itself
reports (Meta app-install actions, Google Ads app-install conversions, TikTok
app installs, the "installs" column of a CSV export): an ad network's claim,
not an attributed install, and labelled as such everywhere.

Credentials come from environment variables (or Google's Application Default
Credentials) only. A config that holds a secret is refused, and every error
message passes through a redactor that removes the secret values the
connector knows about.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable, Iterator
from typing import Any

import duckdb

from ...warehouse import record_load, replace_slice

NETWORKS = (
    "meta",
    "google_ads",
    "tiktok",
    "applovin",
    "unity_ads",
    "mintegral",
    "ironsource",
    "apple_search_ads",
    "moloco",
    "other",
)
SECRET_KEYS = (
    "access_token",
    "token",
    "developer_token",
    "client_secret",
    "secret",
    "password",
    "api_key",
    "private_key",
    "refresh_token",
)


class UAError(RuntimeError):
    """An ad-network source failed; the message never contains a credential."""


class MissingCredential(UAError):
    pass


def reject_inline_secrets(src: dict[str, Any], kind: str) -> None:
    bad = sorted(k for k in src if k.lower() in SECRET_KEYS)
    if bad:
        raise UAError(
            f"{kind}: {', '.join(bad)} must not be in the config file; "
            "put the credential in an environment variable (see docs/OPERATIONS.md)"
        )


def env_secret(name: str, kind: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingCredential(f"{kind}: environment variable {name} is not set")
    return value


class Redactor:
    """Replaces known secret values in text (exception messages, logs)."""

    def __init__(self, *secrets: str | None) -> None:
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= 4 and secret not in self.secrets:
            self.secrets = sorted([*self.secrets, secret], key=len, reverse=True)

    def clean(self, text: Any) -> str:
        out = str(text)
        for s in self.secrets:
            out = out.replace(s, "***")
        return out


def num(v: Any) -> float | None:
    """Parse a number as networks send it: "12.30", 12, "1,234.5", "" or "-"."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if s in ("", "-", "--", "NA", "N/A", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def as_int(v: Any) -> int | None:
    f = num(v)
    return int(round(f)) if f is not None else None


def country(v: Any) -> str | None:
    s = str(v or "").strip().upper()
    if s in ("", "UNKNOWN", "NONE", "-", "--", "ZZ", "XX"):
        return None
    return s


def platform(v: Any) -> str | None:
    s = str(v or "").strip().upper()
    return {
        "ANDROID": "ANDROID",
        "GOOGLE_PLAY": "ANDROID",
        "IOS": "IOS",
        "IPHONE": "IOS",
        "IPAD": "IOS",
        "APPLE": "IOS",
    }.get(s, None if not s or s in ("ALL", "UNKNOWN") else s)


def chunks(start: dt.date, end: dt.date, days: int) -> Iterator[tuple[dt.date, dt.date]]:
    """Split [start, end] into consecutive windows of at most `days` days."""
    cur = start
    while cur <= end:
        stop = min(end, cur + dt.timedelta(days=days - 1))
        yield cur, stop
        cur = stop + dt.timedelta(days=1)


def ua_window(start: dt.date, end: dt.date, src: dict[str, Any]) -> tuple[dt.date, dt.date]:
    """The days a UA source re-reads: the last `lookback_days` (default 30) of the window.

    Networks revise recent days (attribution windows, invalid-traffic
    credits); 30 days covers Meta's 28-day click window. Older days keep the
    rows of earlier runs.
    """
    days = int(src.get("lookback_days", 30))
    return max(start, end - dt.timedelta(days=days - 1)), end


def finish_rows(
    rows: Iterable[dict[str, Any]],
    *,
    network: str,
    start: dt.date,
    end: dt.date,
    parser: Any,
    default_platform: str | None = None,
    default_currency: str | None = None,
) -> list[dict[str, Any]]:
    from .creative import annotate

    out = []
    for r in rows:
        if r.get("date") is None or not (start <= r["date"] <= end):
            continue
        r["network"] = network
        r["platform"] = platform(r.get("platform")) or platform(default_platform)
        r["country"] = country(r.get("country"))
        r["currency"] = (r.get("currency") or default_currency or "USD").upper()
        for k in ("campaign_id", "adset_id", "ad_id", "account_id"):
            if r.get(k) is not None:
                r[k] = str(r[k])
        out.append(r)
    annotate(out, parser)
    return out


def load(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    rows: list[dict[str, Any]],
    start: dt.date,
    end: dt.date,
    detail: str,
    log: Any = print,
) -> int:
    """Replace this source's [start, end] slice of raw_ua_spend (idempotent)."""
    n = replace_slice(
        con,
        "raw_ua_spend",
        rows,
        app_id=app_id,
        source=source,
        where="date BETWEEN ? AND ?",
        params=[start, end],
    )
    record_load(con, app_id=app_id, source=source, table="raw_ua_spend", rows=n, detail=detail)
    spend = sum(r.get("spend") or 0 for r in rows)
    log(f"    {source:<13} -> raw_ua_spend           {n:>7} rows ({detail}, spend {spend:,.2f})")
    return n
