"""`pulse_sdk` source: the collector's local store -> warehouse raw_pulse_* tables.

    apps:
      - id: pebble_pop
        sources:
          pulse_sdk: {bundle_ids: [com.example.pebblepop]}   # data_dir defaults to collector.data_dir

Apps that pulse.yaml does not list are added from the registry automatically
(collector/registry.py), each with a pulse_sdk source of its own bundle id.

Every run reloads the lookback window (dates in the app's time zone): events
of deleted installs are left out, so a deletion reaches the warehouse with the
next run. The same run purges deleted installs and data older than the raw
retention from the NDJSON segments (segments written in the last two hours are
left for the next run, because the collector may still be appending to them)
and marks a deletion request done once no segment holds that install.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import duckdb

from ..contract import spec
from ..warehouse import utcnow
from .store import connect_db

ACTIVE_S = 7200
_DUCK = {"STRING": "VARCHAR", "INT64": "BIGINT", "TIMESTAMP": "BIGINT", "BOOL": "BOOLEAN"}
_purged: dict[str, float] = {}


def ts(ms: int | None) -> dt.datetime | None:
    return None if ms is None else dt.datetime.fromtimestamp(ms / 1000, dt.UTC).replace(tzinfo=None)


def _columns(table: str) -> dict[str, str]:
    return {c["name"]: _DUCK[c["type"]] for c in spec()["storage"][table]["columns"]}


def _files(root: Path) -> list[str]:
    return sorted(p.as_posix() for p in root.glob("dt=*/*.ndjson"))


def _deleted(data_dir: Path) -> list[str]:
    db = data_dir / "collector.sqlite3"
    if not db.exists():
        return []
    con = connect_db(db)
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT install_id FROM deletion_requests")]
    finally:
        con.close()


def purge(
    data_dir: Path,
    *,
    retention_days: int | None = None,
    now: float | None = None,
    log: Any = print,
) -> dict[str, int]:
    """Remove deleted installs and expired days from the segments; close done requests."""
    data_dir = Path(data_dir)
    now = time.time() if now is None else now
    retention_days = retention_days or spec()["limits"]["raw_retention_days"]
    deleted = set(_deleted(data_dir))
    cutoff = (dt.datetime.fromtimestamp(now, dt.UTC) - dt.timedelta(days=retention_days)).date()
    stats = {"expired_days": 0, "rewritten": 0, "removed_rows": 0, "requests_done": 0}
    still: set[str] = set()
    for sub in ("events", "quarantine"):
        root = data_dir / sub
        for day_dir in sorted(root.glob("dt=*")):
            try:
                day = dt.date.fromisoformat(day_dir.name[3:])
            except ValueError:
                continue
            if day < cutoff:
                for f in day_dir.glob("*.ndjson"):
                    f.unlink()
                try:
                    day_dir.rmdir()
                except OSError:
                    pass
                stats["expired_days"] += 1
                continue
            if not deleted:
                continue
            for f in sorted(day_dir.glob("*.ndjson")):
                active = now - f.stat().st_mtime < ACTIVE_S
                keep, hit = [], 0
                with open(f, "rb") as fh:
                    for line in fh:
                        try:
                            inst = json.loads(line).get("install_id")
                        except ValueError:
                            inst = None
                        if inst in deleted:
                            hit += 1
                            if active:
                                still.add(inst)
                            continue
                        keep.append(line)
                if hit and not active:
                    tmp = f.with_name(f.name + ".purge")
                    with open(tmp, "wb") as out:
                        out.writelines(keep)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(tmp, f)
                    stats["rewritten"] += 1
                    stats["removed_rows"] += hit
    db = data_dir / "collector.sqlite3"
    if deleted and db.exists():
        con = connect_db(db)
        try:
            ms = int(now * 1000)
            for inst in deleted - still:
                cur = con.execute(
                    "UPDATE deletion_requests SET status = 'done', done_ms = ? "
                    "WHERE install_id = ? AND status <> 'done'",
                    (ms, inst),
                )
                stats["requests_done"] += cur.rowcount
        finally:
            con.close()
    if any(stats.values()):
        log(
            f"    pulse_sdk purge -> {stats['removed_rows']} row(s) of deleted installs removed, "
            f"{stats['expired_days']} expired day(s), {stats['requests_done']} deletion request(s) done"
        )
    return stats


def _stage(
    con: duckdb.DuckDBPyConnection, data_dir: Path, start: dt.date, end: dt.date, tag: str
) -> int:
    """Load the window's events and quarantine once per run into TEMP tables."""
    lo = int(
        dt.datetime.combine(start - dt.timedelta(days=1), dt.time(), dt.UTC).timestamp() * 1000
    )
    hi = int(dt.datetime.combine(end + dt.timedelta(days=2), dt.time(), dt.UTC).timestamp() * 1000)
    deleted = _deleted(data_dir)
    n = 0
    for table, ts_col in (("events", "ts"), ("quarantine", "received_at")):
        name = f"pulse_stage_{table}_{tag}"
        files = _files(data_dir / table)
        cols = _columns(table)
        con.execute(f"DROP TABLE IF EXISTS {name}")
        if not files:
            con.execute(
                f"CREATE TEMP TABLE {name} ("
                + ", ".join(f'"{c}" {t}' for c, t in cols.items())
                + ")"
            )
            continue
        spec_cols = "{" + ", ".join(f"'{c}': '{t}'" for c, t in cols.items()) + "}"
        con.execute(
            f"CREATE TEMP TABLE {name} AS SELECT * FROM read_ndjson(?, columns = {spec_cols}, "
            f"ignore_errors = true) WHERE {ts_col} IS NOT NULL AND {ts_col} BETWEEN ? AND ? "
            "AND (install_id IS NULL OR NOT list_contains(?, install_id))",
            [files, lo, hi, deleted],
        )
        if table == "events":
            n = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    return n


