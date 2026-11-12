"""Local storage of the collector: NDJSON segments (events, quarantine) + SQLite state.

Events are appended to `events/dt=YYYY-MM-DD/HH-<pid>.ndjson` (UTC hour of
receipt), quarantined events to `quarantine/...` in the same layout. Every
batch is written with one write, flushed and fsync'ed before the collector
answers 2xx, so an acknowledged event survives a crash or power loss. New
segment directories are fsync'ed too (POSIX).

collector.sqlite3 (WAL) holds what must be queried and updated across
processes: the app registry (the collector writes it, `pulse apps refresh`
and the runner read and update it), the dedupe index (event ids and
(install_id, seq) seen in the last `dedupe_days`), production installs per app
(for live detection, until the app is live) and deletion requests. SQLite
because the collector and other `pulse` commands use it at the same time; a
DuckDB file allows only one writing process.

No IP address is written anywhere; see collector/geo.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_registry (
    app_id            TEXT NOT NULL,
    platform          TEXT NOT NULL,
    display_name      TEXT,
    genre             TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    first_seen_ms     INTEGER NOT NULL,
    last_seen_ms      INTEGER,
    first_version     TEXT,
    last_version      TEXT,
    seen_release      INTEGER NOT NULL DEFAULT 0,
    seen_test_store   INTEGER NOT NULL DEFAULT 0,
    stores_seen       TEXT,
    status_changed_ms INTEGER,
    store_listed      INTEGER,
    store_version     TEXT,
    store_url         TEXT,
    store_name        TEXT,
    store_checked_ms  INTEGER,
    store_note        TEXT,
    PRIMARY KEY (app_id, platform)
);
CREATE TABLE IF NOT EXISTS registry_prod_installs (
    app_id         TEXT NOT NULL,
    platform       TEXT NOT NULL,
    install_id     TEXT NOT NULL,
    first_seen_ms  INTEGER NOT NULL,
    last_seen_ms   INTEGER NOT NULL,
    PRIMARY KEY (app_id, platform, install_id)
);
CREATE TABLE IF NOT EXISTS seen_events (
    install_id   TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    seq          INTEGER,
    received_ms  INTEGER NOT NULL,
    PRIMARY KEY (install_id, event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS seen_events_seq ON seen_events (install_id, seq);
CREATE INDEX IF NOT EXISTS seen_events_received ON seen_events (received_ms);
CREATE TABLE IF NOT EXISTS deletion_requests (
    install_id    TEXT NOT NULL,
    app_id        TEXT,
    requested_ms  INTEGER NOT NULL,
    reason        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    done_ms       INTEGER,
    PRIMARY KEY (install_id, requested_ms)
);
"""

REGISTRY_COLUMNS = (
    "app_id", "platform", "display_name", "genre", "status", "first_seen_ms", "last_seen_ms",
    "first_version", "last_version", "seen_release", "seen_test_store", "stores_seen",
    "status_changed_ms", "store_listed", "store_version", "store_url", "store_name",
    "store_checked_ms", "store_note",
)  # fmt: skip


