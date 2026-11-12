-- When players turn a setting off, by the session number they did it in.
WITH off AS (
    SELECT app_id, params->>'setting' AS setting, install_id, arg_min(snum, ts) AS snum
    FROM stg_pulse_events
    WHERE is_prod AND name = 'setting_change'
      AND lower(params->>'to') IN ('off', '0', 'false', 'mute', 'muted')
    GROUP BY 1, 2, 3
)
SELECT
    app_id,
    setting,
    CASE WHEN snum IS NULL THEN 'unknown'
         WHEN snum = 1 THEN '1'
         WHEN snum = 2 THEN '2'
         WHEN snum <= 5 THEN '3-5'
         WHEN snum <= 10 THEN '6-10'
         ELSE '11+' END AS session_bucket,
    COUNT(*) AS players
FROM off
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
