"""Write GA4- and Crashlytics-export-shaped Parquet files from flat columns.

Used by the synthetic demo and by the tests: the files have the same nested
layout as the BigQuery exports (event_params as a repeated key/value record,
app_info / device / geo structs), so the exact extraction SQL that runs on
BigQuery can run on them in DuckDB.

Flat input columns (GA4): event_date (YYYYMMDD), event_timestamp (micros),
event_name, user_pseudo_id, user_first_touch_timestamp, platform, app_id,
app_version, device_category, mobile_model_name, country, event_value_in_usd,
plus one column per event parameter named p_<param> (string, int or float).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

GA4_BASE_COLUMNS = {
    "event_date": pa.string(),
    "event_timestamp": pa.int64(),
    "event_name": pa.string(),
    "user_pseudo_id": pa.string(),
    "user_first_touch_timestamp": pa.int64(),
    "platform": pa.string(),
    "app_id": pa.string(),
    "app_version": pa.string(),
    "device_category": pa.string(),
    "mobile_model_name": pa.string(),
    "country": pa.string(),
    "event_value_in_usd": pa.float64(),
}


def flat_table(columns: Mapping[str, Sequence[Any]], param_types: Mapping[str, str]) -> pa.Table:
    """Build a typed flat table. param_types maps param name -> 'string'|'int'|'double'."""
    arrays, names = [], []
    for name, typ in GA4_BASE_COLUMNS.items():
        arrays.append(pa.array(columns.get(name, [None] * len(columns["event_name"])), type=typ))
        names.append(name)
    pa_types = {"string": pa.string(), "int": pa.int64(), "double": pa.float64()}
    for p, t in param_types.items():
        col = f"p_{p}"
        values = columns.get(col, [None] * len(columns["event_name"]))
        arrays.append(pa.array(values, type=pa_types[t]))
        names.append(col)
    return pa.Table.from_arrays(arrays, names=names)


def _param_items(flat: pa.Table) -> str:
    items = []
    for field in flat.schema:
        if not field.name.startswith("p_"):
            continue
        key, col = field.name[2:], f'"{field.name}"'
        s = i = f = "NULL"
        if pa.types.is_string(field.type):
            s = col
        elif pa.types.is_integer(field.type):
            i = col
        else:
            f = col
        items.append(
            "{'key': '" + key + "', 'value': {"
            f"'string_value': CAST({s} AS VARCHAR), 'int_value': CAST({i} AS BIGINT), "
            f"'float_value': CAST(NULL AS DOUBLE), 'double_value': CAST({f} AS DOUBLE)"
            "}}"
        )
    if not items:
        return "[]"
    return (
        "list_filter([" + ", ".join(items) + "], lambda p: p.value.string_value IS NOT NULL "
        "OR p.value.int_value IS NOT NULL OR p.value.double_value IS NOT NULL)"
    )


def write_ga4_parquet(flat: pa.Table, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.register("flat", flat)
    con.execute(
        f"""
        COPY (
            SELECT
                event_date,
                event_timestamp,
                event_name,
                {_param_items(flat)} AS event_params,
                user_pseudo_id,
                CAST(NULL AS VARCHAR) AS user_id,
                user_first_touch_timestamp,
                platform,
                {{'id': app_id, 'version': app_version}} AS app_info,
                {{'category': device_category, 'mobile_model_name': mobile_model_name}} AS device,
                {{'country': country}} AS geo,
                event_value_in_usd
            FROM flat
            ORDER BY event_date, event_timestamp
        ) TO '{str(out).replace("'", "''")}' (FORMAT parquet)
        """
    )
    con.close()
    return out


def write_crashlytics_parquet(rows: Mapping[str, Sequence[Any]], out_path: str | Path) -> Path:
    """rows: event_timestamp (datetime), platform, is_fatal, installation_uuid, issue_id, app_version."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    flat = pa.table(
        {
            "event_timestamp": pa.array(rows["event_timestamp"], type=pa.timestamp("us")),
            "platform": pa.array(rows["platform"], type=pa.string()),
            "is_fatal": pa.array(rows["is_fatal"], type=pa.bool_()),
            "installation_uuid": pa.array(rows["installation_uuid"], type=pa.string()),
            "issue_id": pa.array(rows["issue_id"], type=pa.string()),
            "app_version": pa.array(rows["app_version"], type=pa.string()),
        }
    )
    con = duckdb.connect()
    con.register("flat", flat)
    con.execute(
        f"""
        COPY (
            SELECT platform, is_fatal, issue_id, event_timestamp, installation_uuid,
                   {{'display_version': app_version, 'build_version': app_version}} AS application
            FROM flat ORDER BY event_timestamp
        ) TO '{str(out).replace("'", "''")}' (FORMAT parquet)
        """
    )
    con.close()
    return out
