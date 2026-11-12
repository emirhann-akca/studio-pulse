-- Dimensional install cohorts (brain B3): one row per cohort day and GROUPING
-- SETS cell. `grouping_set` names the dimensions of the cell ('' = all users);
-- retained users are NULL until day N is observed.
--   GA4 apps: the cohort_dims pull (aggregated inside BigQuery).
--   SDK apps: the same cells from the studio SDK's production installs
--     (stg_sdk_installs): platform, country, first app version and device tier
--     as of the install; no network, since the SDK does not know the campaign.
--     Day N is classic (active on exactly day N), as in the pull, and a cell
--     needs 50 day-0 installs like the pull's default min_cell_users.
WITH ga4 AS (
    SELECT
        app_id, cohort_date, platform, country, app_version, network, device_tier,
        users, d1_users, d7_users, d30_users, observed_days
    FROM raw_ga4_cohort_dims
    QUALIFY row_number() OVER (
        PARTITION BY app_id, cohort_date, platform, country, app_version, network, device_tier
        ORDER BY _loaded_at DESC, _source
    ) = 1
),
sdk_inst AS (
    SELECT
        app_id,
        install_id,
        cohort_date,
        COALESCE(platform, 'unknown') AS platform,
        COALESCE(country, 'unknown') AS country,
        COALESCE(first_version, 'unknown') AS app_version,
        COALESCE(device_tier, 'unknown') AS device_tier,
        CAST(app_last_date - cohort_date AS INTEGER) AS observed_days
    FROM stg_sdk_installs
    WHERE cohort_date IS NOT NULL
),
sdk_act AS (
    SELECT DISTINCT app_id, install_id, date FROM stg_pulse_events WHERE is_prod
),
sdk_user AS (
    SELECT
        i.app_id, i.install_id, i.cohort_date, i.platform, i.country, i.app_version,
        i.device_tier, i.observed_days,
        MAX(CASE WHEN a.date = i.cohort_date + 1 THEN 1 ELSE 0 END) AS r1,
        MAX(CASE WHEN a.date = i.cohort_date + 7 THEN 1 ELSE 0 END) AS r7,
        MAX(CASE WHEN a.date = i.cohort_date + 30 THEN 1 ELSE 0 END) AS r30
    FROM sdk_inst i
    LEFT JOIN sdk_act a
        ON a.app_id = i.app_id AND a.install_id = i.install_id AND a.date > i.cohort_date
    GROUP BY ALL
),
sdk AS (
    SELECT
        app_id, cohort_date, platform, country, app_version,
        CAST(NULL AS VARCHAR) AS network,
        device_tier,
        COUNT(*) AS users,
        SUM(r1) AS d1_users,
        SUM(r7) AS d7_users,
        SUM(r30) AS d30_users,
        MIN(observed_days) AS observed_days
    FROM sdk_user
    GROUP BY GROUPING SETS (
        (app_id, cohort_date),
        (app_id, cohort_date, platform),
        (app_id, cohort_date, country),
        (app_id, cohort_date, app_version),
        (app_id, cohort_date, device_tier),
        (app_id, cohort_date, platform, country),
        (app_id, cohort_date, platform, app_version),
        (app_id, cohort_date, platform, device_tier),
        (app_id, cohort_date, country, app_version),
        (app_id, cohort_date, country, device_tier),
        (app_id, cohort_date, app_version, device_tier)
    )
    HAVING COUNT(*) >= 50
),
cells AS (
    SELECT * FROM ga4
    UNION ALL BY NAME
    SELECT * FROM sdk
)
SELECT
    app_id,
    cohort_date,
    platform,
    country,
    app_version,
    network,
    device_tier,
    concat_ws(',',
        CASE WHEN platform IS NOT NULL THEN 'platform' END,
        CASE WHEN country IS NOT NULL THEN 'country' END,
        CASE WHEN app_version IS NOT NULL THEN 'app_version' END,
        CASE WHEN network IS NOT NULL THEN 'network' END,
        CASE WHEN device_tier IS NOT NULL THEN 'device_tier' END
    ) AS grouping_set,
    users,
    CASE WHEN observed_days >= 1 THEN d1_users END AS d1_users,
    CASE WHEN observed_days >= 7 THEN d7_users END AS d7_users,
    CASE WHEN observed_days >= 30 THEN d30_users END AS d30_users
FROM cells
