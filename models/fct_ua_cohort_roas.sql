-- ROAS of each paid install cohort (app x network x campaign x spend day).
--   roas_dN   cumulative revenue of the cohort by day N / spend of that day
--             (NULL until day N is complete); revenue basis per network, see
--             stg_ua_cohort_cum
--   cpi_usd   spend / installs (installs on the same basis)
SELECT
    app_id, network, campaign_key,
    MAX(campaign_name) AS campaign_name,
    cohort_date,
    MAX(attribution) AS attribution,
    MAX(spend_usd) AS spend_usd,
    MAX(network_installs) AS network_installs,
    MAX(installs) AS installs,
    MAX(spend_usd) / NULLIF(MAX(installs), 0) AS cpi_usd,
    MAX(CASE WHEN day_n = 0 THEN cum_revenue_usd END) AS revenue_d0_usd,
    MAX(CASE WHEN day_n = 7 THEN cum_revenue_usd END) AS revenue_d7_usd,
    MAX(CASE WHEN day_n = 30 THEN cum_revenue_usd END) AS revenue_d30_usd,
    MAX(CASE WHEN day_n = 0 THEN cum_revenue_usd END) / NULLIF(MAX(spend_usd), 0) AS roas_d0,
    MAX(CASE WHEN day_n = 7 THEN cum_revenue_usd END) / NULLIF(MAX(spend_usd), 0) AS roas_d7,
    MAX(CASE WHEN day_n = 30 THEN cum_revenue_usd END) / NULLIF(MAX(spend_usd), 0) AS roas_d30,
    MAX(day_n) AS observed_days
FROM stg_ua_cohort_cum
GROUP BY app_id, network, campaign_key, cohort_date
ORDER BY app_id, network, campaign_key, cohort_date
