-- Daily active-user aggregates per app, platform and date: the GA4 export plus
-- SDK apps (stg_sdk_active_users, the same columns). If an app was loaded by
-- more than one source, a GA4 row wins over the SDK (during a migration both
-- report; checks compare them), then the most recent load.
WITH src AS (
    SELECT app_id, platform, date, dau, wau, mau, new_users, sessions, engagement_sec,
           crashed_users, crashed_sessions, iap_revenue_usd, ad_revenue_usd, payers, history_days,
           _source, _loaded_at
    FROM raw_ga4_active_users
    UNION ALL
    SELECT app_id, platform, date, dau, wau, mau, new_users, sessions, engagement_sec,
           crashed_users, crashed_sessions, iap_revenue_usd, ad_revenue_usd, payers, history_days,
           _source, _loaded_at
    FROM stg_sdk_active_users
)
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
FROM src
QUALIFY row_number() OVER (
    PARTITION BY app_id, platform, date ORDER BY _source = 'pulse_sdk', _loaded_at DESC, _source
) = 1
