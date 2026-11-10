-- Frame time and slow-frame share per app version and device tier: the view
-- used to spot a performance regression introduced by a release.
SELECT
    app_id,
    app_version,
    device_tier,
    MIN(date) AS first_seen,
    COUNT(*) AS windows,
    quantile_cont(CAST(ft_p50_ms AS DOUBLE), 0.5) AS ft_p50_ms,
    quantile_cont(CAST(ft_p95_ms AS DOUBLE), 0.5) AS ft_p95_ms,
    CAST(SUM(slow_frames) AS DOUBLE) / NULLIF(SUM(frames), 0) AS slow_frame_share
FROM stg_perf_windows
WHERE app_version IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 4, 3
