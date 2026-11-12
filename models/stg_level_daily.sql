-- Level funnel events per day: GA4 (raw_level_daily) plus SDK level_start /
-- level_complete / level_fail of production builds (level_quit has its own
-- column in fct_sdk_level_funnel).
SELECT app_id, date, level, action, events, users, _loaded_at
FROM raw_level_daily
UNION ALL
SELECT
    app_id,
    date,
    CAST(params->>'level' AS INTEGER) AS level,
    CASE name WHEN 'level_start' THEN 'start' WHEN 'level_complete' THEN 'complete' ELSE 'fail' END AS action,
    COUNT(*) AS events,
    COUNT(DISTINCT install_id) AS users,
    CAST(NULL AS TIMESTAMP) AS _loaded_at
FROM stg_pulse_events
WHERE is_prod AND name IN ('level_start', 'level_complete', 'level_fail')
  AND params->>'level' IS NOT NULL
GROUP BY ALL
