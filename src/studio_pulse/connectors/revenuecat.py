"""RevenueCat scheduled data export connector (Transactions feed).

RevenueCat delivers files to a bucket as
    <prefix>/<YYYY-MM-DD>/transactions_<timestamp>.<csv.gz|parquet>
(Parquet deliveries are split into several files per run). With "receive new
and updated transactions only" enabled, every delivery contains the rows that
changed since the previous one, so one transaction shows up in several files:
after a refund, an unsubscribe, a billing issue, a grace period, ... Summing
the raw rows double counts revenue.

This connector stores every received version (one row per file row, tagged
with the file name, the delivery timestamp and the row number). The model
models/stg_rc_transactions.sql then keeps exactly one version per
(store_transaction_id, renewal_number) - the one with the newest `updated_at`,
falling back to the newest delivery - which is RevenueCat's documented key.

Files are read from a local directory or from gs://bucket/prefix (downloaded
once into a local cache). Files already loaded are skipped.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import duckdb

from ..warehouse import utcnow

# Export column -> warehouse type. Columns missing from a file become NULL,
# so older export versions (e.g. without tax/commission) still load.
RC_COLUMNS: dict[str, str] = {
    "store_transaction_id": "VARCHAR",
    "original_store_transaction_id": "VARCHAR",
    "renewal_number": "INTEGER",
    "rc_original_app_user_id": "VARCHAR",
    "product_identifier": "VARCHAR",
    "store": "VARCHAR",
    "platform": "VARCHAR",
    "country": "VARCHAR",
    "start_time": "TIMESTAMP",
    "end_time": "TIMESTAMP",
    "is_auto_renewable": "BOOLEAN",
    "is_trial_period": "BOOLEAN",
    "is_trial_conversion": "BOOLEAN",
    "is_sandbox": "BOOLEAN",
    "ownership_type": "VARCHAR",
    "refunded_at": "TIMESTAMP",
    "price_in_usd": "DOUBLE",
    "purchase_price_in_usd": "DOUBLE",
    "purchased_currency": "VARCHAR",
    "price_in_purchased_currency": "DOUBLE",
    "purchase_price_in_purchased_currency": "DOUBLE",
    "tax_percentage": "DOUBLE",
    "commission_percentage": "DOUBLE",
    "takehome_percentage": "DOUBLE",
    "first_seen_time": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
}

_SUFFIXES = (".parquet", ".csv", ".csv.gz")
_TS_PATTERNS = [
    (re.compile(r"transactions_(\d{10,13})"), "epoch"),
    (re.compile(r"(\d{8}T\d{6})"), "%Y%m%dT%H%M%S"),
    (re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}[:-]\d{2}[:-]\d{2})"), "iso"),
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), "%Y-%m-%d"),
]


def file_timestamp(rel_path: str) -> dt.datetime | None:
    """Best-effort delivery time from the file name or its dated folder."""
    name = rel_path.replace("\\", "/")
    for rx, fmt in _TS_PATTERNS:
        m = rx.search(name.rsplit("/", 1)[-1]) or rx.search(name)
        if not m:
            continue
        raw = m.group(1)
        try:
            if fmt == "epoch":
                v = int(raw)
                return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.UTC).replace(
                    tzinfo=None
                )
            if fmt == "iso":
                return dt.datetime.fromisoformat(raw[:10] + "T" + raw[11:].replace("-", ":"))
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def discover_files(root: Path) -> list[Path]:
    files = [p for p in root.rglob("*") if p.is_file() and p.name.endswith(_SUFFIXES)]
    return sorted(
        files, key=lambda p: (file_timestamp(str(p.relative_to(root))) or dt.datetime.min, str(p))
    )


def sync_gcs(
    uri: str,
    cache_dir: Path,
    client: Any = None,
    log: Any = print,
    suffixes: tuple[str, ...] | None = None,
    label: str = "revenuecat",
    contains: str | None = None,
) -> Path:
    """Download new export files from gs://bucket/prefix into cache_dir (read-only on GCS)."""
    suffixes = suffixes or _SUFFIXES
    m = re.match(r"^gs://([^/]+)/?(.*)$", uri)
    if not m:
        raise ValueError(f"not a gs:// uri: {uri}")
    bucket, prefix = m.group(1), m.group(2)
    if client is None:
        from google.cloud import storage

        client = storage.Client()
    cache_dir.mkdir(parents=True, exist_ok=True)
    root = cache_dir.resolve()
    fetched = 0
    for blob in client.list_blobs(bucket, prefix=prefix or None):
        if not blob.name.endswith(suffixes) or (contains and contains not in blob.name):
            continue
        rel = blob.name[len(prefix) :].lstrip("/") if prefix else blob.name
        target = (root / rel).resolve()
        # Object names are arbitrary strings: "../" must not write outside the cache.
        if not target.is_relative_to(root):
            log(f"    {label}: skipped object outside the export prefix: {blob.name!r}")
            continue
        if target.exists() and blob.size is not None and target.stat().st_size == blob.size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(target))
        fetched += 1
    log(f"    {label}: {fetched} new file(s) downloaded")
    return cache_dir