def bigquery_sql(dialect: Any, table: str, start: dt.date, end: dt.date) -> str:
    """The window of one collector table in BigQuery (cloud mode), in the local store's shape.

    TIMESTAMP columns come back as epoch milliseconds, like the NDJSON segments. The
    events table is partitioned by DATE(received_at): the received_at bound prunes it
    (an event is never received before it happened). Installs with a deletion request
    are left out, as `_stage` does for the local store.
    """
    cols = spec()["storage"][table]["columns"]
    sel = ", ".join(
        f"{dialect.epoch_ms(c['name'])} AS {c['name']}" if c["type"] == "TIMESTAMP" else c["name"]
        for c in cols
    )
    ts_col = "ts" if table == "events" else "received_at"
    lo, hi = (
        dialect.ts_lit(start - dt.timedelta(days=1)),
        dialect.ts_lit(end + dt.timedelta(days=2)),
    )
    deleted = dialect.table("deletion_requests")
    return (
        f"SELECT {sel} FROM {dialect.table(table)} "
        f"WHERE received_at >= {lo} AND {ts_col} >= {lo} AND {ts_col} < {hi} "
        f"AND (install_id IS NULL OR install_id NOT IN "
        f"(SELECT install_id FROM {deleted} WHERE install_id IS NOT NULL))"
    )


REGISTRY_MS = {"first_seen": "first_seen_ms", "last_seen": "last_seen_ms",
               "status_changed_at": "status_changed_ms", "store_checked_at": "store_checked_ms"}  # fmt: skip


def bigquery_registry_sql(dialect: Any) -> str:
    """The cloud collector's app_registry in the local registry's shape (times in ms)."""
    cols = spec()["storage"]["app_registry"]["columns"]
    sel = ", ".join(
        f"{dialect.epoch_ms(c['name'])} AS {REGISTRY_MS.get(c['name'], c['name'])}"
        if c["type"] == "TIMESTAMP" else c["name"]
        for c in cols
    )  # fmt: skip
    return f"SELECT {sel} FROM {dialect.table('app_registry')} ORDER BY app_id, platform"


def stage_rows(con: duckdb.DuckDBPyConnection, tag: str, tables: dict[str, Any]) -> int:
    """TEMP stage tables from Arrow tables (BigQuery results), typed like `_stage`'s."""
    n = 0
    for table in ("events", "quarantine"):
        name = f"pulse_stage_{table}_{tag}"
        cols = _columns(table)
        con.execute(f"DROP TABLE IF EXISTS {name}")
        con.execute(
            f"CREATE TEMP TABLE {name} (" + ", ".join(f'"{c}" {t}' for c, t in cols.items()) + ")"
        )
        data = tables.get(table)
        if data is not None and data.num_rows:
            con.register("pulse_stage_src", data)
            try:
                con.execute(
                    f"INSERT INTO {name} SELECT "
                    + ", ".join(f'CAST("{c}" AS {t})' for c, t in cols.items())
                    + " FROM pulse_stage_src"
                )
            finally:
                con.unregister("pulse_stage_src")
        if table == "events":
            n = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    return n


