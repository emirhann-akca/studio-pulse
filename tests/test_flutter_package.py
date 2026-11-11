"""The Flutter package and the Python side agree on the event contract.

flutter/studio_pulse_perf/test/golden/pulse_sample.jsonl is written by the
package's own `flutter test` (pulse_perf_test.dart compares its output with
it), so the connector here parses real Dart output, as tests/test_event_schema.py
does for the Unity package's C#.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import GA4Builder

from studio_pulse import events as E
from studio_pulse.connectors import ga4, unity_jsonl
from studio_pulse.connectors.dialect import DuckDBDialect
from studio_pulse.connectors.executors import DuckDBExecutor

PKG = Path(__file__).parents[1] / "flutter" / "studio_pulse_perf"


def dart_constants() -> dict[str, object]:
    src = (PKG / "lib" / "src" / "schema.dart").read_text(encoding="utf-8")
    out: dict[str, object] = {}
    for typ, name, value in re.findall(r"static const (String|int|double) (\w+) = ([^;]+);", src):
        key = re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
        v = value.strip().strip("'")
        out[key] = v if typ == "String" else (int(v) if typ == "int" else float(v))
    return out


def test_dart_constants_match_python():
    dart = dart_constants()
    assert len(dart) >= 20
    renamed = {"SOURCE_RSS": "SOURCE_RSS", "SOURCE_FLUTTER": "SOURCE_FLUTTER"}
    for key, value in dart.items():
        py = renamed.get(key, key)
        assert hasattr(E, py), f"{key} missing in studio_pulse/events.py"
        assert getattr(E, py) == value, key
    # every parameter the package sends is part of the contract
    params = {v for k, v in dart.items() if k.startswith("PARAM_")}
    assert params <= set(E.PERF_PARAMS) | set(E.LOAD_PARAMS)


def test_golden_output_parses_in_the_connector():
    lines = (
        (PKG / "test" / "golden" / "pulse_sample.jsonl").read_text(encoding="utf-8").splitlines()
    )
    perf, load, st = unity_jsonl.parse_lines(lines, app="com.example.flutter")
    assert (st.perf, st.load, st.malformed, st.unknown_event) == (1, 1, 0, 0)
    p = perf[0]
    assert p["ft_src"] == E.SOURCE_FLUTTER and p["mem_src"] == E.SOURCE_RSS
    assert (p["frames"], p["slow_frames"], p["janky_frames"]) == (1800, 6, 1086)
    assert (p["ft_p50_ms"], p["ft_p95_ms"], p["ft_max_ms"]) == (17.0, 17.8, 40.0)
    assert (p["cpu_main_p95_ms"], p["gpu_p95_ms"], p["sys_used_mb"]) == (7.12, 8.9, 600)
    assert p["device_tier"] == "low" and p["platform"] == "ANDROID"
    assert load[0]["kind"] == E.LOAD_KIND_COLD_START and load[0]["duration_ms"] == 2345.678


def test_firebase_route_maps_to_the_perf_model_without_code_changes(con, tmp_path):
    """The same params sent through FirebaseAnalyticsSink land in the GA4 export;
    the GA4 connector reads them into raw_perf_windows and the models as is."""
    import json

    line = (
        (PKG / "test" / "golden" / "pulse_sample.jsonl").read_text(encoding="utf-8").splitlines()[1]
    )
    params = json.loads(line)["params"]
    b = GA4Builder.__new__(GA4Builder)
    GA4Builder.__init__(b)
    b.PARAMS = {
        **GA4Builder.PARAMS,
        **{
            k: ("string" if isinstance(v, str) else "int" if isinstance(v, int) else "double")
            for k, v in params.items()
        },
    }
    b.add(0, E.PERF_EVENT, "u1", platform="ANDROID", app="com.example.flutter", **params)
    ex = DuckDBExecutor({"ga4_events": b.write(tmp_path / "e.parquet")})
    from conftest import D0, run_models

    ga4.ingest(
        con,
        app_id="f",
        source="ga4_files",
        dialect=DuckDBDialect(),
        executor=ex,
        start=D0,
        end=D0,
        opts=ga4.GA4Options(queries=["perf"]),
        log=lambda *_: None,
    )
    run_models(con)
    row = con.execute(
        "SELECT ft_src, frames, janky_frames, ft_p95_ms, gpu_p95_ms FROM stg_perf_windows"
    ).fetchone()
    assert row == ("flutter_ft", 1800, 1086, 17.8, 8.9)
    share = con.execute(
        "SELECT janky_frame_share FROM fct_perf_daily WHERE device_tier = 'all'"
    ).fetchone()[0]
    assert share == pytest.approx(1086 / 1800)


def test_events_doc_describes_the_flutter_package():
    doc = (Path(__file__).parents[1] / "docs" / "EVENTS.md").read_text(encoding="utf-8")
    for needle in (
        "`janky_frames`",
        "`flutter_ft`",
        "`dart_rss`",
        "addTimingsCallback",
        "flutter/studio_pulse_perf",
    ):
        assert needle in doc, needle


def test_per_frame_dart_code_has_no_allocation_patterns():
    src = (PKG / "lib" / "src" / "pulse_perf.dart").read_text(encoding="utf-8")
    start = src.index("{", src.index("void onTimings("))
    body = src[start : src.index("Future<void> flush(")]
    for bad in ("new ", "[]", "{}", "Duration", "toString", "$", ".map(", "List<", "DateTime"):
        assert bad not in body, bad
