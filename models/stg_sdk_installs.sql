-- One row per SDK install (production builds). cohort_date is the day of its
-- pulse_install event; installs that started before the loaded window have no
-- pulse_install in it and stay out of cohorts and new users (they would look
-- like new players on the window's first day). churned: no event in the last
-- 7 days before the app's newest day. max_level_started is the level a player
-- reached; for churned players it is the level they quit at.
WITH ev AS (
    SELECT * FROM stg_pulse_events WHERE is_prod
),
app_last AS (
    SELECT app_id, MAX(date) AS app_last_date FROM ev GROUP BY 1
),
i AS (
    SELECT
        app_id,
        install_id,
        arg_min(platform, ts) AS platform,
        MIN(date) FILTER (WHERE name = 'pulse_install') AS cohort_date,
        MIN(date) AS first_date,
        MAX(date) AS last_date,
        MIN(ts) AS first_seen_at,
        arg_min(install_store, ts) AS install_store,
        arg_min(country, ts) AS country,
        arg_min(device_tier, ts) AS device_tier,
        arg_min(app_version, ts) AS first_version,
        COUNT(DISTINCT date) AS active_days,
        COUNT(DISTINCT sid) AS sessions,
        MAX(CAST(params->>'level' AS INTEGER)) FILTER (WHERE name = 'level_start') AS max_level_started,
        MAX(CAST(params->>'level' AS INTEGER)) FILTER (WHERE name = 'level_complete') AS max_level_completed
    FROM ev
    GROUP BY app_id, install_id
)
SELECT
    i.*,
    a.app_last_date,
    i.last_date < a.app_last_date - 7 AS churned
FROM i
JOIN app_last a USING (app_id)
