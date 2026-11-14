"""Event contract v2: one YAML source, generated outputs that never drift, one rule set.

contract/pulse_v2.yaml generates Python, C#, Dart, a JSON Schema, BigQuery
schemas and docs/CONTRACT.md. These tests regenerate everything in memory and
compare with the committed files (CI also regenerates and runs git diff), check
the YAML against GA4's naming rules and Firebase's reserved names, and run the
collector's validator and the JSON Schema over the same golden samples.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from studio_pulse import events as E
from studio_pulse.contract import generate as G
from studio_pulse.contract import generated as C
from studio_pulse.contract import spec
from studio_pulse.contract.validate import EnvelopeError, check_envelope, check_event

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "contract" / "golden"


def test_generated_outputs_are_up_to_date():
    assert G.stale(ROOT) == [], "run: python -m studio_pulse.contract.generate"


def test_check_mode_reports_a_stale_output(tmp_path):
    for rel in [str(G.SOURCE), *G.OUTPUTS]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    assert G.main(["--check", "--root", str(tmp_path)]) == 0
    dart = tmp_path / "flutter/studio_pulse/lib/src/contract.g.dart"
    dart.write_text(dart.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8")
    assert G.main(["--check", "--root", str(tmp_path)]) == 1
    assert G.main(["--root", str(tmp_path)]) == 0
    assert G.main(["--check", "--root", str(tmp_path)]) == 0


def test_names_follow_ga4_rules_and_avoid_reserved_names():
    s = spec()
    pat = re.compile(s["names"]["pattern"])
    reserved = set(s["names"]["reserved_names"])
    for name, ev in s["events"].items():
        assert pat.fullmatch(name) and len(name) <= 40, name
        assert name not in reserved, name
        assert not name.startswith(tuple(s["names"]["reserved_prefixes"])), name
        assert (ev["category"] == "automatic") == name.startswith("pulse_"), name
        assert len(ev["params"]) <= s["limits"]["max_params"], name
        for p in ev["params"]:
            assert pat.fullmatch(p), (name, p)
    # The Firebase names the SDK must never emit (FirebaseAnalytics.Event reference).
    for name in ("first_open", "session_start", "app_exception", "error", "user_engagement"):
        assert name in reserved
    assert s["limits"]["batch_max_events"] == 500 and s["limits"]["batch_max_bytes"] == 1 << 20
    assert s["limits"]["event_max_bytes"] == 8192


def test_task_events_are_in_the_catalog():
    ev = spec()["events"]
    for name in ("level_start", "level_complete", "level_fail", "level_quit", "tutorial_begin",
                 "tutorial_step", "tutorial_complete", "setting_change", "earn_virtual_currency",
                 "spend_virtual_currency", "purchase", "ad_impression", "ad_event", "screen_view",
                 "pulse_install", "pulse_session_start", "pulse_error", "pulse_exit", "pulse_perf",
                 "pulse_load"):  # fmt: skip
        assert name in ev, name
    fail = ev["level_fail"]["params"]
    assert {"reason", "pos_x", "pos_y", "progress"} <= set(fail)
    assert set(ev["setting_change"]["params"]) == {"setting", "from", "to"}
    assert {"music", "sfx", "haptics", "language"} <= set(spec()["enums"]["setting"])
    assert ev["purchase"]["firebase"] is False


def test_v1_perf_and_load_params_are_unchanged():
    s = spec()["events"]
    v2 = {"int": "int", "double": "double", "string": "string"}
    assert {k: v2[f["type"]] for k, f in s["pulse_perf"]["params"].items()} == E.PERF_PARAMS
    assert {k: v2[f["type"]] for k, f in s["pulse_load"]["params"].items()} == E.LOAD_PARAMS
    assert C.EVENT_PULSE_PERF == E.PERF_EVENT and C.EVENT_PULSE_LOAD == E.LOAD_EVENT


def _cs_and_dart() -> tuple[str, str]:
    cs = (ROOT / "unity/com.studio.pulse/Runtime/Core/PulseContract.g.cs").read_text(
        encoding="utf-8"
    )
    dart = (ROOT / "flutter/studio_pulse/lib/src/contract.g.dart").read_text(encoding="utf-8")
    return cs, dart


def test_csharp_and_dart_carry_every_name():
    cs, dart = _cs_and_dart()
    cs_strings = set(re.findall(r'public const string \w+ = "([^"]*)";', cs))
    dart_strings = set(re.findall(r"static const String \w+ = '([^']*)';", dart))
    names = set(spec()["events"]) | set(G.all_params(spec())) | set(spec()["ctx"])
    for values in spec()["enums"].values():
        names |= set(values)
    assert names <= cs_strings and names <= dart_strings
    assert "using UnityEngine" not in cs
    assert "public const long LateAfterMs = 2592000000L;" in cs  # > int.MaxValue
    py = {v for k, v in vars(C).items() if k.startswith("EVENT_")}
    assert py == set(spec()["events"])


def test_dart_identifiers_avoid_reserved_words():
    _, dart = _cs_and_dart()
    for ident in re.findall(r"static const \w+(?:<\w+>)? (\w+) =", dart):
        assert ident not in G.DART_RESERVED, ident


def test_golden_batch_is_valid():
    body = json.loads((GOLDEN / "batch_ok.json").read_text(encoding="utf-8"))
    sent_at, schema, ctx, events = check_envelope(body)
    assert schema == "2.0" and ctx["platform"] == "android" and len(events) == 29
    for ev in events:
        out, reason = check_event(ev)
        assert reason is None, (ev["name"], reason)
    assert set(spec()["events"]) <= {e["name"] for e in events}  # every catalog event once


@pytest.mark.parametrize(
    "case",
    json.loads((GOLDEN / "events_bad.json").read_text(encoding="utf-8")),
    ids=lambda c: c["reason"],
)
def test_bad_events_are_quarantined_with_their_reason(case):
    out, reason = check_event(case["event"])
    assert out is None and reason == case["reason"]


def test_child_mode_strips_uid_and_free_text():
    ev = {
        "id": "0190f0e2-7c1a-7cc3-9b1e-000000000001",
        "seq": 1,
        "name": "tutorial_step",
        "ts": 1794499900000,
        "sid": "0190f0e0-0000-7000-8000-000000000001",
        "uid": "player-42",
        "p": {"step": 2, "step_name": "drag the gem"},
    }
    out, reason = check_event(copy.deepcopy(ev), child=True)
    assert reason is None and out["uid"] is None and out["p"] == {"step": 2}
    custom = dict(ev, name="booster_used", p={"booster": "hammer", "count": 2})
    out, _ = check_event(custom, child=True)
    assert out["p"] == {"count": 2}


def test_envelope_errors():
    body = json.loads((GOLDEN / "batch_ok.json").read_text(encoding="utf-8"))
    for mutate, reason, status in [
        (lambda b: b.update(v=1), "unsupported_version", 400),
        (lambda b: b.pop("ctx"), "missing:ctx", 400),
        (lambda b: b["ctx"].pop("install_id"), "ctx.missing:install_id", 400),
        (lambda b: b["ctx"].update(platform="nintendo"), "ctx.enum:platform", 400),
        (lambda b: b["ctx"].update(install_id="abc"), "ctx.uuid:install_id", 400),
        (lambda b: b["ctx"].update(app_name="ops@example.com"), "ctx.pii_email:app_name", 400),
        (lambda b: b.update(events=[]), "no_events", 400),
        (lambda b: b.update(events=b["events"] * 20), "too_many_events", 413),
    ]:
        b = copy.deepcopy(body)
        mutate(b)
        with pytest.raises(EnvelopeError) as exc:
            check_envelope(b)
        assert (exc.value.reason, exc.value.status) == (reason, status)


def test_json_schema_agrees_with_the_validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "contract/generated/pulse_v2.batch.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    v = jsonschema.Draft202012Validator(schema)
    body = json.loads((GOLDEN / "batch_ok.json").read_text(encoding="utf-8"))
    assert list(v.iter_errors(body)) == []
    for case in json.loads((GOLDEN / "events_bad.json").read_text(encoding="utf-8")):
        b = copy.deepcopy(body)
        b["events"] = [case["event"]]
        assert bool(list(v.iter_errors(b))) == case["schema_rejects"], case["reason"]


def test_bigquery_schemas_match_the_storage_spec():
    s = spec()["storage"]
    for table in ("events", "quarantine", "app_registry", "deletion_requests"):
        cols = json.loads(
            (ROOT / f"contract/generated/bigquery/{table}.json").read_text(encoding="utf-8")
        )
        assert [c["name"] for c in cols] == [c["name"] for c in s[table]["columns"]]
        assert {c["type"] for c in cols} <= {"STRING", "INT64", "TIMESTAMP", "BOOL"}
    names = [c["name"] for c in s["events"]["columns"]]
    assert "ip" not in " ".join(names).split() and "country" in names


def _yaml():
    return yaml.safe_load((ROOT / G.SOURCE).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda y: y["events"].update(session_start={"category": "app"}), "reserved"),
        (lambda y: y["events"].update(pulse_custom={"category": "game"}), "prefix"),
        (lambda y: y["events"].update(my_auto={"category": "automatic"}), "prefix"),
        (
            lambda y: y["events"].update(
                big={"category": "game", "params": {f"p{i}": {"type": "int"} for i in range(26)}}
            ),
            "more than 25",
        ),
        (
            lambda y: y["events"]["level_fail"]["params"].update(
                x={"type": "string", "enum": "nope"}
            ),
            "unknown enum",
        ),
        (lambda y: y["events"].update(BadName={"category": "game"}), "breaks"),
    ],
)
def test_generator_refuses_a_broken_contract(mutate, message):
    y = _yaml()
    mutate(y)
    with pytest.raises(G.ContractError, match=message):
        G.normalize(y)


def test_privacy_manifest_is_a_valid_plist_without_tracking():
    import plistlib
    import shutil
    import subprocess

    path = ROOT / "contract/generated/PrivacyInfo.xcprivacy"
    m = plistlib.loads(path.read_bytes())
    assert m["NSPrivacyTracking"] is False and m["NSPrivacyTrackingDomains"] == []
    types = {t["NSPrivacyCollectedDataType"] for t in m["NSPrivacyCollectedDataTypes"]}
    assert "NSPrivacyCollectedDataTypeDeviceID" in types
    assert "NSPrivacyCollectedDataTypeCoarseLocation" in types
    assert all(
        not t["NSPrivacyCollectedDataTypeTracking"] for t in m["NSPrivacyCollectedDataTypes"]
    )
    if shutil.which("plutil"):  # macOS: Apple's own linter
        assert subprocess.run(["plutil", "-lint", str(path)], capture_output=True).returncode == 0
