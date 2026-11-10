-- Load times per app, kind (cold_start / scene_load) and scene over the last
-- 28 days of data: sample count, p50 and p95.
WITH last_day AS (
    SELECT app_id, MAX(date) AS last_date FROM stg_load_times GROUP BY 1
)
SELECT
    l.app_id,
    l.kind,
    l.scene,
    COUNT(*) AS samples,
    quantile_cont(CAST(l.duration_ms AS DOUBLE), 0.5) AS p50_ms,
    quantile_cont(CAST(l.duration_ms AS DOUBLE), 0.95) AS p95_ms
FROM stg_load_times l JOIN last_day d USING (app_id)
WHERE l.date > d.last_date - 28
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
