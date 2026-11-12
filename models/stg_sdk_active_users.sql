-- SDK apps in the shape of raw_ga4_active_users, so stg_active_users and every
-- model after it (fct_app_daily, the portfolio page, alerts) treat SDK apps
-- like GA4 apps. Users are installs (install_id) of production builds:
--   dau / wau / mau      distinct installs with an event that day / in the 7 / 30 days up to it
--   new_users            installs whose pulse_install happened that day
--   sessions, engagement sessions starting that day, their foreground time
--   crashed_*            installs / sessions with a crash, ANR or fatal error
--   iap / ad revenue     purchases (no sandbox, no restores) and ad_impression value, in USD
WITH act AS (
    SELECT DISTINCT app_id, platform, date, install_id FROM stg_pulse_events WHERE is_prod
),
days AS (
    SELECT DISTINCT app_id, platform, date FROM act
),
first_day AS (
    SELECT app_id, MIN(date) AS first_date FROM act GROUP BY 1
),
dau AS (
    SELECT app_id, platform, date, COUNT(*) AS dau FROM act GROUP BY ALL
),
wau AS (
    SELECT d.app_id, d.platform, d.date,
           COUNT(DISTINCT a.install_id) FILTER (WHERE a.date > d.date - 7) AS wau,
           COUNT(DISTINCT a.install_id) AS mau
    FROM days d
    JOIN act a ON a.app_id = d.app_id AND a.platform = d.platform
              AND a.date > d.date - 30 AND a.date <= d.date
    GROUP BY ALL
),
new_users AS (
    SELECT app_id, platform, cohort_date AS date, COUNT(*) AS new_users
    FROM stg_sdk_installs WHERE cohort_date IS NOT NULL GROUP BY ALL
),
sess AS (
    SELECT app_id, platform, date,
           COUNT(*) AS sessions,
           SUM(length_ms) / 1000.0 AS engagement_sec,
           COUNT(DISTINCT install_id) FILTER (WHERE crashed) AS crashed_users,
           COUNT(*) FILTER (WHERE crashed) AS crashed_sessions
    FROM stg_sdk_sessions GROUP BY ALL
),
iap AS (
    SELECT app_id, platform, date,
           SUM(revenue_usd) AS iap_revenue_usd,
           COUNT(DISTINCT install_id) FILTER (WHERE revenue_usd > 0) AS payers
    FROM stg_sdk_purchases GROUP BY ALL
),
ads AS (
    SELECT app_id, platform, date, SUM(revenue_usd) AS ad_revenue_usd
    FROM stg_sdk_ad_impressions GROUP BY ALL
)
SELECT
    d.app_id,
    d.platform,
    d.date,
    dau.dau,
    wau.wau,
    wau.mau,
    COALESCE(n.new_users, 0) AS new_users,
    COALESCE(s.sessions, 0) AS sessions,
    COALESCE(s.engagement_sec, 0) AS engagement_sec,
    COALESCE(s.crashed_users, 0) AS crashed_users,
    COALESCE(s.crashed_sessions, 0) AS crashed_sessions,
    COALESCE(i.iap_revenue_usd, 0) AS iap_revenue_usd,
    COALESCE(ad.ad_revenue_usd, 0) AS ad_revenue_usd,
    COALESCE(i.payers, 0) AS payers,
    CAST(d.date - f.first_date + 1 AS INTEGER) AS history_days,
    'pulse_sdk' AS _source,
    CAST(NULL AS TIMESTAMP) AS _loaded_at
FROM days d
JOIN first_day f USING (app_id)
JOIN dau USING (app_id, platform, date)
JOIN wau USING (app_id, platform, date)
LEFT JOIN new_users n USING (app_id, platform, date)
LEFT JOIN sess s USING (app_id, platform, date)
LEFT JOIN iap i USING (app_id, platform, date)
LEFT JOIN ads ad USING (app_id, platform, date)