def ingest(
    con: duckdb.DuckDBPyConnection,
    *,
    app_id: str,
    source: str,
    data_dir: Path | None,
    bundle_ids: list[str],
    timezone: str,
    start: dt.date,
    end: dt.date,
    log: Any = print,
    stage: Any = None,
    stage_key: str | None = None,
    registry: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Load one app's window. `stage(con, tag)` (cloud mode) fills the stage tables
    instead of the local NDJSON store; `stage_key` names that source for the tag."""
    if stage is None:
        data_dir = Path(data_dir)  # type: ignore[arg-type]
        key = f"{data_dir.resolve()}|{start}|{end}"
    else:
        key = f"{stage_key}|{start}|{end}"
    tag = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]  # a name
    if stage is None and _purged.get(key, -1e9) < time.monotonic() - 60:  # once per run per folder
        purge(data_dir, log=log)  # type: ignore[arg-type]
        _purged[key] = time.monotonic()
    try:
        con.execute(f"SELECT 1 FROM pulse_stage_events_{tag} LIMIT 0")
    except duckdb.Error:
        if stage is None:
            _stage(con, data_dir, start, end, tag)  # type: ignore[arg-type]
        else:
            stage(con, tag)
    now = utcnow()
    local_date = "CAST(timezone(?, epoch_ms({c})::TIMESTAMPTZ) AS DATE)"
    con.execute("BEGIN TRANSACTION")
    try:
        for t in ("raw_pulse_events", "raw_pulse_quarantine", "raw_pulse_app_registry"):
            con.execute(f"DELETE FROM {t} WHERE app_id = ? AND _source = ?", [app_id, source])
        con.execute(
            f"""
INSERT INTO raw_pulse_events BY NAME
SELECT * FROM (
  SELECT ? AS app_id, app_id AS bundle_id, platform, {local_date.format(c="ts")} AS date,
         event_id, seq, name, epoch_ms(ts) AS ts, epoch_ms(ts_client) AS ts_client,
         epoch_ms(received_at) AS received_at, skew_ms, late, sid, snum, fg_ms,
         CAST(params AS JSON) AS params, app_version, build, build_env, install_id, install_store,
         country, device_tier, os, device_model, sdk, engine, locale, consent, child, schema,
         ? AS _source, ? AS _loaded_at
  FROM pulse_stage_events_{tag} WHERE list_contains(?, app_id)
) WHERE date BETWEEN ? AND ?""",
            [app_id, timezone, source, now, bundle_ids, start, end],
        )
        con.execute(
            f"""
INSERT INTO raw_pulse_quarantine BY NAME
SELECT * FROM (
  SELECT ? AS app_id, app_id AS bundle_id, platform, {local_date.format(c="received_at")} AS date,
         split_part(reason, ':', 1) AS reason, name, COUNT(*) AS events,
         ? AS _source, ? AS _loaded_at
  FROM pulse_stage_quarantine_{tag} WHERE list_contains(?, app_id)
  GROUP BY ALL
) WHERE date BETWEEN ? AND ?""",
            [app_id, timezone, source, now, bundle_ids, start, end],
        )
        from .registry import read_registry

        if registry is None:
            registry = read_registry(data_dir) if data_dir is not None else []
        reg = [r for r in registry if r["app_id"] in bundle_ids]
        for r in reg:
            con.execute(
                "INSERT INTO raw_pulse_app_registry BY NAME SELECT ? AS app_id, ? AS bundle_id, "
                "? AS platform, ? AS display_name, ? AS genre, ? AS status, ? AS first_seen, "
                "? AS last_seen, ? AS first_version, ? AS last_version, ? AS status_changed_at, "
                "? AS store_listed, ? AS store_version, ? AS store_url, ? AS store_checked_at, "
                "? AS _source, ? AS _loaded_at",
                [
                    app_id,
                    r["app_id"],
                    r["platform"],
                    r["display_name"],
                    r["genre"],
                    r["status"],
                    ts(r["first_seen_ms"]),
                    ts(r["last_seen_ms"]),
                    r["first_version"],
                    r["last_version"],
                    ts(r["status_changed_ms"]),
                    None if r["store_listed"] is None else bool(r["store_listed"]),
                    r["store_version"],
                    r["store_url"],
                    ts(r["store_checked_ms"]),
                    source,
                    now,
                ],  # fmt: skip
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    rows, qrows, min_d, max_d = con.execute(
        "SELECT (SELECT COUNT(*) FROM raw_pulse_events WHERE app_id = ? AND _source = ?), "
        "(SELECT COALESCE(SUM(events), 0) FROM raw_pulse_quarantine WHERE app_id = ? AND _source = ?), "
        "MIN(date), MAX(date) FROM raw_pulse_events WHERE app_id = ? AND _source = ?",
        [app_id, source] * 3,
    ).fetchone()
    con.execute(
        "INSERT INTO meta_loads VALUES (?, ?, 'raw_pulse_events', ?, ?, ?, ?, NULL, ?)",
        [app_id, source, rows, min_d, max_d, now,
         f"{len(bundle_ids)} bundle id(s), {qrows} quarantined event(s), {len(reg)} registry row(s)"],
    )  # fmt: skip
    log(
        f"    pulse_sdk     -> {rows} events, {qrows} quarantined, registry {len(reg)} row(s)"
        f" ({', '.join(bundle_ids)})"
    )
    return {"events": rows, "quarantined": qrows, "registry": len(reg)}
