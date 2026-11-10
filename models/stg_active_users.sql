-- Daily active-user aggregates per app, platform and date (from the GA4 export).
-- If an app was loaded by more than one source, the most recent load wins.
SELECT
    app_id,
    COALESCE(platform, 'UNKNOWN') AS platform,
    date,
    dau,
    wau,
    mau,
    new_users,
    sessions,
    engagement_sec,
    crashed_users,
    crashed_sessions,
    iap_revenue_usd,
    ad_revenue_usd,
    payers,
    history_days
FROM raw_ga4_active_users
QUALIFY row_number() OVER (
    PARTITION BY app_id, platform, date ORDER BY _loaded_at DESC, _source
) = 1