def connect_db(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """A SQLite connection in WAL mode with a generous busy timeout."""
    if read_only:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.executescript(SCHEMA)
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def _fsync_dir(path: Path) -> None:
    if sys.platform == "win32":  # directories cannot be opened for fsync on Windows
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SegmentWriter:
    """Appends NDJSON lines to hourly segment files; durable when write() returns."""

    def __init__(self, root: Path, *, fsync: bool = True) -> None:
        self.root = root
        self.fsync = fsync
        self._files: dict[str, BinaryIO] = {}
        self._lock = threading.Lock()

    def _file(self, received_ms: int) -> BinaryIO:
        t = time.gmtime(received_ms / 1000)
        day = time.strftime("%Y-%m-%d", t)
        key = f"{day}/{t.tm_hour:02d}"
        f = self._files.get(key)
        if f is None:
            for old in list(self._files):  # one open segment at a time
                self._files.pop(old).close()
            d = self.root / f"dt={day}"
            new_dir = not d.exists()
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{t.tm_hour:02d}-{os.getpid()}.ndjson"
            f = open(p, "ab")  # noqa: SIM115 (kept open across batches)
            self._files[key] = f
            if self.fsync:
                if new_dir:
                    _fsync_dir(self.root)
                _fsync_dir(d)
        return f

    def write(self, rows: list[dict[str, Any]], received_ms: int) -> None:
        if not rows:
            return
        data = b"".join(
            json.dumps(r, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
            for r in rows
        )
        with self._lock:
            f = self._file(received_ms)
            f.write(data)
            f.flush()
            if self.fsync:
                os.fsync(f.fileno())

    def close(self) -> None:
        with self._lock:
            for f in self._files.values():
                f.close()
            self._files.clear()


class LocalStore:
    """Events and quarantine as NDJSON segments; registry, dedupe and deletions in SQLite."""

    kind = "local"

    def __init__(self, data_dir: Path, *, fsync: bool = True, dedupe_days: int = 7) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.events = SegmentWriter(self.data_dir / "events", fsync=fsync)
        self.quarantine = SegmentWriter(self.data_dir / "quarantine", fsync=fsync)
        self.db_path = self.data_dir / "collector.sqlite3"
        self._db = connect_db(self.db_path)
        self._db_lock = threading.Lock()
        self.dedupe_ms = dedupe_days * 86_400_000
        self._last_prune = 0

    # -- events -----------------------------------------------------------------------

    def write(self, rows: list[dict], quarantine: list[dict], received_ms: int) -> None:
        self.quarantine.write(quarantine, received_ms)
        self.events.write(rows, received_ms)

    def seen(self, keys: list[tuple[str, str, int | None]]) -> set[int]:
        """Indexes of keys (event_id, install_id, seq) already stored."""
        if not keys:
            return set()
        dup: set[int] = set()
        with self._db_lock:
            cur = self._db.cursor()
            for i, (eid, inst, seq) in enumerate(keys):
                row = cur.execute(
                    "SELECT 1 FROM seen_events WHERE install_id = ? AND (event_id = ? OR seq = ?) LIMIT 1",
                    (inst, eid, seq),
                ).fetchone()
                if row:
                    dup.add(i)
        return dup

    def mark_seen(self, keys: list[tuple[str, str, int | None]], received_ms: int) -> None:
        with self._db_lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "INSERT OR IGNORE INTO seen_events VALUES (?, ?, ?, ?)",
                    [(i, e, s, received_ms) for e, i, s in keys],
                )
                if received_ms - self._last_prune > 3_600_000:
                    self._db.execute(
                        "DELETE FROM seen_events WHERE received_ms < ?",
                        (received_ms - self.dedupe_ms,),
                    )
                    self._last_prune = received_ms
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    # -- registry ---------------------------------------------------------------------

    def register_app(
        self,
        app_id: str,
        platform: str,
        now_ms: int,
        *,
        name: str | None,
        genre: str,
        version: str | None,
    ) -> bool:
        """Create the (app_id, platform) row if missing; True when this call created it."""
        with self._db_lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO app_registry (app_id, platform, display_name, genre, status, "
                "first_seen_ms, last_seen_ms, first_version, last_version, status_changed_ms) "
                "VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)",
                (app_id, platform, name or app_id, genre, now_ms, now_ms, version, version, now_ms),
            )
            return cur.rowcount == 1

    def touch_app(
        self,
        app_id: str,
        platform: str,
        now_ms: int,
        *,
        version: str | None,
        release: bool,
        test_store: bool,
        store: str | None,
        genre: str | None,
        name: str | None,
    ) -> str:
        """Update last seen / flags; promote new -> testing. Returns the status."""
        with self._db_lock:
            row = self._db.execute(
                "SELECT status, stores_seen, genre, display_name FROM app_registry "
                "WHERE app_id = ? AND platform = ?",
                (app_id, platform),
            ).fetchone()
            if row is None:
                return "new"
            stores = set(filter(None, (row["stores_seen"] or "").split(",")))
            if store:
                stores.add(store)
            status = row["status"]
            new_status = "testing" if status == "new" and (release or test_store) else status
            self._db.execute(
                "UPDATE app_registry SET last_seen_ms = MAX(COALESCE(last_seen_ms, 0), ?), "
                "last_version = COALESCE(?, last_version), "
                "seen_release = MAX(seen_release, ?), seen_test_store = MAX(seen_test_store, ?), "
                "stores_seen = ?, status = ?, "
                "status_changed_ms = CASE WHEN status <> ? THEN ? ELSE status_changed_ms END, "
                "genre = CASE WHEN genre = 'app' AND ? = 'game' THEN 'game' ELSE genre END, "
                "display_name = CASE WHEN display_name = app_id AND ? IS NOT NULL THEN ? "
                "ELSE display_name END "
                "WHERE app_id = ? AND platform = ?",
                (
                    now_ms,
                    version,
                    int(release),
                    int(test_store),
                    ",".join(sorted(stores)),
                    new_status,
                    new_status,
                    now_ms,
                    genre,
                    name,
                    name,
                    app_id,
                    platform,
                ),  # fmt: skip
            )
            return new_status

    def add_prod_installs(self, rows: Iterable[tuple[str, str, str]], now_ms: int) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._db_lock:
            self._db.executemany(
                "INSERT INTO registry_prod_installs VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (app_id, platform, install_id) DO UPDATE SET last_seen_ms = excluded.last_seen_ms",
                [(a, p, i, now_ms, now_ms) for a, p, i in rows],
            )

    def registry(self) -> list[dict[str, Any]]:
        with self._db_lock:
            rows = self._db.execute(
                f"SELECT {', '.join(REGISTRY_COLUMNS)} FROM app_registry ORDER BY app_id, platform"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- deletions --------------------------------------------------------------------

    def add_deletion(self, install_id: str, app_id: str | None, reason: str, now_ms: int) -> None:
        with self._db_lock:
            self._db.execute(
                "INSERT OR IGNORE INTO deletion_requests (install_id, app_id, requested_ms, reason) "
                "VALUES (?, ?, ?, ?)",
                (install_id, app_id, now_ms, reason),
            )
            self._db.execute(
                "DELETE FROM registry_prod_installs WHERE install_id = ?", (install_id,)
            )
            self._db.execute("DELETE FROM seen_events WHERE install_id = ?", (install_id,))

    def deleted_installs(self) -> set[str]:
        with self._db_lock:
            return {
                r[0]
                for r in self._db.execute("SELECT install_id FROM deletion_requests").fetchall()
            }

    def close(self) -> None:
        self.events.close()
        self.quarantine.close()
        with self._db_lock:
            self._db.close()


def read_registry(data_dir: Path) -> list[dict[str, Any]]:
    """Registry rows for other processes (read-only; empty when there is no database)."""
    db = Path(data_dir) / "collector.sqlite3"
    if not db.exists():
        return []
    con = connect_db(db, read_only=True)
    try:
        rows = con.execute(
            f"SELECT {', '.join(REGISTRY_COLUMNS)} FROM app_registry ORDER BY app_id, platform"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def read_deletions(data_dir: Path) -> list[dict[str, Any]]:
    db = Path(data_dir) / "collector.sqlite3"
    if not db.exists():
        return []
    con = connect_db(db, read_only=True)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM deletion_requests").fetchall()]
    finally:
        con.close()
