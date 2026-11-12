-- Players by the highest level they started, and how many of them churned
-- (no activity for churn_window_days): GA4 (raw_level_max) plus SDK installs.
SELECT app_id, level, users_max_here, users_churned, as_of_date, churn_window_days, _loaded_at
FROM raw_level_max
UNION ALL
SELECT
    app_id,
    max_level_started AS level,
    COUNT(*) AS users_max_here,
    COUNT(*) FILTER (WHERE churned) AS users_churned,
    MAX(app_last_date) AS as_of_date,
    7 AS churn_window_days,
    CAST(NULL AS TIMESTAMP) AS _loaded_at
FROM stg_sdk_installs
WHERE max_level_started IS NOT NULL
GROUP BY app_id, max_level_started
