"""Cloud mode: BigQuery Storage Write API sink and registry MERGE, with fake clients only.

No Google Cloud call is made; the fakes check what would be sent. The deploy
scripts are checked statically (and `sh -n` where a POSIX shell exists).
"""

from __future__ import annotations

import json
import re
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

from studio_pulse.collector import bigquery as bq_mod  # noqa: E402
from studio_pulse.collector.app import create_app  # noqa: E402
from studio_pulse.collector.bigquery import BigQueryStore, SeenSet, arrow_schema  # noqa: E402
from studio_pulse.collector.core import Collector  # noqa: E402
from studio_pulse.collector.settings import (  # noqa: E402
    DEFAULT_LOCAL_KEY,
    CollectorSettings,
    from_dict,
)
from studio_pulse.contract import spec  # noqa: E402

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "contract" / "golden" / "batch_ok.json"
KEY = {"X-Pulse-Key": DEFAULT_LOCAL_KEY}


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


def _merges(q) -> list[dict]:
    return [p for s, p in q.sql if s.lstrip().startswith("MERGE")]


def test_two_live_versions_do_not_run_a_merge_per_batch(cloud):
    """Each MERGE bills >= 10 MB and takes seconds; after an update two versions alternate."""
    _, _, q, store = cloud
    t, app = 1_793_000_000_000, "com.example.game"
    touch = dict(test_store=False, store="app_store", genre="game", name=None)
    store.register_app(app, "ios", t, name=None, genre="game", version="1.0")
    for i in range(200):
        store.touch_app(app, "ios", t + i * 1000, version=("1.0", "1.1")[i % 2], release=True,
                        **touch)  # fmt: skip
    # the first sight, the promotion to testing, the new version 1.1: nothing per batch
    assert len(_merges(q)) == 3 and _merges(q)[1]["promote"] is True
    store.touch_app(app, "ios", t + 16 * 60_000, version="1.0", release=True, **touch)
    assert len(_merges(q)) == 4  # last_seen moves on once per quarter of an hour
    for i in range(100):  # made-up versions cannot buy a MERGE each
        store.touch_app(app, "ios", t + 16 * 60_000 + i, version=f"9.{i}", release=True, **touch)
    assert len(_merges(q)) <= 4 + bq_mod.MAX_VERSIONS


def test_the_registry_is_read_once_a_minute_for_live(cloud):
    _, _, q, store = cloud
    selects = lambda: sum(s.lstrip().startswith("SELECT app_id") for s, _ in q.sql)  # noqa: E731
    store.registry()
    store.registry()
    assert selects() == 1
    store.register_app("com.example.new", "android", 1, name=None, genre="app", version="1")
    store.registry()  # a new app shows at once
    assert selects() == 2


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


def test_seen_set_is_bounded_and_remembers_the_newest_half():
    seen = SeenSet(1000)
    for i in range(10_000):
        seen.add(("e", "install", i))
        assert len(seen) <= 1000
    assert all(("e", "install", i) in seen for i in range(9_500, 10_000))
    assert ("e", "install", 0) not in seen and ("s", "install", 9_999) not in seen
    seen.add(("e", "install", 9_999))  # a repeat does not grow it
    assert len(seen) <= 1000
    with pytest.raises(ValueError):
        SeenSet(1)


def test_a_retry_after_the_cache_rolled_over_is_stored_again_not_lost(cloud):
    """The cache only saves duplicate rows; the warehouse dedupes what it forgot."""
    client, w, _, store = cloud
    store._seen = SeenSet(4)
    body = golden_now()
    assert client.post("/v2/batch", json=body, headers=KEY).json()["accepted"] == 29
    again = client.post("/v2/batch", json=body, headers=KEY).json()
    assert again["accepted"] + again["duplicates"] == 29 and again["accepted"] > 0


def test_dedupe_capacity_comes_from_the_environment(monkeypatch):
    s = from_dict(
        None,
        Path("unused"),
        env={"PULSE_COLLECTOR_MODE": "bigquery", "PULSE_BQ_PROJECT": "p",
             "PULSE_BQ_DATASET": "d", "PULSE_DEDUPE_CAPACITY": "5000"},
    )  # fmt: skip
    assert s.dedupe_capacity == 5000
    assert from_dict(None, Path("unused"), env={}).dedupe_capacity == 1_000_000
    from google.cloud import bigquery, bigquery_storage_v1

    monkeypatch.setattr(bigquery_storage_v1, "BigQueryWriteClient", lambda: FakeWrite())
    monkeypatch.setattr(bigquery, "Client", lambda **kw: FakeQuery())
    store = bq_mod.BigQueryStore.from_settings(s)
    assert store._seen.capacity == 5000
    with pytest.raises(ValueError):
        CollectorSettings(data_dir=Path("unused"), dedupe_capacity=1)


def test_deploy_files_size_the_service_and_hold_no_secret():
    d = ROOT / "deploy" / "cloudrun"
    sh = (d / "deploy.sh").read_text(encoding="utf-8")
    ps = (d / "deploy.ps1").read_bytes().decode("ascii")
    # scale to zero, one instance (one live view, the cost cap), 40 requests at a time
    # (the worker threads), 1 GiB: 40 x 1 MiB bodies next to the dedupe set
    for var, default in (("MIN_INSTANCES", "0"), ("MAX_INSTANCES", "1"), ("CONCURRENCY", "40"),
                         ("MEMORY", "1Gi"), ("DEDUPE_CAPACITY", "1000000")):  # fmt: skip
        assert f'{var}="${{{var}:-{default}}}"' in sh
        assert f"if ($env:{var}) {{ $env:{var} }} else {{ '{default}' }}" in ps
    for flag in ("--min-instances", "--max-instances", "--concurrency", "--memory"):
        assert f"{flag}=" in sh and f"{flag}=" in ps
    for text in (sh, ps):
        assert "--cpu-throttling" in text and "PULSE_DEDUPE_CAPACITY=" in text
        assert "pulse-write-keys:latest" in text and "sp_pub_" not in text
        assert "contract/generated/bigquery" in text
    # a second run keeps what exists: every create is behind an existence check
    assert len(re.findall(r"^missing ", sh, re.M)) == 7 and ps.count("if (Missing ") == 7
    assert "--apply" in sh and "-Apply" in ps  # print-only unless asked
    docker = (d / "Dockerfile").read_text(encoding="utf-8")
    assert "PULSE_COLLECTOR_MODE=bigquery" in docker and "USER pulse" in docker
    readme = (d / "README.md").read_text(encoding="utf-8")
    assert "--max-instances=1" in readme and "APPS=" in readme and "DAU" in readme
    if shutil.which("sh"):
        for f in ("deploy.sh", "bootstrap.sh"):
            assert subprocess.run(["sh", "-n", str(d / f)], check=False).returncode == 0
