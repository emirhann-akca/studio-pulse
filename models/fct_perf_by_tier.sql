-- Client performance per app and device tier over the last 28 days of perf
-- data (same formulas as fct_perf_daily, computed over all windows at once).
WITH last_day AS (
    SELECT app_id, MAX(date) AS last_date FROM stg_perf_windows GROUP BY 1
),
w AS (
    SELECT p.*
    FROM stg_perf_windows p JOIN last_day l USING (app_id)
    WHERE p.date > l.last_date - 28
),
t AS (
    SELECT app_id, device_tier, frames, ft_p50_ms, ft_p95_ms, slow_frames, mem_alloc_mb, mem_reserved_mb FROM w
    UNION ALL
    SELECT app_id, 'all', frames, ft_p50_ms, ft_p95_ms, slow_frames, mem_alloc_mb, mem_reserved_mb FROM w
)
SELECT
    app_id,
    device_tier,
    COUNT(*) AS windows,
    SUM(frames) AS frames,
    quantile_cont(CAST(ft_p50_ms AS DOUBLE), 0.5) AS ft_p50_ms,
    quantile_cont(CAST(ft_p95_ms AS DOUBLE), 0.5) AS ft_p95_ms,
    CAST(SUM(slow_frames) AS DOUBLE) / NULLIF(SUM(frames), 0) AS slow_frame_share,
    quantile_cont(CAST(mem_alloc_mb AS DOUBLE), 0.95) AS mem_alloc_p95_mb,
    quantile_cont(CAST(mem_reserved_mb AS DOUBLE), 0.95) AS mem_reserved_p95_mb
FROM t
GROUP BY 1, 2
ORDER BY 1, CASE device_tier WHEN 'low' THEN 1 WHEN 'mid' THEN 2 WHEN 'high' THEN 3 WHEN 'unknown' THEN 4 ELSE 5 END
