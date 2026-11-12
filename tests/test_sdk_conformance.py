"""Cross-SDK conformance: the batches the Unity (C#) and Flutter (Dart) SDKs write.

contract/golden/sdk_script.json is replayed by both SDKs' own test suites with a
fake clock and a seeded SplitMix64; both must produce sdk_conformance.ndjson byte
for byte (C#: unity/dotnet ConformanceTests, Dart: test/conformance_test.dart).
Here the goldens (that file and the hand-written batch_ok.json) are checked
against the JSON Schema and the collector's validator, and posted to the real
collector app, which must store every event exactly once.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio_pulse.collector.app import create_app
from studio_pulse.collector.core import Collector
from studio_pulse.collector.settings import DEFAULT_LOCAL_KEY, CollectorSettings
from studio_pulse.collector.store import LocalStore
from studio_pulse.contract.validate import check_envelope, check_event

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "contract" / "golden"
SDK_LINES = (GOLDEN / "sdk_conformance.ndjson").read_text(encoding="utf-8").splitlines()


def sdk_batches() -> list[dict]:
    return [json.loads(line) for line in SDK_LINES if line]


def all_goldens() -> list[tuple[str, dict]]:
    out = [("batch_ok.json", json.loads((GOLDEN / "batch_ok.json").read_text(encoding="utf-8")))]
    out += [(f"sdk_conformance.ndjson:{i + 1}", b) for i, b in enumerate(sdk_batches())]
    return out


@pytest.mark.parametrize("name,body", all_goldens(), ids=[n for n, _ in all_goldens()])
def test_goldens_pass_the_json_schema_and_the_validator(name, body):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "contract/generated/pulse_v2.batch.schema.json").read_text(encoding="utf-8")
    )
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(body))
    assert errors == [], [e.message for e in errors]
    _, _, ctx, events = check_envelope(json.loads(json.dumps(body)))
    for ev in events:
        out, reason = check_event(ev, child=bool(ctx.get("child")))
        assert reason is None, (name, ev["name"], reason)


def test_sdk_golden_is_compact_canonical_json():
    """What the SDKs write is exactly Python's compact json.dumps of the same data:
    no spaces, UTF-8 (not \\u escapes), shortest round-trip numbers."""
    for line in SDK_LINES:
        assert line == json.dumps(json.loads(line), separators=(",", ":"), ensure_ascii=False)


def test_sdk_golden_covers_the_script():
    script = json.loads((GOLDEN / "sdk_script.json").read_text(encoding="utf-8"))
    events = [e for b in sdk_batches() for e in b["events"]]
    names = {e["name"] for e in events}
    assert {
        "pulse_install", "pulse_session_start", "pulse_consent", "pulse_heartbeat", "pulse_background",
        "pulse_foreground", "pulse_app_update", "pulse_exit", "pulse_error", "pulse_perf", "pulse_load",
        "level_start", "level_fail", "level_complete", "level_quit", "tutorial_begin", "tutorial_step",
        "tutorial_complete", "setting_change", "earn_virtual_currency", "spend_virtual_currency",
        "purchase", "ad_event", "ad_impression", "screen_view", "booster_used",
    } <= names  # fmt: skip
    assert {s["call"] for s in script["steps"]} >= {"start", "restart", "background", "error"}
    by_seq = {e["seq"]: e for e in events}
    assert sorted(by_seq) == list(range(1, 32))  # 31 events, the re-sent batch repeats 26-27
    fail = next(e for e in events if e["name"] == "level_fail")["p"]
    assert fail == {"level": 37, "attempt": 1, "duration_ms": 61000, "reason": "out_of_moves",
                    "progress": 0.8, "pos_x": 0.42, "pos_y": 0.77, "pos_space": "norm"}  # fmt: skip
    exit_ = next(e for e in events if e["name"] == "pulse_exit")
    first_sid = by_seq[1]["sid"]
    assert exit_["p"]["reason"] == "unclean" and exit_["p"]["prev_sid"] == first_sid
    assert exit_["sid"] != first_sid and exit_["snum"] == 2
    errors = [e for e in events if e["name"] == "pulse_error"]
    assert len(errors) == 1  # the second error has the same fingerprint in the same session
    assert "<path>" in errors[0]["p"]["msg"] and "4242" not in errors[0]["p"]["msg"]
    assert sdk_batches()[-1]["ctx"]["app_version"] == "1.4.1"
    ids = [b["ctx"]["install_id"] for b in sdk_batches()]
    assert len(set(ids)) == 1 and str(ids[0])[14] == "4"  # one UUIDv4 install id
    assert all(e["id"][14] == "7" for e in events)  # UUIDv7 event ids


@pytest.fixture()
def collector(tmp_path):
    t = {"ms": 0}
    settings = CollectorSettings(data_dir=tmp_path / "c", rate_per_ip=0, rate_per_install=0)
    store = LocalStore(settings.data_dir)
    c = Collector(settings, store, clock=lambda: t["ms"])
    client = TestClient(create_app(settings, c))
    yield client, t, settings.data_dir
    store.close()


def stored_rows(data_dir: Path) -> list[dict]:
    out = []
    for f in sorted((data_dir / "events").glob("dt=*/*.ndjson")):
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x]
    return out


def post(client, body: dict, gz: bool):
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"X-Pulse-Key": DEFAULT_LOCAL_KEY, "Content-Type": "application/json"}
    if gz:
        raw = gzip.compress(raw)
        headers["Content-Encoding"] = "gzip"
    return client.post("/v2/batch", content=raw, headers=headers)


def test_goldens_are_stored_once_by_the_real_collector(collector):
    client, t, data_dir = collector
    batch_ok = json.loads((GOLDEN / "batch_ok.json").read_text(encoding="utf-8"))
    t["ms"] = batch_ok["sent_at"] + 150
    r = post(client, batch_ok, gz=True)
    assert r.status_code == 200 and r.json()["accepted"] == len(batch_ok["events"]), r.text
    totals = {"accepted": 0, "duplicates": 0, "quarantined": 0}
    for i, line in enumerate(SDK_LINES):
        body = json.loads(line)
        t["ms"] = body["sent_at"] + 80  # 80 ms in transit
        r = post(client, body, gz=len(line) >= 1024)  # the SDKs gzip bodies of 1 KB or more
        assert r.status_code == 200, (i, r.text)
        for k in totals:
            totals[k] += r.json()[k]
    assert totals == {"accepted": 31, "duplicates": 2, "quarantined": 0}
    install = sdk_batches()[0]["ctx"]["install_id"]
    rows = [r for r in stored_rows(data_dir) if r["install_id"] == install]
    assert sorted(r["seq"] for r in rows) == list(range(1, 32))
    by_name = {r["name"]: r for r in rows}
    assert json.loads(by_name["setting_change"]["params"]) == {"setting": "music", "from": "on", "to": "off"}
    assert json.loads(by_name["purchase"]["params"])["price_micros"] == 49_990_000
    assert json.loads(by_name["ad_impression"]["params"])["value"] == 0.0123
    booster = json.loads(by_name["booster_used"]["params"])
    assert booster["note"] == 'tab\tquote" é 🎮' and booster["power"] == 1.5
    assert by_name["booster_used"]["uid"] == "player-1042"
    assert by_name["pulse_exit"]["app_version"] == "1.4.1"
    assert all(abs(r["skew_ms"] - 80) == 0 for r in rows)  # ts corrected by the transit time
    assert {r["install_store"] for r in rows} == {"google_play"}
    assert {r["sdk"] for r in rows} == {"conformance/2.0.0"}


def _load_number_generator():
    spec = importlib.util.spec_from_file_location("sdk_numbers", GOLDEN / "sdk_numbers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_number_table_is_the_shortest_round_trip_form():
    gen = _load_number_generator()
    table = json.loads((GOLDEN / "sdk_numbers.json").read_text(encoding="utf-8"))
    assert len(table) > 3000
    for bits, text in table:
        v = struct.unpack(">d", bytes.fromhex(bits))[0]
        assert gen.js(v) == text
        assert float(text) == v  # reads back to the same double
        assert float(json.loads(text)) == v  # integers above 2**53 parse as int
