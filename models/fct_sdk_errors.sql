-- SDK errors by fingerprint (pulse_error fp): how often, for how many installs.
SELECT
    app_id,
    params->>'fp' AS fingerprint,
    arg_max(params->>'type', ts) AS type,
    arg_max(params->>'frame', ts) AS frame,
    COUNT(*) AS events,
    COUNT(DISTINCT install_id) AS installs,
    COUNT(DISTINCT sid) AS sessions,
    COUNT(*) FILTER (WHERE CAST(params->>'fatal' AS BOOLEAN)) AS fatal_events,
    MIN(date) AS first_date,
    MAX(date) AS last_date,
    arg_max(app_version, ts) AS last_version
FROM stg_pulse_events
WHERE is_prod AND name = 'pulse_error'
GROUP BY 1, 2
ORDER BY 1, events DESC, 2
