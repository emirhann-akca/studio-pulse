"""UA spend from CSV exports: the floor for any network without an API connector.

AppLovin, Unity Ads, Mintegral (or any other network) report exports are read
from a file or a folder of `*.csv` / `*.tsv` / `*.txt` files. Headers are
matched case-insensitively against the aliases below (the usual names in those
dashboards' exports); anything else is mapped with `columns:`. UTF-8, UTF-8
with BOM and UTF-16 files are read; the delimiter (`,` `;` tab) is detected.

    ua_csv:
      network: applovin            # see common.NETWORKS (free text is kept as is)
      path: reports/applovin       # file or folder
      currency: USD                # when the file has no currency column
      platform: ANDROID            # when the file has no platform column
      date_format: "%m/%d/%Y"      # when dates are not ISO (YYYY-MM-DD) or YYYYMMDD
      columns: {spend: "Cost (EUR)"}   # field -> header overrides

Each file is its own slice: re-importing a file replaces exactly its rows, and
a new export of the same days should replace the old file (the `ua_spend_unique`
check fails when two files overlap). Header aliases come from public help pages
and real export samples are not available here: verify one export of each
network with `pulse ingest --source ua_csv` and add `columns:` where needed.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Any

import duckdb

from ...warehouse import record_load, replace_slice
from ..stores import read_text
from . import common
from .creative import load_parser

ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "day", "stat_time_day", "date_start", "reporting date", "report date"),
    "campaign_id": ("campaign id", "campaign_id", "campaign_id_external", "campaign id external"),
    "campaign_name": ("campaign name", "campaign", "campaign_name", "offer name", "offer"),
    "adset_id": ("ad set id", "adset id", "adset_id", "ad group id", "adgroup_id"),
    "adset_name": ("ad set name", "adset name", "adset_name", "ad group name", "adgroup_name"),
    "ad_id": (
        "ad id",
        "ad_id",
        "creative set id",
        "creative_set_id",
        "creative pack id",
        "creative_pack_id",
        "creative id",
        "creative_id",
    ),
    "ad_name": (
        "ad name",
        "ad_name",
        "creative set",
        "creative_set",
        "creative set name",
        "creative pack",
        "creative pack name",
        "creative_pack_name",
        "creative name",
        "creative_name",
        "creative",
    ),
    "country": ("country", "country code", "country_code", "geo", "country_iso"),
    "platform": ("platform", "os", "device platform"),
    "spend": ("spend", "cost", "amount spent", "spend (usd)", "cost (usd)", "amount spent (usd)"),
    "currency": ("currency", "account currency", "account_currency"),
    "impressions": ("impressions", "impression", "imps", "starts"),
    "clicks": ("clicks", "click"),
    "network_installs": (
        "installs",
        "install",
        "app installs",
        "mobile app installs",
        "conversions",
    ),
}
REQUIRED = ("date", "spend")


def _dialect(text: str) -> str:
    head = text.split("\n", 1)[0]
    return max((",", ";", "\t"), key=head.count)


def resolve_columns(header: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    norm = {h.strip().lower(): h for h in header if h}
    out: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        if overrides and field in overrides:
            want = overrides[field].strip().lower()
            if want not in norm:
                raise common.UAError(
                    f"ua_csv: column {overrides[field]!r} (for {field}) not in file"
                )
            out[field] = norm[want]
            continue
        for a in aliases:
            if a in norm:
                out[field] = norm[a]
                break
    missing = [f for f in REQUIRED if f not in out]
    if missing:
        raise common.UAError(
            f"ua_csv: no column for {', '.join(missing)} (headers: {', '.join(header)}); "
            "map it with columns: {field: header}"
        )
    return out


def parse_date(value: str, fmt: str | None) -> dt.date:
    v = value.strip()
    try:
        if fmt:
            return dt.datetime.strptime(v, fmt).date()
        try:
            return dt.date.fromisoformat(v[:10])
        except ValueError:
            if len(v) >= 8 and v[:8].isdigit():
                return dt.datetime.strptime(v[:8], "%Y%m%d").date()
            raise
    except ValueError:
        raise common.UAError(
            f"ua_csv: cannot read date {value!r}"
            + (f" with date_format {fmt!r}" if fmt else "; set date_format (e.g. '%m/%d/%Y')")
        ) from None


def parse_csv(text: str, src: dict[str, Any]) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text), delimiter=_dialect(text))
    cols = resolve_columns(list(reader.fieldnames or []), src.get("columns"))
    fmt = src.get("date_format")
    rows = []
    for rec in reader:
        get = {f: (rec.get(h) or "").strip() for f, h in cols.items()}
        if not get.get("date"):
            continue
        if get["date"].lower().startswith(("total", "sum")):
            continue
        rows.append(
            {
                "date": parse_date(get["date"], fmt),
                "campaign_id": get.get("campaign_id") or None,
                "campaign_name": get.get("campaign_name") or None,
                "adset_id": get.get("adset_id") or None,
                "adset_name": get.get("adset_name") or None,
                "ad_id": get.get("ad_id") or None,
                "ad_name": get.get("ad_name") or None,
                "country": get.get("country"),
                "platform": get.get("platform") or src.get("platform"),
                "spend": common.num(get.get("spend")),
                "currency": get.get("currency") or src.get("currency"),
                "impressions": common.as_int(get.get("impressions")),
                "clicks": common.as_int(get.get("clicks")),
                "network_installs": common.num(get.get("network_installs")),
            }
        )
    return rows


def files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(f"ua_csv: {path} does not exist")
    return sorted(p for p in path.rglob("*") if p.suffix.lower() in (".csv", ".tsv", ".txt"))


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    src: dict[str, Any],
    path: Path,
    creative_parser: str | None = None,
    log: Any = print,
) -> int:
    common.reject_inline_secrets(src, "ua_csv")
    network = str(src.get("network") or "").strip().lower()
    if not network:
        raise common.UAError("ua_csv: set network (applovin, unity_ads, mintegral, ...)")
    parser = load_parser(src.get("creative_parser", creative_parser))
    total, spend = 0, 0.0
    found = files(path)
    for f in found:
        rows = parse_csv(read_text(f), src)
        rows = common.finish_rows(
            rows,
            network=network,
            start=dt.date.min,
            end=dt.date.max,
            parser=parser,
            default_platform=src.get("platform"),
            default_currency=src.get("currency"),
        )
        key = f.name if f == path else f.relative_to(path).as_posix()
        total += replace_slice(
            con,
            "raw_ua_spend",
            rows,
            app_id=app_id,
            source=source,
            where="_file = ?",
            params=[key],
            constants={"_file": key},
        )
        spend += sum(r.get("spend") or 0 for r in rows)
    record_load(
        con,
        app_id=app_id,
        source=source,
        table="raw_ua_spend",
        rows=total,
        detail=f"{network}: {len(found)} file(s)",
    )
    log(
        f"    {source:<13} -> raw_ua_spend           {total:>7} rows ({network}, {len(found)} file(s), spend {spend:,.2f})"
    )
    return total
