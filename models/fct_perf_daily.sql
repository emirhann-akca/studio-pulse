-- Client performance per app, day and device tier (plus an 'all' tier).
--   ft_p50_ms           median over windows of the per-window median frame time
--   ft_p95_ms           median over windows of the per-window p95 frame time
--   slow_frame_share    sum(slow_frames) / sum(frames)   (frames > 33.3 ms)
--   mem_alloc_p95_mb    p95 over windows of allocated managed+native memory
--   sys_used_p95_mb     p95 over windows of System Used Memory (the OS view)
WITH w AS (
    SELECT app_id, date, device_tier, frames, ft_p50_ms, ft_p95_ms, slow_frames, mem_alloc_mb, sys_used_mb
    FROM stg_perf_windows
    UNION ALL
    SELECT app_id, date, 'all', frames, ft_p50_ms, ft_p95_ms, slow_frames, mem_alloc_mb, sys_used_mb
    FROM stg_perf_windows
)
SELECT
    app_id,
    date,
    device_tier,
    COUNT(*) AS windows,
    SUM(frames) AS frames,
    quantile_cont(CAST(ft_p50_ms AS DOUBLE), 0.5) AS ft_p50_ms,
    quantile_cont(CAST(ft_p95_ms AS DOUBLE), 0.5) AS ft_p95_ms,
    CAST(SUM(slow_frames) AS DOUBLE) / NULLIF(SUM(frames), 0) AS slow_frame_share,
    quantile_cont(CAST(mem_alloc_mb AS DOUBLE), 0.95) AS mem_alloc_p95_mb,
    quantile_cont(CAST(sys_used_mb AS DOUBLE), 0.95) AS sys_used_p95_mb
FROM w
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