def _reader(path: Path) -> str:
    p = str(path).replace("'", "''")
    if path.name.endswith(".parquet"):
        return f"read_parquet('{p}')"
    return f"read_csv('{p}', header = true, auto_detect = true, all_varchar = true)"


def load_file(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    path: Path,
    rel: str,
    product_prefixes: list[str] | None = None,
) -> int:
    reader = _reader(path)
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()}
    select = []
    for col, typ in RC_COLUMNS.items():
        if col in cols:
            expr = f'TRY_CAST("{col}" AS {typ})'
            if typ == "BOOLEAN":
                expr = f'TRY_CAST(lower(CAST("{col}" AS VARCHAR)) AS BOOLEAN)'
            select.append(f"{expr} AS {col}")
        else:
            select.append(f"CAST(NULL AS {typ}) AS {col}")
    where, prefix_args = "", list(product_prefixes or [])
    if prefix_args:
        ors = " OR ".join(
            "starts_with(CAST(product_identifier AS VARCHAR), ?)" for _ in prefix_args
        )
        where = f"WHERE {ors}"
    con.execute(
        "DELETE FROM raw_revenuecat_transactions WHERE app_id = ? AND _source = ? AND _file = ?",
        [app_id, source, rel],
    )
    con.execute(
        f"""
        INSERT INTO raw_revenuecat_transactions BY NAME
        SELECT {", ".join(select)},
               ? AS app_id, ? AS _file, ? AS _file_ts,
               row_number() OVER () AS _row, ? AS _source, ? AS _loaded_at
        FROM {reader} {where}
        """,
        [app_id, rel, file_timestamp(rel), source, utcnow(), *prefix_args],
    )
    return con.execute(
        "SELECT COUNT(*) FROM raw_revenuecat_transactions WHERE app_id = ? AND _source = ? AND _file = ?",
        [app_id, source, rel],
    ).fetchone()[0]


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    path: str,
    cache_dir: Path | None = None,
    product_prefixes: list[str] | None = None,
    full_refresh: bool = False,
    gcs_client: Any = None,
    log: Any = print,
) -> int:
    if path.startswith("gs://"):
        root = sync_gcs(
            path,
            cache_dir or Path(".pulse-cache") / "revenuecat" / app_id,
            client=gcs_client,
            log=log,
        )
    else:
        root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"RevenueCat export directory not found: {root}")
    loaded = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT _file FROM raw_revenuecat_transactions WHERE app_id = ? AND _source = ?",
            [app_id, source],
        ).fetchall()
    }
    total = files = 0
    for f in discover_files(root):
        rel = str(f.relative_to(root)).replace("\\", "/")
        if rel in loaded and not full_refresh:
            continue
        total += load_file(
            con, app_id=app_id, source=source, path=f, rel=rel, product_prefixes=product_prefixes
        )
        files += 1
    con.execute(
        "INSERT INTO meta_loads SELECT ?, ?, 'raw_revenuecat_transactions', ?, "
        "MIN(CAST(start_time AS DATE)), MAX(CAST(start_time AS DATE)), ?, NULL, ? "
        "FROM raw_revenuecat_transactions WHERE app_id = ? AND _source = ?",
        [app_id, source, total, utcnow(), f"{files} new file(s)", app_id, source],
    )
    log(
        f"    revenuecat    -> raw_revenuecat_transactions {total:>5} rows from {files} new file(s)"
    )
    return total


__all__ = ["ingest", "load_file", "discover_files", "file_timestamp", "sync_gcs", "RC_COLUMNS"]
