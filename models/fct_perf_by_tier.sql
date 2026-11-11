-- Client performance per app and device tier over the last 28 days of perf
-- data (same formulas as fct_perf_daily, computed over all windows at once).
--   cpu_main_p95_ms / gpu_p95_ms   median over windows of the window's p95 CPU
--                                  main-thread / GPU time (FrameTimingManager);
--                                  next to ft_p95_ms they show which side is
--                                  the bottleneck
--   ftm_share                      share of windows whose frame times came from
--                                  FrameTimingManager rather than deltaTime
--   janky_frame_share              frames over the display's frame budget /
--                                  frames, over windows that report it (Flutter)
WITH last_day AS (
    SELECT app_id, MAX(date) AS last_date FROM stg_perf_windows GROUP BY 1
),
w AS (
    SELECT p.*
    FROM stg_perf_windows p JOIN last_day l USING (app_id)
    WHERE p.date > l.last_date - 28
),
t AS (
    SELECT app_id, device_tier, frames, ft_p50_ms, ft_p95_ms, slow_frames, janky_frames,
           cpu_main_p95_ms, gpu_p95_ms, ft_src, mem_alloc_mb, mem_reserved_mb, sys_used_mb FROM w
    UNION ALL
    SELECT app_id, 'all', frames, ft_p50_ms, ft_p95_ms, slow_frames, janky_frames,
           cpu_main_p95_ms, gpu_p95_ms, ft_src, mem_alloc_mb, mem_reserved_mb, sys_used_mb FROM w
)
SELECT
    app_id,
    device_tier,
    COUNT(*) AS windows,
    SUM(frames) AS frames,
    quantile_cont(CAST(ft_p50_ms AS DOUBLE), 0.5) AS ft_p50_ms,
    quantile_cont(CAST(ft_p95_ms AS DOUBLE), 0.5) AS ft_p95_ms,
    CAST(SUM(slow_frames) AS DOUBLE) / NULLIF(SUM(frames), 0) AS slow_frame_share,
    CAST(SUM(janky_frames) AS DOUBLE)
        / NULLIF(SUM(CASE WHEN janky_frames IS NOT NULL THEN frames END), 0) AS janky_frame_share,
    quantile_cont(cpu_main_p95_ms, 0.5) AS cpu_main_p95_ms,
    quantile_cont(gpu_p95_ms, 0.5) AS gpu_p95_ms,
    AVG(CASE WHEN ft_src = 'ftm' THEN 1.0 WHEN ft_src IS NOT NULL THEN 0.0 END) AS ftm_share,
    quantile_cont(CAST(mem_alloc_mb AS DOUBLE), 0.95) AS mem_alloc_p95_mb,
    quantile_cont(CAST(mem_reserved_mb AS DOUBLE), 0.95) AS mem_reserved_p95_mb,
    quantile_cont(CAST(sys_used_mb AS DOUBLE), 0.95) AS sys_used_p95_mb
FROM t
GROUP BY 1, 2
ORDER BY 1, CASE device_tier WHEN 'low' THEN 1 WHEN 'mid' THEN 2 WHEN 'high' THEN 3 WHEN 'unknown' THEN 4 ELSE 5 END
