-- Client performance windows (pulse_perf events), sanity-filtered: the Unity
-- and Flutter packages' JSON lines / GA4 route (raw_perf_windows) plus the same
-- event sent through the SDK collector (stg_pulse_events, production builds).
WITH src AS (
    SELECT app_id, date, platform, app_version, device_tier, scene, window_s, frames, ft_p50_ms,
           ft_p95_ms, ft_max_ms, slow_frames, janky_frames, ft_src, cpu_main_p95_ms, gpu_p95_ms,
           mem_src, mem_alloc_mb, mem_reserved_mb, gc_reserved_mb, sys_used_mb
    FROM raw_perf_windows
    UNION ALL
    SELECT app_id, date, platform, app_version,
           COALESCE(params->>'tier', device_tier) AS device_tier,
           params->>'scene' AS scene,
           CAST(params->>'window_s' AS DOUBLE) AS window_s,
           CAST(params->>'frames' AS BIGINT) AS frames,
           CAST(params->>'ft_p50_ms' AS DOUBLE) AS ft_p50_ms,
           CAST(params->>'ft_p95_ms' AS DOUBLE) AS ft_p95_ms,
           CAST(params->>'ft_max_ms' AS DOUBLE) AS ft_max_ms,
           CAST(params->>'slow_frames' AS BIGINT) AS slow_frames,
           CAST(params->>'janky_frames' AS BIGINT) AS janky_frames,
           params->>'ft_src' AS ft_src,
           CAST(params->>'cpu_main_p95_ms' AS DOUBLE) AS cpu_main_p95_ms,
           CAST(params->>'gpu_p95_ms' AS DOUBLE) AS gpu_p95_ms,
           params->>'mem_src' AS mem_src,
           CAST(params->>'mem_alloc_mb' AS DOUBLE) AS mem_alloc_mb,
           CAST(params->>'mem_reserved_mb' AS DOUBLE) AS mem_reserved_mb,
           CAST(params->>'gc_reserved_mb' AS DOUBLE) AS gc_reserved_mb,
           CAST(params->>'sys_used_mb' AS DOUBLE) AS sys_used_mb
    FROM stg_pulse_events
    WHERE is_prod AND name = 'pulse_perf'
)
SELECT
    app_id,
    date,
    COALESCE(platform, 'UNKNOWN') AS platform,
    app_version,
    COALESCE(NULLIF(lower(device_tier), ''), 'unknown') AS device_tier,
    COALESCE(scene, '') AS scene,
    window_s,
    frames,
    ft_p50_ms,
    ft_p95_ms,
    ft_max_ms,
    slow_frames,
    janky_frames,      -- frames over the display's frame budget (Flutter package only)
    ft_src,            -- 'ftm' (FrameTimingManager) | 'dt' (unscaledDeltaTime) | 'flutter_ft' (Flutter FrameTiming) | NULL
    cpu_main_p95_ms,
    gpu_p95_ms,
    mem_src,
    mem_alloc_mb,
    mem_reserved_mb,
    gc_reserved_mb,
    sys_used_mb
FROM src
WHERE ft_p50_ms > 0 AND ft_p50_ms < 10000
  AND (frames IS NULL OR frames > 0)
