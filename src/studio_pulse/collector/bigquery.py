"""Cloud mode: BigQuery as the collector's store (Cloud Run, `--max-instances=1`).

Events and quarantined rows go through the BigQuery Storage Write API default
stream (gRPC; the first 2 TiB per month are free, see docs/COLLECTOR.md), as
Arrow record batches built from the contract's storage columns. The append is
synchronous: the collector answers 2xx only after BigQuery acknowledged it.

The app registry is one MERGE per (app_id, platform) on first sight (an
in-process cache and lock make it one statement even under concurrent first
batches; MERGE keeps it one row even across instances). Dedupe in cloud mode is
an in-memory, bounded set of event ids and (install_id, seq) (`SeenSet`, one
instance), backed by the warehouse's own dedupe in stg_pulse_events.

Every BigQuery query bills at least 10 MB per table, and a MERGE takes a second or
two, so the registry is touched sparingly: a MERGE when an app is first promoted or
shows a version this process has not seen (up to MAX_VERSIONS per app), otherwise at
most once per TOUCH_EVERY_MS per (app_id, platform); `registry()` is cached for
REGISTRY_TTL_MS (`/live` polls every 5 s). Without that, two app versions in the wild
alternate on every batch and each batch would run a MERGE.

Needs the optional extra `cloud` (google-cloud-bigquery-storage,
google-cloud-bigquery). CI tests it with fake clients only; one real deploy was
checked by hand (deploy/cloudrun/README.md).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

import pyarrow as pa

from ..contract import spec

_ARROW = {
    "STRING": pa.string(),
    "INT64": pa.int64(),
    "BOOL": pa.bool_(),
    "TIMESTAMP": pa.timestamp("us", tz="UTC"),
}


def arrow_schema(table: str) -> pa.Schema:
    cols = spec()["storage"][table]["columns"]
    return pa.schema(
        [pa.field(c["name"], _ARROW[c["type"]], nullable=c["mode"] != "REQUIRED") for c in cols]
    )


def to_arrow(rows: list[dict[str, Any]], table: str) -> pa.RecordBatch:
    """Rows with ms-integer timestamps -> an Arrow batch in the table's column order."""
    schema = arrow_schema(table)
    arrays = []
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_timestamp(f.type):
            vals = [None if v is None else int(v) * 1000 for v in vals]
        arrays.append(pa.array(vals, type=f.type))
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


class SeenSet:
    """Recently seen dedupe keys in bounded memory: two generations of 64-bit fingerprints.

    A key is remembered for at least `capacity // 2` and at most `capacity` later
    additions (when the newer generation is full it becomes the older one and the
    older one is dropped). Only `hash(key)` is kept: capacity 1 M peaked at ~92 MB RSS
    (measured), where the earlier LRU of tuples took ~470 MB for 2 M keys and, next to
    the ~175 MB of the cloud libraries, did not fit a 512 MiB container. A 64-bit collision
    could count a new event as a repeat; at a million keys the chance per event is
    ~1e-13. The warehouse dedupes again, so a forgotten key costs a stored duplicate,
    never a doubled metric.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 2:
            raise ValueError("dedupe capacity must be at least 2")
        self.capacity = capacity
        self._half = capacity // 2
        self._new: set[int] = set()
        self._old: set[int] = set()

    def __len__(self) -> int:
        return len(self._new) + len(self._old)

    def __contains__(self, key: Any) -> bool:
        h = hash(key)
        return h in self._new or h in self._old

    def add(self, key: Any) -> None:
        h = hash(key)
        if h in self._new:
            return
        self._new.add(h)
        if len(self._new) >= self._half:
            self._old, self._new = self._new, set()


TOUCH_EVERY_MS = 15 * 60_000  # last_seen in the registry: a quarter of an hour is enough
MAX_VERSIONS = 20  # new versions per app that MERGE at once; more wait for the timer
REGISTRY_TTL_MS = 60_000  # /live reads the registry at most once a minute

MERGE_SQL = """
MERGE `{table}` T
USING (SELECT @app_id AS app_id, @platform AS platform) S
ON T.app_id = S.app_id AND T.platform = S.platform
WHEN NOT MATCHED THEN
  INSERT (app_id, platform, display_name, genre, status, first_seen, last_seen, first_version,
          last_version, status_changed_at)
  VALUES (@app_id, @platform, @name, @genre, 'new', TIMESTAMP_MILLIS(@now), TIMESTAMP_MILLIS(@now),
          @version, @version, TIMESTAMP_MILLIS(@now))
