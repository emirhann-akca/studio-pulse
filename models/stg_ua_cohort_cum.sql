-- Paid install cohorts: for each app, network, campaign and spend day (spend on
-- day d buys the installs of cohort d) the spend, the installs and the
-- cumulative revenue of those installs by day N (complete days only, N <= 90).
--
-- Two attribution bases, set per network in the config (meta_ua_networks):
--   ga4               installs = GA4 users whose first-touch traffic source is
--                     the campaign (traffic_source.source in ga4_sources and
--                     traffic_source.name = campaign name, case-insensitive);
--                     revenue = their GA4 purchase + ad_impression values.
--   network_installs  installs = the network's own install count; revenue =
--                     installs x the cumulative ARPU of the app's whole cohort
--                     of that day (fct_cohort_ltv): a model that assumes paid
--                     users monetise like that day's average new user.
WITH spend AS (
    SELECT app_id, network, campaign_key, MAX(campaign_name) AS campaign_name,
           date AS cohort_date, SUM(spend_usd) AS spend_usd,
           SUM(network_installs) AS network_installs
    FROM stg_ua_spend
    GROUP BY app_id, network, campaign_key, date
),
nets AS (SELECT app_id, network, attribution, ga4_sources FROM meta_ua_networks),
basis AS (
    SELECT s.*, COALESCE(n.attribution, 'network_installs') AS attribution
    FROM spend s LEFT JOIN nets n USING (app_id, network)
),
last_day AS (
    SELECT app_id, MAX(cohort_date + day_n) AS last_date FROM stg_cohort_days GROUP BY 1
),
grid AS (
    SELECT b.*, CAST(d.day_n AS INTEGER) AS day_n
    FROM basis b
    CROSS JOIN range(0, 91) AS d(day_n)
    JOIN last_day l ON l.app_id = b.app_id
    WHERE b.cohort_date + CAST(d.day_n AS INTEGER) <= l.last_date
),
ga4 AS (
    SELECT c.app_id, n.network, lower(trim(c.ts_campaign)) AS campaign_match,
           c.cohort_date, c.day_n, SUM(c.users) AS users,
           SUM(c.iap_revenue_usd + c.ad_revenue_usd) AS revenue_usd
    FROM stg_campaign_cohorts c
    JOIN nets n
        ON n.app_id = c.app_id AND n.attribution = 'ga4'
        AND list_contains(n.ga4_sources, c.ts_source)
    GROUP BY 1, 2, 3, 4, 5
),
joined AS (
    SELECT
        g.*,
        ga.revenue_usd AS ga4_revenue_usd,
        g0.users AS attributed_users,
        l.cum_revenue_usd AS cohort_cum_revenue_usd,
        l.cohort_size
    FROM grid g
    LEFT JOIN ga4 ga
        ON g.attribution = 'ga4' AND ga.app_id = g.app_id AND ga.network = g.network
        AND ga.campaign_match = lower(trim(g.campaign_name))
        AND ga.cohort_date = g.cohort_date AND ga.day_n = g.day_n
    LEFT JOIN ga4 g0
        ON g.attribution = 'ga4' AND g0.app_id = g.app_id AND g0.network = g.network
        AND g0.campaign_match = lower(trim(g.campaign_name))
        AND g0.cohort_date = g.cohort_date AND g0.day_n = 0
    LEFT JOIN fct_cohort_ltv l
        ON l.app_id = g.app_id AND l.cohort_date = g.cohort_date AND l.day_n = g.day_n
)
SELECT
    app_id, network, campaign_key, campaign_name, cohort_date, day_n, attribution,
    spend_usd,
    network_installs,
    CASE WHEN attribution = 'ga4' THEN COALESCE(attributed_users, 0)
         ELSE network_installs END AS installs,
    CASE WHEN attribution = 'ga4' THEN
             SUM(COALESCE(ga4_revenue_usd, 0)) OVER (
                 PARTITION BY app_id, network, campaign_key, cohort_date
                 ORDER BY day_n ROWS UNBOUNDED PRECEDING)
         ELSE network_installs * cohort_cum_revenue_usd / NULLIF(cohort_size, 0)
    END AS cum_revenue_usd,
    cohort_cum_revenue_usd / NULLIF(cohort_size, 0) AS cohort_arpu_usd
FROM joined
WHERE attribution = 'ga4' OR cohort_size IS NOT NULL
