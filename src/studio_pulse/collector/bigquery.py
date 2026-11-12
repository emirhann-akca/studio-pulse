"""Cloud mode: BigQuery as the collector's store (Cloud Run, `--max-instances=1`).

Events and quarantined rows go through the BigQuery Storage Write API default
stream (gRPC; the first 2 TiB per month are free, see docs/COLLECTOR.md), as
Arrow record batches built from the contract's storage columns. The append is
synchronous: the collector answers 2xx only after BigQuery acknowledged it.

The app registry is one MERGE per (app_id, platform) on first sight (an
in-process cache and lock make it one statement even under concurrent first
batches; MERGE keeps it one row even across instances). Dedupe in cloud mode is
an in-memory LRU of event ids and (install_id, seq) (one instance), backed by
the warehouse's own dedupe in stg_pulse_events.

Needs the optional extra `cloud` (google-cloud-bigquery-storage,
google-cloud-bigquery). Tested only with fake clients in this repository; it
has not been run against a real project.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
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


class LRUSet:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._d: OrderedDict[Any, None] = OrderedDict()

    def __contains__(self, key: Any) -> bool:
        if key in self._d:
            self._d.move_to_end(key)
            return True
        return False

    def add(self, key: Any) -> None:
        self._d[key] = None
        self._d.move_to_end(key)
        while len(self._d) > self.capacity:
            self._d.popitem(last=False)


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
        dedupe_capacity: int = 2_000_000,
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
        self._seen = LRUSet(dedupe_capacity)
        self._lock = threading.Lock()
        self._registered: set[tuple[str, str]] = set()
        self._reg_lock = threading.Lock()
        self._deleted: set[str] | None = None
        self.merges = 0

    @classmethod
    def from_settings(cls, settings: Any) -> BigQueryStore:
        bq = settings.bigquery
        if not bq.get("project") or not bq.get("dataset"):
            raise RuntimeError("cloud mode needs bigquery.project and bigquery.dataset")
        return cls(bq["project"], bq["dataset"], location=bq.get("location"))

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
            return True

    def touch_app(self, app_id: str, platform: str, now_ms: int, **kw: Any) -> str:
        promote = bool(kw.get("release") or kw.get("test_store"))
        if promote or kw.get("version"):
            self._merge(app_id, platform, now_ms, version=kw.get("version"), promote=promote)
        return "testing" if promote else "new"

    def add_prod_installs(self, rows: Iterable[tuple[str, str, str]], now_ms: int) -> None:
        """Cloud mode counts production installs from the events table (`pulse apps refresh`)."""

    def registry(self) -> list[dict[str, Any]]:
        sql = (
            "SELECT app_id, platform, display_name, genre, status, "
            "UNIX_MILLIS(first_seen) AS first_seen_ms, UNIX_MILLIS(last_seen) AS last_seen_ms, "
            "first_version, last_version FROM "
            f"`{self.project}.{self.dataset}.app_registry` ORDER BY app_id, platform"
        )
        return [dict(r.items()) for r in self.query_client.query(sql).result()]

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
