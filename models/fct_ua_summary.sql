-- One row per app (network '(all)'), network (campaign_key '(all)') and
-- campaign: delivery over the 30 days ending at the app's last spend day, and
-- the pooled ROAS curve at D0 / D7 / D30 with the observed payback day.
--   cpi_30d_usd     spend / installs over the 30 days (installs on the network's
--                   attribution basis; network_cpi_30d_usd uses network installs)
--   payback_day     first day N whose pooled ROAS reaches 100%; NULL when the
--                   curve has not reached it within its observed days
WITH last AS (SELECT app_id, MAX(date) AS last_date FROM stg_ua_spend GROUP BY 1),
recent AS (
    SELECT
        s.app_id,
        CASE WHEN GROUPING(s.network) = 1 THEN '(all)' ELSE s.network END AS network,
        CASE WHEN GROUPING(s.campaign_key) = 1 THEN '(all)' ELSE s.campaign_key END AS campaign_key,
        CASE WHEN GROUPING(s.campaign_key) = 1 THEN '(all)' ELSE MAX(s.campaign_name) END AS campaign_name,
        SUM(s.spend_usd) AS spend_30d_usd,
        SUM(s.impressions) AS impressions_30d,
        SUM(s.clicks) AS clicks_30d,
        SUM(s.network_installs) AS network_installs_30d
    FROM stg_ua_spend s JOIN last l USING (app_id)
    WHERE s.date > l.last_date - 30
    GROUP BY GROUPING SETS ((s.app_id, s.network, s.campaign_key), (s.app_id, s.network), (s.app_id))
),
inst AS (
    SELECT
        c.app_id,
        CASE WHEN GROUPING(c.network) = 1 THEN '(all)' ELSE c.network END AS network,
        CASE WHEN GROUPING(c.campaign_key) = 1 THEN '(all)' ELSE c.campaign_key END AS campaign_key,
        SUM(c.installs) AS installs_30d,
        CASE WHEN COUNT(DISTINCT c.attribution) > 1 THEN 'mixed' ELSE MAX(c.attribution) END AS attribution
    FROM stg_ua_cohort_cum c JOIN last l USING (app_id)
    WHERE c.day_n = 0 AND c.cohort_date > l.last_date - 30
    GROUP BY GROUPING SETS ((c.app_id, c.network, c.campaign_key), (c.app_id, c.network), (c.app_id))
),
curve AS (
    SELECT app_id, network, campaign_key,
           MAX(CASE WHEN day_n = 0 THEN roas END) AS roas_d0,
           MAX(CASE WHEN day_n = 7 THEN roas END) AS roas_d7,
           MAX(CASE WHEN day_n = 30 THEN roas END) AS roas_d30,
           MAX(CASE WHEN day_n = 7 THEN blended_roas END) AS blended_roas_d7,
           MAX(CASE WHEN day_n = 30 THEN blended_roas END) AS blended_roas_d30,
           MIN(CASE WHEN roas >= 1 THEN day_n END) AS payback_day,
           MAX(day_n) AS curve_days
    FROM fct_ua_roas_curve GROUP BY 1, 2, 3
)
SELECT
    r.app_id, r.network, r.campaign_key, r.campaign_name,
    i.attribution,
    r.spend_30d_usd, r.impressions_30d, r.clicks_30d, r.network_installs_30d, i.installs_30d,
    r.spend_30d_usd / NULLIF(i.installs_30d, 0) AS cpi_30d_usd,
    r.spend_30d_usd / NULLIF(r.network_installs_30d, 0) AS network_cpi_30d_usd,
    CAST(r.clicks_30d AS DOUBLE) / NULLIF(r.impressions_30d, 0) AS ctr_30d,
    r.network_installs_30d * 1000.0 / NULLIF(r.impressions_30d, 0) AS ipm_30d,
    c.roas_d0, c.roas_d7, c.roas_d30, c.blended_roas_d7, c.blended_roas_d30,
    c.payback_day, c.curve_days
FROM recent r
LEFT JOIN inst i USING (app_id, network, campaign_key)
LEFT JOIN curve c USING (app_id, network, campaign_key)
ORDER BY r.app_id, r.network = '(all)' DESC, r.network, r.campaign_key = '(all)' DESC, r.spend_30d_usd DESC
