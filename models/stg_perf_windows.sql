-- Client performance windows (pulse_perf events), sanity-filtered.
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
FROM raw_perf_windows
WHERE ft_p50_ms > 0 AND ft_p50_ms < 10000
  AND (frames IS NULL OR frames > 0)
