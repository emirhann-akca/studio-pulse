-- One row per app and day: engagement, revenue, stability and store metrics.
-- Formulas are documented in docs/METRICS.md.
WITH au AS (
    SELECT app_id, date,
           SUM(dau) AS dau, SUM(wau) AS wau, SUM(mau) AS mau, MIN(history_days) AS history_days,
           SUM(new_users) AS new_users, SUM(sessions) AS sessions, SUM(engagement_sec) AS engagement_sec,
           SUM(crashed_users) AS ga4_crashed_users, SUM(crashed_sessions) AS ga4_crashed_sessions,
           SUM(iap_revenue_usd) AS ga4_iap_usd, SUM(ad_revenue_usd) AS ga4_ad_usd,
           SUM(payers) AS ga4_payers
    FROM stg_active_users
    GROUP BY 1, 2
),
rc AS (
    SELECT app_id, purchase_date AS date,
           SUM(revenue_usd) AS rc_revenue_usd,
           SUM(proceeds_usd) AS rc_proceeds_usd,
           COUNT(DISTINCT CASE WHEN revenue_usd > 0 THEN rc_original_app_user_id END) AS rc_payers,
           COUNT(*) FILTER (WHERE revenue_usd > 0) AS rc_paid_transactions,
           COUNT(*) FILTER (WHERE is_refunded) AS rc_refunds
    FROM stg_rc_transactions
    GROUP BY 1, 2
),
rc_apps AS (SELECT DISTINCT app_id FROM stg_rc_transactions),
admob_apps AS (SELECT DISTINCT app_id FROM stg_admob_daily),
cr AS (
    SELECT app_id, date, SUM(fatal_events) AS fatal_events, SUM(crashed_installs) AS crashed_installs
    FROM stg_crashes_daily GROUP BY 1, 2
),
cr_apps AS (SELECT DISTINCT app_id FROM stg_crashes_daily),
st AS (
    SELECT app_id, date,
           SUM(installs) AS store_installs, SUM(uninstalls) AS store_uninstalls,
           avg(total_avg_rating) AS rating
    FROM stg_store_daily GROUP BY 1, 2
),
days AS (
    SELECT app_id, date FROM au
    UNION SELECT app_id, date FROM rc
    UNION SELECT app_id, date FROM stg_admob_daily
    UNION SELECT app_id, date FROM cr
    UNION SELECT app_id, date FROM st
),
joined AS (
    SELECT
        d.app_id,
        d.date,
        au.dau,
        CASE WHEN au.history_days >= 7 THEN au.wau END AS wau,
        CASE WHEN au.history_days >= 30 THEN au.mau END AS mau,
        au.new_users,
        au.sessions,
        au.engagement_sec,
        CASE WHEN ra.app_id IS NOT NULL THEN COALESCE(rc.rc_proceeds_usd, 0) ELSE au.ga4_iap_usd END AS iap_revenue_usd,
        CASE WHEN ra.app_id IS NOT NULL THEN 'revenuecat_proceeds'
             WHEN au.ga4_iap_usd IS NOT NULL THEN 'ga4_gross' END AS iap_basis,
        rc.rc_revenue_usd AS iap_gross_after_refunds_usd,
        rc.rc_refunds,
        CASE WHEN aa.app_id IS NOT NULL THEN COALESCE(ad.earnings_usd, 0) ELSE au.ga4_ad_usd END AS ad_revenue_usd,
        CASE WHEN aa.app_id IS NOT NULL THEN 'admob_earnings'
             WHEN au.ga4_ad_usd IS NOT NULL THEN 'ga4_ad_impression' END AS ad_basis,
        ad.impressions AS ad_impressions,
        ad.earnings_usd AS admob_earnings_usd,
        CASE WHEN ra.app_id IS NOT NULL THEN COALESCE(rc.rc_payers, 0) ELSE au.ga4_payers END AS payers,
        CASE WHEN ca.app_id IS NOT NULL THEN COALESCE(cr.crashed_installs, 0) ELSE au.ga4_crashed_users END AS crashed_users,
        CASE WHEN ca.app_id IS NOT NULL THEN COALESCE(cr.fatal_events, 0) ELSE au.ga4_crashed_sessions END AS crashed_sessions,
        CASE WHEN ca.app_id IS NOT NULL THEN 'crashlytics' WHEN au.ga4_crashed_users IS NOT NULL THEN 'ga4_app_exception' END AS crash_basis,
        st.store_installs,
        st.store_uninstalls,
        st.rating
    FROM days d
    LEFT JOIN au ON au.app_id = d.app_id AND au.date = d.date
    LEFT JOIN rc ON rc.app_id = d.app_id AND rc.date = d.date
    LEFT JOIN stg_admob_daily ad ON ad.app_id = d.app_id AND ad.date = d.date
    LEFT JOIN cr ON cr.app_id = d.app_id AND cr.date = d.date
    LEFT JOIN st ON st.app_id = d.app_id AND st.date = d.date
    LEFT JOIN rc_apps ra ON ra.app_id = d.app_id
    LEFT JOIN admob_apps aa ON aa.app_id = d.app_id
    LEFT JOIN cr_apps ca ON ca.app_id = d.app_id
)
SELECT
    *,
    CAST(dau AS DOUBLE) / NULLIF(mau, 0) AS stickiness,
    CAST(sessions AS DOUBLE) / NULLIF(dau, 0) AS sessions_per_dau,
    engagement_sec / 60.0 / NULLIF(dau, 0) AS engagement_min_per_dau,
    COALESCE(iap_revenue_usd, 0) + COALESCE(ad_revenue_usd, 0) AS revenue_usd,
    (COALESCE(iap_revenue_usd, 0) + COALESCE(ad_revenue_usd, 0)) / NULLIF(dau, 0) AS arpdau,
    CAST(payers AS DOUBLE) / NULLIF(dau, 0) AS payer_conversion,
    admob_earnings_usd * 1000.0 / NULLIF(ad_impressions, 0) AS ecpm_usd,
    -- Crash counts come from Crashlytics while DAU/sessions come from GA4. A
    -- crash loop on one device can produce more fatal events than GA4 saw
    -- sessions, so numerators are capped at their denominators.
    1 - CAST(LEAST(crashed_users, dau) AS DOUBLE) / NULLIF(dau, 0) AS crash_free_users,
    1 - CAST(LEAST(crashed_sessions, sessions) AS DOUBLE) / NULLIF(sessions, 0) AS crash_free_sessions
FROM joined
ORDER BY app_id, date
