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
        public const string ParamFtP50 = "ft_p50_ms";
        public const string ParamFtP95 = "ft_p95_ms";
        public const string ParamFtMax = "ft_max_ms";
        public const string ParamSlowFrames = "slow_frames";
        public const string ParamMemAlloc = "mem_alloc_mb";
        public const string ParamMemReserved = "mem_reserved_mb";
        public const string ParamSysMem = "sys_mem_mb";
        public const string ParamKind = "kind";
        public const string ParamDuration = "duration_ms";

        public const string LoadKindColdStart = "cold_start";
        public const string LoadKindScene = "scene_load";

        /// <summary>A frame slower than two 60 Hz vsync intervals (below 30 fps).</summary>
        public const float SlowFrameMs = 33.3f;
    }
}
