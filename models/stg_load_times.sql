-- Load-time samples (pulse_load events); drops impossible values (> 10 min).
SELECT
    app_id,
    date,
    COALESCE(platform, 'UNKNOWN') AS platform,
    app_version,
    COALESCE(NULLIF(lower(device_tier), ''), 'unknown') AS device_tier,
    kind,
    COALESCE(scene, '') AS scene,
    duration_ms
FROM raw_load_times
WHERE duration_ms > 0 AND duration_ms < 600000
