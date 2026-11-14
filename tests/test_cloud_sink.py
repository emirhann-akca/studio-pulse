"""Cloud mode: BigQuery Storage Write API sink and registry MERGE, with fake clients only.

No Google Cloud call is made; the fakes check what would be sent. The deploy
scripts are checked statically (and `sh -n` where a POSIX shell exists).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("google.cloud.bigquery_storage_v1")
from google.cloud.bigquery_storage_v1 import types  # noqa: E402

from studio_pulse.collector.app import create_app  # noqa: E402
from studio_pulse.collector.bigquery import BigQueryStore, arrow_schema  # noqa: E402
from studio_pulse.collector.core import Collector  # noqa: E402
from studio_pulse.collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings  # noqa: E402
from studio_pulse.contract import spec  # noqa: E402

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "contract" / "golden" / "batch_ok.json"


class FakeWrite:
    def __init__(self, fail: bool = False) -> None:
        self.requests: list[types.AppendRowsRequest] = []
        self.fail = fail
        self._lock = threading.Lock()

    def append_rows(self, requests):
        reqs = list(requests)
        with self._lock:
            self.requests += reqs
        resp = types.AppendRowsResponse()
        if self.fail:
            resp.error.code = 3
            resp.error.message = "invalid"
        return iter([resp])

    def rows(self, table: str) -> list[dict]:
        out = []
        for r in self.requests:
            if not r.write_stream.endswith(f"/tables/{table}/streams/_default"):
                continue
            schema = pa.ipc.read_schema(pa.py_buffer(r.arrow_rows.writer_schema.serialized_schema))
            batch = pa.ipc.read_record_batch(
                pa.py_buffer(r.arrow_rows.rows.serialized_record_batch), schema
            )
            assert batch.num_rows == r.arrow_rows.rows.row_count
            out += batch.to_pylist()
        return out


class FakeQuery:
    def __init__(self) -> None:
        self.sql: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in getattr(job_config, "query_parameters", [])}
        time.sleep(0.01)  # widen any race
        with self._lock:
            self.sql.append((sql, params))

        class Job:
            def result(self_inner):
                return []

        return Job()


@pytest.fixture()
def cloud():
    w, q = FakeWrite(), FakeQuery()
    store = BigQueryStore("proj", "pulse_raw", write_client=w, query_client=q)
    settings = CollectorSettings(data_dir=Path("unused"), mode="bigquery", rate_per_ip=0)
    clock = [int(time.time() * 1000)]
    col = Collector(settings, store, clock=lambda: clock[0])
    return TestClient(create_app(settings, col)), w, q, store


def golden_now() -> dict:
    body = json.loads(GOLDEN.read_text(encoding="utf-8"))
    off = int(time.time() * 1000) - body["sent_at"]
    body["sent_at"] += off
    for e in body["events"]:
        e["ts"] += off
    return body


def test_events_are_appended_as_arrow_to_the_default_stream(cloud):
    client, w, q, _ = cloud
    r = client.post("/v2/batch", json=golden_now(), headers={"X-Pulse-Key": DEFAULT_LOCAL_KEY})
    assert r.status_code == 200 and r.json()["accepted"] == 29
    assert w.requests[0].write_stream == (
        "projects/proj/datasets/pulse_raw/tables/events/streams/_default"
    )
    rows = w.rows("events")
    assert len(rows) == 29
    cols = [c["name"] for c in spec()["storage"]["events"]["columns"]]
    assert list(rows[0]) == cols
    schema = arrow_schema("events")
    assert schema.field("ts").type == pa.timestamp("us", tz="UTC")
    assert schema.field("event_id").nullable is False and schema.field("uid").nullable is True
    lf = next(x for x in rows if x["name"] == "level_fail")
    assert json.loads(lf["params"])["reason"] == "out_of_moves"
    assert lf["received_at"].tzinfo is not None and lf["app_id"] == "com.example.puzzle"
    # the same batch again: in-memory dedupe, nothing appended
    n = len(w.requests)
    again = client.post("/v2/batch", json=golden_now(), headers={"X-Pulse-Key": DEFAULT_LOCAL_KEY})
    assert again.json()["duplicates"] == 29 and len(w.requests) == n


def test_registry_merge_runs_once_under_fifty_parallel_first_batches(cloud):
    _, _, q, store = cloud
    with ThreadPoolExecutor(50) as pool:
        created = list(
            pool.map(
                lambda i: store.register_app(
                    "com.example.new", "ios", 1, name=None, genre="app", version="1"
                ),
                range(50),
            )
        )
    merges = [s for s, p in q.sql if s.lstrip().startswith("MERGE")]
    assert created.count(True) == 1 and len(merges) == 1
    sql, params = next((s, p) for s, p in q.sql if s.lstrip().startswith("MERGE"))
    assert "`proj.pulse_raw.app_registry`" in sql and params["app_id"] == "com.example.new"


def test_a_failed_append_answers_503_so_the_sdk_retries():
    w, q = FakeWrite(fail=True), FakeQuery()
    store = BigQueryStore("proj", "ds", write_client=w, query_client=q)
    s = CollectorSettings(data_dir=Path("unused"), mode="bigquery", rate_per_ip=0)
    client = TestClient(create_app(s, Collector(s, store)))
    r = client.post("/v2/batch", json=golden_now(), headers={"X-Pulse-Key": DEFAULT_LOCAL_KEY})
    assert r.status_code == 503


def test_deletion_requests_are_appended_and_honoured(cloud):
    client, w, *_ = cloud
    body = golden_now()
    inst = body["ctx"]["install_id"]
    r = client.post(
        "/v2/delete", json={"install_id": inst}, headers={"X-Pulse-Key": DEFAULT_LOCAL_KEY}
    )
    assert r.status_code == 202
    (row,) = w.rows("deletion_requests")
    assert row["install_id"] == inst and row["status"] == "pending"
    r = client.post("/v2/batch", json=body, headers={"X-Pulse-Key": DEFAULT_LOCAL_KEY})
    assert r.json()["ignored"] == 29 and w.rows("events") == []


def test_deploy_files_pin_one_instance_and_hold_no_secret():
    d = ROOT / "deploy" / "cloudrun"
    sh = (d / "deploy.sh").read_text(encoding="utf-8")
    ps = (d / "deploy.ps1").read_bytes().decode("ascii")
    for text in (sh, ps):
        assert (
            "--max-instances=1" in text and "--concurrency=200" in text and "--memory=512Mi" in text
        )
        assert "pulse-write-keys:latest" in text and "sp_pub_" not in text
        assert "contract/generated/bigquery" in text
    assert "--apply" in sh and "-Apply" in ps  # print-only unless asked
    docker = (d / "Dockerfile").read_text(encoding="utf-8")
    assert "PULSE_COLLECTOR_MODE=bigquery" in docker and "USER pulse" in docker
    readme = (d / "README.md").read_text(encoding="utf-8")
    assert "$12/month" in readme and "max-instances=1" in readme
    if shutil.which("sh"):
        assert subprocess.run(["sh", "-n", str(d / "deploy.sh")], check=False).returncode == 0
