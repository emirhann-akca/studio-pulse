-- Pooled ROAS curve by days since install, per campaign, per network
-- (campaign_key '(all)') and per app (network '(all)'). Like the LTV curve,
-- day N pools only the cohorts that have reached day N:
--   roas(N) = sum of cumulative revenue by day N / sum of spend, over those cohorts
-- Points backed by fewer than 7 cohorts are dropped. The app rows also carry
-- blended_roas: the revenue of every new user of those cohort days (paid and
-- organic, fct_cohort_ltv) / the same spend.
WITH pooled AS (
    SELECT
        app_id,
        CASE WHEN GROUPING(network) = 1 THEN '(all)' ELSE network END AS network,
        CASE WHEN GROUPING(campaign_key) = 1 THEN '(all)' ELSE campaign_key END AS campaign_key,
        CASE WHEN GROUPING(campaign_key) = 1 THEN '(all)' ELSE MAX(campaign_name) END AS campaign_name,
        day_n,
        CASE WHEN COUNT(DISTINCT attribution) > 1 THEN 'mixed' ELSE MAX(attribution) END AS attribution,
        COUNT(DISTINCT cohort_date) AS cohorts,
        SUM(spend_usd) AS spend_usd,
        SUM(installs) AS installs,
        SUM(cum_revenue_usd) AS revenue_usd
    FROM stg_ua_cohort_cum
    GROUP BY GROUPING SETS ((app_id, network, campaign_key, day_n), (app_id, network, day_n), (app_id, day_n))
),
blended AS (
    SELECT u.app_id, u.day_n, SUM(l.cum_revenue_usd) AS all_users_revenue_usd
    FROM (SELECT DISTINCT app_id, cohort_date, day_n FROM stg_ua_cohort_cum) u
    JOIN fct_cohort_ltv l USING (app_id, cohort_date, day_n)
    GROUP BY 1, 2
)
SELECT
    p.app_id, p.network, p.campaign_key, p.campaign_name, p.day_n, p.attribution,
    p.cohorts, p.spend_usd, p.installs, p.revenue_usd,
    p.revenue_usd / NULLIF(p.spend_usd, 0) AS roas,
    CASE WHEN p.network = '(all)' THEN b.all_users_revenue_usd / NULLIF(p.spend_usd, 0) END AS blended_roas
FROM pooled p
LEFT JOIN blended b ON b.app_id = p.app_id AND b.day_n = p.day_n
WHERE p.cohorts >= 7
ORDER BY p.app_id, p.network, p.campaign_key, p.day_n
