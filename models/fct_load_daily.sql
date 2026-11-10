-- Daily load-time percentiles per app and kind (all scenes together).
SELECT
    app_id,
    date,
    kind,
    COUNT(*) AS samples,
    quantile_cont(CAST(duration_ms AS DOUBLE), 0.5) AS p50_ms,
    quantile_cont(CAST(duration_ms AS DOUBLE), 0.95) AS p95_ms
FROM stg_load_times
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
