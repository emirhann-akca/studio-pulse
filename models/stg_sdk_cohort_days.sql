-- SDK install cohorts in the shape of raw_ga4_cohort_days: installs of cohort
-- `cohort_date` (their pulse_install day) active on day N after it, with their
-- sessions, revenue and first-time payers. Feeds stg_cohort_days, so retention
-- (classic day-N), LTV curves and pLTV work for SDK apps unchanged.
WITH inst AS (
    SELECT app_id, platform, install_id, cohort_date
    FROM stg_sdk_installs WHERE cohort_date IS NOT NULL
),
act AS (
    SELECT DISTINCT app_id, install_id, date FROM stg_pulse_events WHERE is_prod
),
sess AS (
    SELECT app_id, install_id, date, COUNT(*) AS sessions FROM stg_sdk_sessions GROUP BY ALL
),
iap AS (
    SELECT app_id, install_id, date, SUM(revenue_usd) AS iap FROM stg_sdk_purchases GROUP BY ALL
),
ads AS (
    SELECT app_id, install_id, date, SUM(revenue_usd) AS ads FROM stg_sdk_ad_impressions GROUP BY ALL
),
first_pay AS (
    SELECT app_id, install_id, MIN(date) AS date FROM stg_sdk_purchases WHERE revenue_usd > 0 GROUP BY ALL
)
SELECT
    i.app_id,
    i.platform,
    i.cohort_date,
    CAST(a.date - i.cohort_date AS INTEGER) AS day_n,
    COUNT(*) AS users,
    COALESCE(SUM(s.sessions), 0) AS sessions,
    COALESCE(SUM(p.iap), 0) AS iap_revenue_usd,
    COALESCE(SUM(ad.ads), 0) AS ad_revenue_usd,
    COUNT(fp.install_id) AS new_payers,
    'pulse_sdk' AS _source,
    CAST(NULL AS TIMESTAMP) AS _loaded_at
FROM inst i
JOIN act a ON a.app_id = i.app_id AND a.install_id = i.install_id AND a.date >= i.cohort_date
LEFT JOIN sess s ON s.app_id = a.app_id AND s.install_id = a.install_id AND s.date = a.date
LEFT JOIN iap p ON p.app_id = a.app_id AND p.install_id = a.install_id AND p.date = a.date
LEFT JOIN ads ad ON ad.app_id = a.app_id AND ad.install_id = a.install_id AND ad.date = a.date
LEFT JOIN first_pay fp ON fp.app_id = a.app_id AND fp.install_id = a.install_id AND fp.date = a.date
GROUP BY ALL
