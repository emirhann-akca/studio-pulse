-- AdMob network report per app and day, earnings converted to USD.
WITH fx AS (
    SELECT currency, usd_per_unit, valid_from,
           lead(valid_from) OVER (PARTITION BY currency ORDER BY valid_from) AS valid_to
    FROM raw_fx_rates
)
SELECT
    a.app_id,
    a.date,
    SUM(a.impressions) AS impressions,
    SUM(a.clicks) AS clicks,
    SUM(a.ad_requests) AS ad_requests,
    SUM(a.matched_requests) AS matched_requests,
    SUM(CASE WHEN upper(COALESCE(a.currency, 'USD')) = 'USD' THEN a.earnings
             ELSE a.earnings * fx.usd_per_unit END) AS earnings_usd
FROM raw_admob_daily a
LEFT JOIN fx
    ON fx.currency = upper(a.currency)
    AND a.date >= fx.valid_from
    AND (fx.valid_to IS NULL OR a.date < fx.valid_to)
GROUP BY a.app_id, a.date
