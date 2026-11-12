-- SDK apps as the collector's registry sees them (status new / testing / live,
-- store listing) next to what the warehouse holds for them.
WITH reg AS (
    SELECT *
    FROM raw_pulse_app_registry
    QUALIFY row_number() OVER (PARTITION BY app_id, bundle_id, platform ORDER BY _loaded_at DESC) = 1
),
ev AS (
    SELECT app_id, platform, COUNT(*) AS events, COUNT(DISTINCT install_id) AS installs,
           MAX(date) AS last_date
    FROM stg_pulse_events GROUP BY 1, 2
)
SELECT
    reg.app_id,
    reg.bundle_id,
    reg.platform,
    reg.display_name,
    reg.genre,
    reg.status,
    reg.first_seen,
    reg.last_seen,
    reg.first_version,
    reg.last_version,
    reg.status_changed_at,
    reg.store_listed,
    reg.store_version,
    reg.store_url,
    COALESCE(ev.events, 0) AS events,
    COALESCE(ev.installs, 0) AS installs,
    ev.last_date
FROM reg
LEFT JOIN ev ON ev.app_id = reg.app_id AND ev.platform = upper(reg.platform)
ORDER BY 1, 3
