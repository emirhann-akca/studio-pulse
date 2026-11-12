-- Studio Pulse SDK events (contract v2, docs/CONTRACT.md), one row per event.
-- The collector already drops repeats; this keeps one row per event id and per
-- (install_id, seq) anyway (a crash between its write and its dedupe index, an
-- overlapping reload). is_prod marks release builds that did not come from the
-- editor or a simulator: the rows every product metric below uses. Debug and
-- editor events stay visible in the data-quality model and on the Live page.
WITH by_id AS (
    SELECT *
    FROM raw_pulse_events
    QUALIFY row_number() OVER (
        PARTITION BY app_id, install_id, event_id ORDER BY received_at, _source
    ) = 1
)
SELECT
    app_id,
    bundle_id,
    upper(platform) AS platform,
    date,
    event_id,
    seq,
    name,
    ts,
    ts_client,
    received_at,
    skew_ms,
    late,
    sid,
    snum,
    fg_ms,
    params,
    app_version,
    build_env,
    install_id,
    install_store,
    COALESCE(country, 'unknown') AS country,
    COALESCE(NULLIF(lower(device_tier), ''), 'unknown') AS device_tier,
    schema,
    COALESCE(build_env, 'release') = 'release'
        AND COALESCE(install_store, 'unknown') NOT IN ('editor', 'simulator') AS is_prod
FROM by_id
QUALIFY row_number() OVER (
    PARTITION BY app_id, install_id, seq ORDER BY received_at, event_id
) = 1
