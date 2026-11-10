"""The Unity package and the Python connectors must agree on the event schema.

tests/fixtures/unity/csharp_sample.jsonl and csharp_frames.txt were produced
by running the UnityEngine-free core of the Unity package (PulseJson,
FrameWindow, DeviceTierRules) under Mono, so these tests check the real C#
output, not a Python imitation of it.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest
from conftest import FIXTURES

from studio_pulse import events as E
from studio_pulse.connectors import unity_jsonl

ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "unity" / "com.studio.pulse" / "Runtime"


def csharp_constants() -> dict[str, object]:
    src = (RUNTIME / "PulseSchema.cs").read_text()
    out: dict[str, object] = {}
    for typ, name, value in re.findall(r"public const (string|int|float) (\w+) = ([^;]+);", src):
        key = re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
        out[key] = value.strip('"') if typ == "string" else (int(value) if typ == "int" else float(value.rstrip("f")))
    return out


def test_csharp_constants_match_python_schema():
    cs = csharp_constants()
    assert len(cs) >= 20
    for key, value in cs.items():
        assert hasattr(E, key), f"{key} missing in studio_pulse/events.py"
        assert getattr(E, key) == value, key
    py_params = {k for k in dir(E) if k.startswith("PARAM_") and k != "PARAM_LEVEL"}
    assert py_params == {k for k in cs if k.startswith("PARAM_")}


def test_param_names_respect_ga4_limits():
    for name in list(E.PERF_PARAMS) + list(E.LOAD_PARAMS) + [E.PERF_EVENT, E.LOAD_EVENT]:
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name), name
    assert len(E.PERF_PARAMS) <= 25 and len(E.LOAD_PARAMS) <= 25


def test_events_doc_lists_every_param():
    doc = (ROOT / "docs" / "EVENTS.md").read_text()
    for name in set(E.PERF_PARAMS) | set(E.LOAD_PARAMS) | {E.PERF_EVENT, E.LOAD_EVENT, *E.LOAD_KINDS}:
        assert f"`{name}`" in doc, name


def test_connector_parses_csharp_output():
    lines = (FIXTURES / "unity" / "csharp_sample.jsonl").read_text().splitlines()
    perf, load, st = unity_jsonl.parse_lines(lines, app="com.example.game")
    assert (st.perf, st.load, st.malformed, st.unknown_event) == (1, 1, 0, 0)
    p = perf[0]
    assert p["device_tier"] == "low" and p["scene"] == 'Level "7"' and p["platform"] == "ANDROID"
    assert p["frames"] == 1800 and p["window_s"] == 30.0
    assert (p["ft_src"], p["mem_src"]) == (E.SOURCE_FRAME_TIMING, E.SOURCE_RECORDER)
    assert (p["gc_reserved_mb"], p["sys_used_mb"]) == (96, 688)
    # The harness fed the CPU / GPU rings 0.55x / 0.8x of each frame; percentiles scale with it.
    assert p["cpu_main_p95_ms"] == pytest.approx(0.55 * p["ft_p95_ms"], abs=2e-3)
    assert p["gpu_p95_ms"] == pytest.approx(0.8 * p["ft_p95_ms"], abs=2e-3)
    assert load[0]["kind"] == "cold_start" and load[0]["duration_ms"] == 3912.25


def test_csharp_percentiles_match_duckdb_quantile_cont():
    frames = [float(x) for x in (FIXTURES / "unity" / "csharp_frames.txt").read_text().split()]
    perf, _, _ = unity_jsonl.parse_lines((FIXTURES / "unity" / "csharp_sample.jsonl").read_text().splitlines())
    p = perf[0]
    con = duckdb.connect()
    p50, p95, mx = con.execute(
        "SELECT quantile_cont(x, 0.5), quantile_cont(x, 0.95), max(x) FROM (SELECT unnest(?) AS x)", [frames]
    ).fetchone()
    assert p["ft_p50_ms"] == pytest.approx(p50, abs=2e-3)
    assert p["ft_p95_ms"] == pytest.approx(p95, abs=2e-3)
    assert p["ft_max_ms"] == pytest.approx(mx, abs=2e-3)
    assert p["slow_frames"] == sum(1 for f in frames if f > E.SLOW_FRAME_MS)


def test_firebase_sink_is_behind_define_and_asmdef_sets_it():
    sink = (RUNTIME / "FirebaseAnalyticsSink.cs").read_text()
    assert sink.lstrip().startswith("#if PULSE_FIREBASE") and sink.rstrip().endswith("#endif")
    asmdef = (RUNTIME / "Studio.Pulse.asmdef").read_text()
    assert '"define": "PULSE_FIREBASE"' in asmdef and '"name": "Studio.Pulse"' in asmdef


def test_no_per_frame_allocation_patterns_in_update_path():
    """Cheap static guard: the per-frame methods must not allocate."""
    src = (RUNTIME / "FrameWindow.cs").read_text()
    add = src[src.index("public void Add("): src.index("public void Snapshot(")]
    assert "new " not in add and "ToString" not in add
    sampler = (RUNTIME / "PerfSampler.cs").read_text()
    tick = sampler[sampler.index("public PulseEvent Tick("): sampler.index("void SampleMemory()")]
    assert "new " not in tick and "ToString" not in tick and "GetActiveScene" not in tick
    memory = sampler[sampler.index("void SampleMemory()"): sampler.index("public PulseEvent CloseWindow(")]
    assert "new " not in memory and "ToString" not in memory


def test_sampler_uses_documented_counters_and_disposes_them():
    """Counter names from the Unity 6.3 manual; every recorder is released."""
    src = (RUNTIME / "PerfSampler.cs").read_text()
    started = re.findall(r'ProfilerRecorder\.StartNew\(ProfilerCategory\.(\w+), "([^"]+)"\)', src)
    assert sorted(started) == sorted([
        ("Internal", "CPU Total Frame Time"), ("Internal", "CPU Main Thread Frame Time"), ("Internal", "GPU Frame Time"),
        ("Memory", "Total Reserved Memory"), ("Memory", "GC Reserved Memory"), ("Memory", "System Used Memory"),
    ])
    fields = re.findall(r"^\s*ProfilerRecorder (_\w+);", src, flags=re.M)
    assert len(fields) == 6 and all(f"{f}.Dispose()" in src for f in fields)
    assert "_sampler.Dispose()" in (RUNTIME / "Pulse.cs").read_text()
