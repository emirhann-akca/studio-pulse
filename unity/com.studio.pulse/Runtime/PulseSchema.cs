// Shared event schema. Must match src/studio_pulse/events.py and docs/EVENTS.md
// (tests/test_event_schema.py compares the two). Pure C#: no UnityEngine use.
namespace Studio.Pulse
{
    public static class PulseSchema
    {
        public const int SchemaVersion = 1;

        public const string PerfEvent = "pulse_perf";
        public const string LoadEvent = "pulse_load";

        public const string ParamSchemaVersion = "pulse_v";
        public const string ParamTier = "tier";
        public const string ParamScene = "scene";
        public const string ParamWindowS = "window_s";
        public const string ParamFrames = "frames";
        public const string ParamFtSource = "ft_src";
        public const string ParamFtP50 = "ft_p50_ms";
        public const string ParamFtP95 = "ft_p95_ms";
        public const string ParamFtMax = "ft_max_ms";
        public const string ParamSlowFrames = "slow_frames";
        // Frames over the display's frame budget. Sent by the Flutter package
        // (flutter/studio_pulse); optional in the contract, not sent by Unity yet.
        public const string ParamJankyFrames = "janky_frames";
        public const string ParamCpuMainP95 = "cpu_main_p95_ms";
        public const string ParamGpuP95 = "gpu_p95_ms";
        public const string ParamMemSource = "mem_src";
        public const string ParamMemAlloc = "mem_alloc_mb";
        public const string ParamMemReserved = "mem_reserved_mb";
        public const string ParamGcReserved = "gc_reserved_mb";
        public const string ParamSysUsed = "sys_used_mb";
        public const string ParamSysMem = "sys_mem_mb";
        public const string ParamKind = "kind";
        public const string ParamDuration = "duration_ms";

        public const string LoadKindColdStart = "cold_start";
        public const string LoadKindScene = "scene_load";

        /// <summary>ft_src values: FrameTimingManager counter, or Time.unscaledDeltaTime.</summary>
        public const string SourceFrameTiming = "ftm";
        public const string SourceDeltaTime = "dt";
        /// <summary>mem_src values: ProfilerRecorder memory counters, or the Profiler.GetTotal* API.</summary>
        public const string SourceRecorder = "recorder";
        public const string SourceProfilerApi = "profiler_api";

        /// <summary>A frame slower than two 60 Hz vsync intervals (below 30 fps).</summary>
        public const float SlowFrameMs = 33.3f;
    }
}
