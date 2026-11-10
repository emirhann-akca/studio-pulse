"""Store report importers.

Google Play Console statistics reports (monthly CSVs, UTF-16 encoded, found
in the Play Console's Cloud Storage bucket under stats/):
  installs_<package>_<YYYYMM>_overview.csv
      Date, Package Name, Daily Device Installs, Daily Device Uninstalls,
      Daily User Installs, Daily User Uninstalls, Install events, ...
  ratings_<package>_<YYYYMM>_overview.csv
      Date, Package Name, Daily Average Rating, Total Average Rating

App Store Connect Sales and Trends, Summary Sales Report (daily, tab-separated,
optionally gzipped): Provider, ..., SKU, ..., Product Type Identifier, Units,
Developer Proceeds (per unit), Begin Date (MM/DD/YYYY), ..., Currency of
Proceeds, Apple Identifier, ..., Parent Identifier, ...

Each file is loaded as its own slice (re-importing a file replaces its rows).
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb

from ..warehouse import record_load, replace_slice

# Apple product type identifiers (Sales and Trends documentation).
APPLE_FIRST_DOWNLOAD = {"1", "1F", "1T", "1E", "1EP", "1EU", "F1"}
APPLE_REDOWNLOAD = {"3", "3F", "3T", "F3"}
APPLE_UPDATE = {"7", "7F", "7T", "F7"}
APPLE_IAP = {"IA1", "IA9", "IAY", "IAC", "FI1"}


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if path.name.endswith(".gz"):
        raw = gzip.decompress(raw)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().replace(",", "")
    if v in ("", "NA", "N/A", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(v: str | None) -> int | None:
    f = _num(v)
    return int(round(f)) if f is not None else None


def _first(row: dict[str, str], *names: str) -> str | None:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def parse_play_installs(text: str, package: str | None = None) -> list[dict[str, Any]]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        if package and row.get("Package Name") and row["Package Name"] != package:
            continue
        out.append(
            {
                "date": dt.date.fromisoformat(row["Date"]),
                "installs": _int(
                    _first(row, "Daily User Installs", "Install events", "Daily Device Installs")
                ),
                "uninstalls": _int(
                    _first(
                        row, "Daily User Uninstalls", "Uninstall events", "Daily Device Uninstalls"
                    )
                ),
                "updates": _int(_first(row, "Update events", "Daily Device Upgrades")),
            }
        )
    return out


def parse_play_ratings(text: str, package: str | None = None) -> list[dict[str, Any]]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        if package and row.get("Package Name") and row["Package Name"] != package:
            continue
        out.append(
            {
                "date": dt.date.fromisoformat(row["Date"]),
                "daily_avg_rating": _num(row.get("Daily Average Rating")),
                "total_avg_rating": _num(row.get("Total Average Rating")),
            }
        )
    return out


def parse_appstore_sales(
    text: str, apple_id: str | None = None, sku: str | None = None
) -> list[dict[str, Any]]:
    """Aggregate a Summary Sales Report to (date, proceeds currency) rows."""
    acc: dict[tuple[dt.date, str], dict[str, Any]] = defaultdict(
        lambda: {"installs": 0, "redownloads": 0, "updates": 0, "iap_units": 0, "proceeds": 0.0}
    )
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("Product Type Identifier"):
            continue
        is_app_row = apple_id is None or row.get("Apple Identifier") == apple_id
        is_child = sku is not None and row.get("Parent Identifier") == sku
        if not (is_app_row or is_child):
            continue
        ptype = row["Product Type Identifier"]
        units = _int(row.get("Units")) or 0
        day = dt.datetime.strptime(row["Begin Date"], "%m/%d/%Y").date()
        cur = row.get("Currency of Proceeds") or "USD"
        bucket = acc[(day, cur)]
        if ptype in APPLE_FIRST_DOWNLOAD:
            bucket["installs"] += units
        elif ptype in APPLE_REDOWNLOAD:
            bucket["redownloads"] += units
        elif ptype in APPLE_UPDATE:
            bucket["updates"] += units
        elif ptype in APPLE_IAP:
            bucket["iap_units"] += units
        bucket["proceeds"] += units * (_num(row.get("Developer Proceeds")) or 0.0)
    return [{"date": d, "proceeds_currency": c, **v} for (d, c), v in sorted(acc.items())]


def _files(path: Path, patterns: Iterable[str]) -> list[Path]:
    if path.is_file():
        return [path]
    found: set[Path] = set()
    for pat in patterns:
        found.update(path.rglob(pat))
    return sorted(found)


def ingest_play(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    path: str | Path,
    package: str | None = None,
    log: Any = print,
) -> int:
    root, total = Path(path), 0
    for f in _files(root, ["installs_*overview.csv"]):
        rows = parse_play_installs(read_text(f), package)
        total += replace_slice(
            con,
            "raw_store_installs",
            rows,
            app_id=app_id,
            source=source,
            where="_file = ?",
            params=[f.name],
            constants={"store": "google_play", "_file": f.name},
        )
    n_r = 0
    for f in _files(root, ["ratings_*overview.csv"]):
        rows = parse_play_ratings(read_text(f), package)
        n_r += replace_slice(
            con,
            "raw_store_ratings",
            rows,
            app_id=app_id,
            source=source,
            where="_file = ?",
            params=[f.name],
            constants={"store": "google_play", "_file": f.name},
        )
    record_load(con, app_id=app_id, source=source, table="raw_store_installs", rows=total)
    log(f"    play_console  -> raw_store_installs     {total:>7} rows, ratings {n_r}")
    return total + n_r


def ingest_appstore(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    path: str | Path,
    apple_id: str | None = None,
    sku: str | None = None,
    log: Any = print,
) -> int:
    root, total = Path(path), 0
    for f in _files(root, ["*.txt", "*.txt.gz", "*.tsv"]):
        rows = parse_appstore_sales(read_text(f), apple_id=apple_id, sku=sku)
        total += replace_slice(
            con,
            "raw_store_installs",
            rows,
            app_id=app_id,
            source=source,
            where="_file = ?",
            params=[f.name],
            constants={"store": "app_store", "_file": f.name},
        )
    record_load(con, app_id=app_id, source=source, table="raw_store_installs", rows=total)
    log(f"    app_store     -> raw_store_installs     {total:>7} rows")
    return total
