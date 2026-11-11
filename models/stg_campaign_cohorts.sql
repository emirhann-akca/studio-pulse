-- GA4 install cohorts by first-touch traffic source (one load per key).
SELECT
    app_id,
    COALESCE(platform, 'UNKNOWN') AS platform,
    cohort_date,
    day_n,
    lower(COALESCE(ts_source, '')) AS ts_source,
    lower(COALESCE(ts_medium, '')) AS ts_medium,
    COALESCE(ts_campaign, '') AS ts_campaign,
    COALESCE(users, 0) AS users,
    COALESCE(iap_revenue_usd, 0) AS iap_revenue_usd,
    COALESCE(ad_revenue_usd, 0) AS ad_revenue_usd
FROM raw_ga4_campaign_cohorts
WHERE day_n >= 0
QUALIFY row_number() OVER (
    PARTITION BY app_id, platform, cohort_date, day_n, ts_source, ts_medium, ts_campaign
    ORDER BY _loaded_at DESC, _source
) = 1
