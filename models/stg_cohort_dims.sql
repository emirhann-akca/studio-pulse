-- Dimensional install cohorts from the cohort_dims pull (brain B3): one row per
-- cohort day and GROUPING SETS cell. `grouping_set` names the dimensions of the
-- cell ('' = all users); retained users are NULL until day N is observed.
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
FROM raw_ga4_cohort_dims
QUALIFY row_number() OVER (
    PARTITION BY app_id, cohort_date, platform, country, app_version, network, device_tier
    ORDER BY _loaded_at DESC, _source
) = 1