WHEN MATCHED THEN
  UPDATE SET last_seen = GREATEST(T.last_seen, TIMESTAMP_MILLIS(@now)),
             last_version = COALESCE(@version, T.last_version),
             status = IF(T.status = 'new' AND @promote, 'testing', T.status)
"""


class BigQueryStore:
    kind = "bigquery"

    def __init__(
        self,
        project: str,
        dataset: str,
        *,
        write_client: Any = None,
        query_client: Any = None,
        location: str | None = None,
        dedupe_capacity: int = 1_000_000,
    ) -> None:
        self.project, self.dataset, self.location = project, dataset, location
        if write_client is None or query_client is None:
            try:
                from google.cloud import bigquery, bigquery_storage_v1
            except ImportError as exc:
                raise RuntimeError(
                    "cloud mode needs: pip install 'studio-pulse[cloud]' "
                    "(google-cloud-bigquery-storage, google-cloud-bigquery)"
                ) from exc
            write_client = write_client or bigquery_storage_v1.BigQueryWriteClient()
            query_client = query_client or bigquery.Client(project=project, location=location)
        self.write_client = write_client
        self.query_client = query_client
        self._seen = SeenSet(dedupe_capacity)
        self._lock = threading.Lock()
        self._registered: set[tuple[str, str]] = set()
        self._reg_lock = threading.Lock()
        # (app_id, platform) -> last MERGE (ms), versions merged, promoted
        self._touched: dict[tuple[str, str], tuple[int, set[str], bool]] = {}
        self._registry_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._deleted: set[str] | None = None
        self.merges = 0

    @classmethod
    def from_settings(cls, settings: Any) -> BigQueryStore:
        bq = settings.bigquery
        if not bq.get("project") or not bq.get("dataset"):
            raise RuntimeError("cloud mode needs bigquery.project and bigquery.dataset")
        return cls(
            bq["project"],
            bq["dataset"],
            location=bq.get("location"),
            dedupe_capacity=settings.dedupe_capacity,
        )

    def table_path(self, table: str) -> str:
        return f"projects/{self.project}/datasets/{self.dataset}/tables/{table}"

    def _append(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        from google.cloud.bigquery_storage_v1 import types

        batch = to_arrow(rows, table)
        request = types.AppendRowsRequest(
            write_stream=f"{self.table_path(table)}/streams/_default",
            arrow_rows=types.AppendRowsRequest.ArrowData(
                writer_schema=types.ArrowSchema(
                    serialized_schema=batch.schema.serialize().to_pybytes()
                ),
                rows=types.ArrowRecordBatch(
                    serialized_record_batch=batch.serialize().to_pybytes(), row_count=batch.num_rows
                ),
            ),
        )
        for resp in self.write_client.append_rows(iter([request])):
            if resp.error.code:
                raise OSError(f"BigQuery append to {table} failed: {resp.error.message}")
            if list(resp.row_errors):
                raise OSError(f"BigQuery rejected {len(resp.row_errors)} row(s) of {table}")
            break

    # -- store interface -----------------------------------------------------------

    def write(self, rows: list[dict], quarantine: list[dict], received_ms: int) -> None:
        self._append("quarantine", quarantine)
        self._append("events", rows)

    def seen(self, keys: list[tuple[str, str, int | None]]) -> set[int]:
        with self._lock:
            return {
                i
                for i, (e, inst, seq) in enumerate(keys)
                if ("e", inst, e) in self._seen or ("s", inst, seq) in self._seen
            }

    def mark_seen(self, keys: list[tuple[str, str, int | None]], received_ms: int) -> None:
        with self._lock:
            for e, inst, seq in keys:
                self._seen.add(("e", inst, e))
                self._seen.add(("s", inst, seq))

    def _merge(self, app_id: str, platform: str, now_ms: int, **kw: Any) -> None:
        from google.cloud import bigquery

        params = [
            bigquery.ScalarQueryParameter("app_id", "STRING", app_id),
            bigquery.ScalarQueryParameter("platform", "STRING", platform),
            bigquery.ScalarQueryParameter("name", "STRING", kw.get("name") or app_id),
            bigquery.ScalarQueryParameter("genre", "STRING", kw.get("genre") or "app"),
            bigquery.ScalarQueryParameter("version", "STRING", kw.get("version")),
            bigquery.ScalarQueryParameter("now", "INT64", now_ms),
            bigquery.ScalarQueryParameter("promote", "BOOL", bool(kw.get("promote"))),
        ]
        sql = MERGE_SQL.format(table=f"{self.project}.{self.dataset}.app_registry")
        self.query_client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
        ).result()
        self.merges += 1

    def register_app(self, app_id: str, platform: str, now_ms: int, **kw: Any) -> bool:
        with self._reg_lock:
            if (app_id, platform) in self._registered:
                return False
            self._merge(app_id, platform, now_ms, **kw)
            self._registered.add((app_id, platform))
            version = kw.get("version")
            self._touched[(app_id, platform)] = (now_ms, {version} if version else set(), False)
            self._registry_cache = None
            return True

    def touch_app(self, app_id: str, platform: str, now_ms: int, **kw: Any) -> str:
        promote = bool(kw.get("release") or kw.get("test_store"))
        version = kw.get("version")
        if not (promote or version):
            return "new"
        key = (app_id, platform)
        with self._reg_lock:
            at, versions, promoted = self._touched.get(key, (None, set(), False))
            due = (
                (promote and not promoted)
                or (bool(version) and version not in versions and len(versions) < MAX_VERSIONS)
                or at is None
                or now_ms - at >= TOUCH_EVERY_MS
            )
            if due:
                self._merge(app_id, platform, now_ms, version=version, promote=promote)
                if version and len(versions) < MAX_VERSIONS:
                    versions.add(version)
                self._touched[key] = (now_ms, versions, promoted or promote)
                self._registry_cache = None
        return "testing" if promote or promoted else "new"

    def add_prod_installs(self, rows: Iterable[tuple[str, str, str]], now_ms: int) -> None:
        """Cloud mode counts production installs from the events table (`pulse apps refresh`)."""

    def registry(self) -> list[dict[str, Any]]:
        cached = self._registry_cache
        if cached is not None and time.monotonic() * 1000 - cached[0] < REGISTRY_TTL_MS:
            return cached[1]
        sql = (
            "SELECT app_id, platform, display_name, genre, status, "
            "UNIX_MILLIS(first_seen) AS first_seen_ms, UNIX_MILLIS(last_seen) AS last_seen_ms, "
            "first_version, last_version FROM "
            f"`{self.project}.{self.dataset}.app_registry` ORDER BY app_id, platform"
        )
        rows = [dict(r.items()) for r in self.query_client.query(sql).result()]
        self._registry_cache = (time.monotonic() * 1000, rows)
        return rows

    def add_deletion(self, install_id: str, app_id: str | None, reason: str, now_ms: int) -> None:
        self._append(
            "deletion_requests",
            [{"install_id": install_id, "app_id": app_id, "requested_at": now_ms,
              "reason": reason, "status": "pending", "done_at": None}],
        )  # fmt: skip
        if self._deleted is not None:
            self._deleted.add(install_id)

    def deleted_installs(self) -> set[str]:
        if self._deleted is None:
            sql = (
                f"SELECT DISTINCT install_id FROM `{self.project}.{self.dataset}.deletion_requests`"
            )
            try:
                self._deleted = {r["install_id"] for r in self.query_client.query(sql).result()}
            except Exception:  # first deploy: the table may be empty or missing
                self._deleted = set()
        return set(self._deleted)

    def close(self) -> None:
        pass
