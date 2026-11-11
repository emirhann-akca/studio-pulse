"""Shared client event schema (Unity and Flutter packages <-> Python connectors).

This module is the single Python source of truth for the event and parameter
names documented in docs/EVENTS.md. The Unity package declares the same names
in unity/com.studio.pulse/Runtime/PulseSchema.cs and the Flutter package in
flutter/studio_pulse_perf/lib/src/schema.dart; tests/test_event_schema.py and
tests/test_flutter_package.py fail if they drift apart.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

# Event names (GA4 limit: 40 chars, [a-zA-Z0-9_]).
PERF_EVENT = "pulse_perf"
LOAD_EVENT = "pulse_load"

# Parameter names (GA4 limit: 40 chars; max 25 params per event).
PARAM_SCHEMA_VERSION = "pulse_v"
PARAM_TIER = "tier"
PARAM_SCENE = "scene"
PARAM_WINDOW_S = "window_s"
PARAM_FRAMES = "frames"
PARAM_FT_SOURCE = "ft_src"
PARAM_FT_P50 = "ft_p50_ms"
PARAM_FT_P95 = "ft_p95_ms"
PARAM_FT_MAX = "ft_max_ms"
PARAM_SLOW_FRAMES = "slow_frames"
PARAM_JANKY_FRAMES = "janky_frames"  # optional: frames over the display's frame budget (Flutter)
PARAM_CPU_MAIN_P95 = "cpu_main_p95_ms"
PARAM_GPU_P95 = "gpu_p95_ms"
PARAM_MEM_SOURCE = "mem_src"
PARAM_MEM_ALLOC = "mem_alloc_mb"
PARAM_MEM_RESERVED = "mem_reserved_mb"
PARAM_GC_RESERVED = "gc_reserved_mb"
PARAM_SYS_USED = "sys_used_mb"
PARAM_SYS_MEM = "sys_mem_mb"
PARAM_KIND = "kind"
PARAM_DURATION = "duration_ms"

PERF_PARAMS: dict[str, str] = {
    PARAM_SCHEMA_VERSION: "int",
    PARAM_TIER: "string",
    PARAM_SCENE: "string",
    PARAM_WINDOW_S: "double",
    PARAM_FRAMES: "int",
    PARAM_FT_SOURCE: "string",
    PARAM_FT_P50: "double",
    PARAM_FT_P95: "double",
    PARAM_FT_MAX: "double",
    PARAM_SLOW_FRAMES: "int",
    PARAM_JANKY_FRAMES: "int",  # optional: Flutter package only
    PARAM_CPU_MAIN_P95: "double",  # optional: only when the counter reported
    PARAM_GPU_P95: "double",  # optional: off on OpenGL ES by default
    PARAM_MEM_SOURCE: "string",
    PARAM_MEM_ALLOC: "int",
    PARAM_MEM_RESERVED: "int",
    PARAM_GC_RESERVED: "int",
    PARAM_SYS_USED: "int",  # optional: System Used Memory counter
    PARAM_SYS_MEM: "int",
}

LOAD_PARAMS: dict[str, str] = {
    PARAM_SCHEMA_VERSION: "int",
    PARAM_TIER: "string",
    PARAM_KIND: "string",
    PARAM_SCENE: "string",
    PARAM_DURATION: "double",
}

# ft_src / mem_src values: which Unity API produced the numbers.
SOURCE_FRAME_TIMING = "ftm"  # FrameTimingManager "CPU Total Frame Time" via ProfilerRecorder
SOURCE_DELTA_TIME = "dt"  # Time.unscaledDeltaTime fallback
SOURCE_RECORDER = "recorder"  # ProfilerRecorder memory counters
SOURCE_PROFILER_API = "profiler_api"  # Profiler.GetTotal*Memory fallback
SOURCE_FLUTTER = "flutter_ft"  # Flutter: FrameTiming via SchedulerBinding.addTimingsCallback
SOURCE_RSS = "dart_rss"  # Flutter: dart:io ProcessInfo.currentRss (resident set size)

LOAD_KIND_COLD_START = "cold_start"
LOAD_KIND_SCENE = "scene_load"
LOAD_KINDS = (LOAD_KIND_COLD_START, LOAD_KIND_SCENE)

TIERS = ("low", "mid", "high")

# A frame is "slow" when it takes longer than two 60 Hz vsync intervals,
# i.e. the game dropped below 30 fps for that frame.
SLOW_FRAME_MS = 33.3

# JSON-lines envelope keys written by the Unity JsonLinesSink.
ENVELOPE_KEYS = ("v", "event", "ts", "app", "app_version", "platform", "session", "params")

# Level funnel events (game code logs these to GA4; not emitted by the Unity
# package itself). GA4's recommended level_end(success) is also understood.
LEVEL_START = "level_start"
LEVEL_COMPLETE = "level_complete"
LEVEL_FAIL = "level_fail"
LEVEL_END = "level_end"
PARAM_LEVEL = "level"


def normalize_platform(value: str | None) -> str:
    """Map Unity RuntimePlatform names / GA4 platform values to GA4 style."""
    if not value:
        return "UNKNOWN"
    v = value.strip().upper()
    return {
        "IPHONEPLAYER": "IOS",
        "IPHONE": "IOS",
        "ANDROIDPLAYER": "ANDROID",
    }.get(v, v)
