-- UA delivery per app, day, network and campaign (network-reported numbers).
--   cpi_usd      spend_usd / network_installs
--   ctr          clicks / impressions
--   ipm          network_installs / impressions x 1000 (installs per mille)
--   spend_share  the campaign's share of the app's spend that day
SELECT
    app_id, date, network, campaign_key,
    MAX(campaign_name) AS campaign_name,
    SUM(spend_usd) AS spend_usd,
    SUM(impressions) AS impressions,
    SUM(clicks) AS clicks,
    SUM(network_installs) AS network_installs,
    SUM(spend_usd) / NULLIF(SUM(network_installs), 0) AS cpi_usd,
    CAST(SUM(clicks) AS DOUBLE) / NULLIF(SUM(impressions), 0) AS ctr,
    SUM(network_installs) * 1000.0 / NULLIF(SUM(impressions), 0) AS ipm,
    SUM(spend_usd) / NULLIF(SUM(SUM(spend_usd)) OVER (PARTITION BY app_id, date), 0) AS spend_share
FROM stg_ua_spend
GROUP BY app_id, date, network, campaign_key
ORDER BY app_id, date, network, campaign_key
