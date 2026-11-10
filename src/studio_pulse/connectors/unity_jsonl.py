"""Unity client telemetry delivered as JSON lines (Studio.Pulse JsonLinesSink).

One event per line, see docs/EVENTS.md:

  {"v":1,"event":"pulse_perf","ts":"2026-10-01T12:00:30.000Z","app":"com.example.game",
   "app_version":"1.4.0","platform":"ANDROID","session":"6f1c...",
   "params":{"pulse_v":1,"tier":"low","scene":"Level","window_s":30.0,"frames":1712,
             "ft_p50_ms":16.7,"ft_p95_ms":24.1,"ft_max_ms":71.3,"slow_frames":9,
             "mem_alloc_mb":412,"mem_reserved_mb":520,"sys_mem_mb":3072}}

The same events can also reach the warehouse through GA4 (Firebase sink); the
GA4 connector reads them from event_params with identical names.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .. import events as E
from ..warehouse import replace_slice, utcnow


@dataclass
class ParseStats:
    lines: int = 0
    perf: int = 0
    load: int = 0
    skipped_other_app: int = 0
    unknown_event: int = 0
    malformed: int = 0


def _ts_to_date(ts: str) -> dt.date:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(dt.UTC).date()


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(round(f)) if f is not None else None


def parse_lines(lines: list[str], app: str | None = None) -> tuple[list[dict], list[dict], ParseStats]:
    perf, load, st = [], [], ParseStats()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        st.lines += 1
        try:
            ev = json.loads(raw)
            if ev.get("v") != E.SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            name, params = ev["event"], ev.get("params") or {}
            day = _ts_to_date(ev["ts"])
        except (ValueError, KeyError, TypeError, AttributeError):
            st.malformed += 1
            continue
        if app and ev.get("app") != app:
            st.skipped_other_app += 1
            continue
        common = {
            "date": day,
            "platform": E.normalize_platform(ev.get("platform")),
            "app_version": ev.get("app_version"),
            "device_tier": params.get(E.PARAM_TIER) or "unknown",
        }
        if name == E.PERF_EVENT:
            perf.append(
                {
                    **common,
                    "scene": params.get(E.PARAM_SCENE),
                    "window_s": _f(params.get(E.PARAM_WINDOW_S)),
                    "frames": _i(params.get(E.PARAM_FRAMES)),
                    "ft_p50_ms": _f(params.get(E.PARAM_FT_P50)),
                    "ft_p95_ms": _f(params.get(E.PARAM_FT_P95)),
                    "ft_max_ms": _f(params.get(E.PARAM_FT_MAX)),
                    "slow_frames": _i(params.get(E.PARAM_SLOW_FRAMES)),
                    "mem_alloc_mb": _f(params.get(E.PARAM_MEM_ALLOC)),
                    "mem_reserved_mb": _f(params.get(E.PARAM_MEM_RESERVED)),
                }
            )
            st.perf += 1
        elif name == E.LOAD_EVENT:
            load.append(
                {
                    **common,
                    "kind": params.get(E.PARAM_KIND),
                    "scene": params.get(E.PARAM_SCENE),
                    "duration_ms": _f(params.get(E.PARAM_DURATION)),
                }
            )
            st.load += 1
        else:
            st.unknown_event += 1
    return perf, load, st


def _read(path: Path) -> list[str]:
    data = path.read_bytes()
    if path.name.endswith(".gz"):
        data = gzip.decompress(data)
    return data.decode("utf-8").splitlines()


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    path: str | Path,
    app: str | None = None,
    log: Any = print,
) -> ParseStats:
    root = Path(path)
    files = [root] if root.is_file() else sorted([*root.rglob("*.jsonl"), *root.rglob("*.jsonl.gz")])
    total = ParseStats()
    for f in files:
        perf, load, st = parse_lines(_read(f), app=app)
        key = f.name
        replace_slice(con, "raw_perf_windows", perf, app_id=app_id, source=f"{source}:{key}")
        replace_slice(con, "raw_load_times", load, app_id=app_id, source=f"{source}:{key}")
        for k in vars(total):
            setattr(total, k, getattr(total, k) + getattr(st, k))
    con.execute(
        "INSERT INTO meta_loads SELECT ?, ?, 'raw_perf_windows', ?, MIN(date), MAX(date), ?, NULL, ? "
        "FROM raw_perf_windows WHERE app_id = ? AND starts_with(_source, ?)",
        [app_id, source, total.perf, utcnow(), f"{len(files)} file(s), {total.malformed} malformed line(s)", app_id, source + ":"],
    )
    log(
        f"    unity_jsonl   -> perf {total.perf}, load {total.load} rows from {len(files)} file(s)"
        f" (malformed {total.malformed}, other events {total.unknown_event})"
    )
    return total


__all__ = ["ingest", "parse_lines", "ParseStats"]
