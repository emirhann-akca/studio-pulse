-- Creative scorecard per app: one row per creative and network, plus a
-- network '(all)' row per creative. A creative is its creative_id when the ad
-- name parses (creative-forge names by default, see connectors/ua/creative.py),
-- otherwise the network's ad id ("network:ad_id").
--   ipm            network_installs / impressions x 1000, with a 95% Wilson
--                  interval (ipm_low, ipm_high) on installs / impressions
--   ctr, cpi_usd   clicks / impressions, spend / network installs
--   d7_roas        modeled: sum over spend days complete at day 7 of
--                  installs x the app cohort's D7 cumulative ARPU, / the spend
--                  of those days (networks do not attribute revenue to ads)
--   status         'insufficient data' below the minimum impressions
--                  (config ua.min_creative_impressions, default 5000): never ranked
--   ipm_rank       rank by IPM among the creatives with enough data, per app and network
WITH settings AS (
    SELECT COALESCE(
        (SELECT TRY_CAST(value AS BIGINT) FROM meta_run WHERE key = 'ua_min_creative_impressions'),
        5000) AS min_impressions
),
ads AS (
    SELECT s.*,
           COALESCE(s.creative_id, s.network || ':' || COALESCE(s.ad_id, s.ad_name, '(none)')) AS creative_key,
           l.cum_revenue_usd / NULLIF(l.cohort_size, 0) AS arpu_d7_usd
    FROM stg_ua_spend s
    LEFT JOIN fct_cohort_ltv l ON l.app_id = s.app_id AND l.cohort_date = s.date AND l.day_n = 7
),
agg AS (
    SELECT
        app_id,
        creative_key,
        CASE WHEN GROUPING(network) = 1 THEN '(all)' ELSE network END AS network,
        MAX(creative_id) AS creative_id,
        MAX(creative_concept) AS creative_concept,
        MAX(ad_name) AS example_ad_name,
        COUNT(DISTINCT ad_id) AS ads,
        MIN(date) AS first_date,
        MAX(date) AS last_date,
        SUM(impressions) AS impressions,
        SUM(clicks) AS clicks,
        SUM(network_installs) AS network_installs,
        SUM(spend_usd) AS spend_usd,
        SUM(CASE WHEN arpu_d7_usd IS NOT NULL THEN network_installs * arpu_d7_usd END) AS d7_revenue_usd,
        SUM(CASE WHEN arpu_d7_usd IS NOT NULL THEN spend_usd END) AS d7_spend_usd
    FROM ads
    GROUP BY GROUPING SETS ((app_id, creative_key, network), (app_id, creative_key))
),
scored AS (
    SELECT
        a.*,
        CAST(a.network_installs AS DOUBLE) / NULLIF(a.impressions, 0) AS p,
        CAST(a.impressions AS DOUBLE) AS n,
        CASE WHEN COALESCE(a.impressions, 0) < st.min_impressions THEN 'insufficient data'
             ELSE 'ranked' END AS status,
        st.min_impressions
    FROM agg a CROSS JOIN settings st
)
SELECT
    app_id, network, creative_key, creative_id, creative_concept, example_ad_name, ads,
    first_date, last_date, impressions, clicks, network_installs, spend_usd,
    p * 1000 AS ipm,
    CASE WHEN n > 0 THEN 1000 * ((p + 1.96 * 1.96 / (2 * n))
        - 1.96 * sqrt(p * (1 - p) / n + 1.96 * 1.96 / (4 * n * n))) / (1 + 1.96 * 1.96 / n) END AS ipm_low,
    CASE WHEN n > 0 THEN 1000 * ((p + 1.96 * 1.96 / (2 * n))
        + 1.96 * sqrt(p * (1 - p) / n + 1.96 * 1.96 / (4 * n * n))) / (1 + 1.96 * 1.96 / n) END AS ipm_high,
    CAST(clicks AS DOUBLE) / NULLIF(impressions, 0) AS ctr,
    spend_usd / NULLIF(network_installs, 0) AS cpi_usd,
    d7_revenue_usd / NULLIF(d7_spend_usd, 0) AS d7_roas,
    status,
    min_impressions,
    CASE WHEN status = 'ranked' THEN
        rank() OVER (PARTITION BY app_id, network, status ORDER BY p DESC NULLS LAST, creative_key)
    END AS ipm_rank
FROM scored
ORDER BY app_id, network, status, ipm_rank NULLS LAST, impressions DESC, creative_key
