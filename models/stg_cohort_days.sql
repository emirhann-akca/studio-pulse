-- Install cohorts: users of cohort `cohort_date` active on day `day_n` after
-- install, plus their sessions, GA4-attributed revenue and first-time payers.
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
FROM raw_ga4_cohort_days
WHERE day_n >= 0
QUALIFY row_number() OVER (
    PARTITION BY app_id, platform, cohort_date, day_n ORDER BY _loaded_at DESC, _source
) = 1
