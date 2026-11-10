-- Portfolio view: one row per app with the latest 7-day headline metrics.
-- "7d" = the 7 days ending at the app's last day with DAU data.
WITH last_day AS (
    SELECT app_id, MAX(date) AS last_date FROM fct_app_daily WHERE dau IS NOT NULL GROUP BY 1
),
w AS (
    SELECT f.*, l.last_date FROM fct_app_daily f JOIN last_day l USING (app_id)
),
cur AS (
    SELECT app_id,
           avg(dau) AS dau_7d,
           SUM(new_users) AS new_users_7d,
           SUM(store_installs) AS store_installs_7d,
           SUM(revenue_usd) AS revenue_7d_usd,
           SUM(iap_revenue_usd) AS iap_revenue_7d_usd,
           SUM(ad_revenue_usd) AS ad_revenue_7d_usd,
           SUM(revenue_usd) / NULLIF(SUM(dau), 0) AS arpdau_7d,
           CAST(SUM(payers) AS DOUBLE) / NULLIF(SUM(dau), 0) AS payer_conversion_7d,
           1 - CAST(SUM(LEAST(crashed_users, dau)) AS DOUBLE) / NULLIF(SUM(dau), 0) AS crash_free_users_7d,
           1 - CAST(SUM(LEAST(crashed_sessions, sessions)) AS DOUBLE) / NULLIF(SUM(sessions), 0) AS crash_free_sessions_7d,
           CAST(SUM(sessions) AS DOUBLE) / NULLIF(SUM(dau), 0) AS sessions_per_dau_7d
    FROM w WHERE date > last_date - 7 AND date <= last_date GROUP BY 1
),
prev AS (
    SELECT app_id, avg(dau) AS dau_prev_7d, SUM(revenue_usd) AS revenue_prev_7d_usd
    FROM w WHERE date > last_date - 14 AND date <= last_date - 7 GROUP BY 1
),
latest AS (
    SELECT app_id, mau, wau, stickiness FROM w WHERE date = last_date
),
rating AS (
    SELECT app_id, arg_max(rating, date) AS rating FROM fct_app_daily WHERE rating IS NOT NULL GROUP BY 1
),
ret AS (
    SELECT app_id,
           MAX(CASE WHEN day_n = 1 THEN retention END) AS d1_retention,
           MAX(CASE WHEN day_n = 7 THEN retention END) AS d7_retention,
           MAX(CASE WHEN day_n = 30 THEN retention END) AS d30_retention
    FROM fct_retention_summary GROUP BY 1
),
perf AS (
    SELECT app_id,
           MAX(CASE WHEN device_tier = 'low' THEN ft_p50_ms END) AS low_tier_ft_p50_ms,
           MAX(CASE WHEN device_tier = 'all' THEN ft_p50_ms END) AS ft_p50_ms,
           MAX(CASE WHEN device_tier = 'all' THEN slow_frame_share END) AS slow_frame_share
    FROM fct_perf_by_tier GROUP BY 1
),
ltv AS (
    SELECT app_id, arg_max(cumulative_arpu_usd, day_n) AS ltv_latest_usd, MAX(day_n) AS ltv_latest_day
    FROM fct_ltv_curve WHERE day_n <= 30 GROUP BY 1
)
SELECT
    a.app_id, a.name, a.genre, a.platforms, a.is_demo,
    l.last_date,
    cur.dau_7d, prev.dau_prev_7d,
    cur.dau_7d / NULLIF(prev.dau_prev_7d, 0) - 1 AS dau_change,
    latest.wau, latest.mau, latest.stickiness,
    cur.new_users_7d, cur.store_installs_7d,
    cur.sessions_per_dau_7d,
    ret.d1_retention, ret.d7_retention, ret.d30_retention,
    cur.revenue_7d_usd, prev.revenue_prev_7d_usd,
    cur.iap_revenue_7d_usd, cur.ad_revenue_7d_usd,
    cur.arpdau_7d, cur.payer_conversion_7d,
    ltv.ltv_latest_usd, ltv.ltv_latest_day,
    cur.crash_free_users_7d, cur.crash_free_sessions_7d,
    perf.ft_p50_ms, perf.low_tier_ft_p50_ms, perf.slow_frame_share,
    rating.rating
FROM meta_apps a
LEFT JOIN last_day l USING (app_id)
LEFT JOIN cur USING (app_id)
LEFT JOIN prev USING (app_id)
LEFT JOIN latest USING (app_id)
LEFT JOIN ret USING (app_id)
LEFT JOIN perf USING (app_id)
LEFT JOIN ltv USING (app_id)
LEFT JOIN rating USING (app_id)
ORDER BY a.app_id
