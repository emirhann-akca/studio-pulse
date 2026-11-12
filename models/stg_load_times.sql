-- Load-time samples (pulse_load events) from JSON lines / GA4 and from the SDK
-- collector; drops impossible values (> 10 min).
WITH src AS (
    SELECT app_id, date, platform, app_version, device_tier, kind, scene, duration_ms
    FROM raw_load_times
    UNION ALL
    SELECT app_id, date, platform, app_version,
           COALESCE(params->>'tier', device_tier) AS device_tier,
           params->>'kind' AS kind,
           params->>'scene' AS scene,
           CAST(params->>'duration_ms' AS DOUBLE) AS duration_ms
    FROM stg_pulse_events
    WHERE is_prod AND name = 'pulse_load'
)
SELECT
    app_id,
    date,
    COALESCE(platform, 'UNKNOWN') AS platform,
    app_version,
    COALESCE(NULLIF(lower(device_tier), ''), 'unknown') AS device_tier,
    kind,
    COALESCE(scene, '') AS scene,
    duration_ms
FROM src
WHERE duration_ms > 0 AND duration_ms < 600000
