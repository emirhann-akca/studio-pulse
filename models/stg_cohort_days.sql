-- Install cohorts: users of cohort `cohort_date` active on day `day_n` after
-- install, plus their sessions, attributed revenue and first-time payers. GA4
-- export plus SDK apps (stg_sdk_cohort_days); a GA4 row wins over the SDK.
WITH src AS (
    SELECT app_id, platform, cohort_date, day_n, users, sessions, iap_revenue_usd,
           ad_revenue_usd, new_payers, _source, _loaded_at
    FROM raw_ga4_cohort_days
    UNION ALL
    SELECT app_id, platform, cohort_date, day_n, users, sessions, iap_revenue_usd,
           ad_revenue_usd, new_payers, _source, _loaded_at
    FROM stg_sdk_cohort_days
)
SELECT
    app_id,
    COALESCE(platform, 'UNKNOWN') AS platform,
    cohort_date,
    day_n,
    COALESCE(users, 0) AS users,
    COALESCE(sessions, 0) AS sessions,
    COALESCE(iap_revenue_usd, 0) AS iap_revenue_usd,
    COALESCE(ad_revenue_usd, 0) AS ad_revenue_usd,
    COALESCE(new_payers, 0) AS new_payers
FROM src
WHERE day_n >= 0
QUALIFY row_number() OVER (
    PARTITION BY app_id, platform, cohort_date, day_n
    ORDER BY _source = 'pulse_sdk', _loaded_at DESC, _source
) = 1
