"""Model fingerprint: every stg_* / fct_* table as sorted CSV plus its SHA-256.

CI builds the demo on Windows, macOS and Linux and compares these hashes, so a
platform difference in a connector, a model or the generator shows up as a
changed table instead of going unnoticed.

To be comparable the export is canonical:
- columns in table order, minus load timestamps (`loaded_at` columns record
  when the pipeline ran, not what it computed);
- floats written with 12 significant digits, so a last-bit difference from a
  parallel sum does not count, while any real difference does;
- rows formatted first and then sorted as text, so ties and float noise cannot
  reorder them; LF line endings, UTF-8.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import duckdb

VOLATILE = ("loaded_at",)


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        s = f"{v:.12g}"
        return "0" if s == "-0" else s
    if isinstance(v, decimal.Decimal):
        return _cell(float(v))
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_cell(x) for x in v) + "]"
    s = str(v)
    if any(ch in s for ch in ',"\n\r'):
        s = '"' + s.replace('"', '""') + '"'
    return s


def model_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND (table_name LIKE 'stg\\_%' ESCAPE '\\' "
        "OR table_name LIKE 'fct\\_%' ESCAPE '\\') ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def table_csv(con: duckdb.DuckDBPyConnection, table: str) -> tuple[str, int]:
    cols = [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
            "ORDER BY ordinal_position",
            [table],
        ).fetchall()
        if not any(v in r[0] for v in VOLATILE)
    ]
    quoted = ", ".join(f'"{c}"' for c in cols)
    rows = con.execute(f"SELECT {quoted} FROM {table}").fetchall()
    lines = sorted(",".join(_cell(v) for v in row) for row in rows)
    return ",".join(cols) + "\n" + "".join(line + "\n" for line in lines), len(rows)


def fingerprint(con: duckdb.DuckDBPyConnection, out_dir: Path | None = None) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    for t in model_tables(con):
        text, n = table_csv(con, t)
        data = text.encode("utf-8")
        tables[t] = {"rows": n, "sha256": hashlib.sha256(data).hexdigest()}
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{t}.csv").write_bytes(data)
    combined = hashlib.sha256(
        "".join(f"{t}:{v['sha256']}\n" for t, v in sorted(tables.items())).encode("ascii")
    ).hexdigest()
    result = {"sha256": combined, "tables": tables}
    if out_dir is not None:
        (out_dir / "fingerprint.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return result
