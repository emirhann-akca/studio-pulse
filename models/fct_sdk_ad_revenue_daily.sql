-- SDK impression-level ad revenue per app, day and ad format.
SELECT
    app_id,
    date,
    ad_format,
    COUNT(*) AS impressions,
    SUM(revenue_usd) AS revenue_usd,
    SUM(revenue_usd) * 1000.0 / NULLIF(COUNT(*), 0) AS ecpm_usd
FROM stg_sdk_ad_impressions
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
