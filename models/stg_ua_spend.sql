-- Ad-network spend per day, network, campaign, ad and country, in USD.
-- spend_usd = spend for USD accounts, otherwise spend x the fallback FX rate
-- valid on that day (raw_fx_rates); NULL when the currency has no rate, which
-- the check ua_spend_converted reports. campaign_key identifies a campaign
-- (its id, or its name when the export has no id).
WITH fx AS (
    SELECT currency, usd_per_unit, valid_from,
           lead(valid_from) OVER (PARTITION BY currency ORDER BY valid_from) AS valid_to
    FROM raw_fx_rates
),
s AS (
    SELECT
        app_id, date, lower(network) AS network, account_id,
        COALESCE(campaign_id, campaign_name, '(none)') AS campaign_key,
        COALESCE(campaign_name, campaign_id, '(none)') AS campaign_name,
        adset_id, adset_name, ad_id, ad_name, creative_id, creative_concept,
        upper(country) AS country, upper(platform) AS platform,
        spend, upper(COALESCE(currency, 'USD')) AS currency,
        impressions, clicks, network_installs, _source
    FROM raw_ua_spend
)
SELECT
    s.app_id, s.date, s.network, s.account_id, s.campaign_key, s.campaign_name,
    s.adset_id, s.adset_name, s.ad_id, s.ad_name, s.creative_id, s.creative_concept,
    s.country, s.platform, s.currency, s.spend,
    CASE WHEN s.currency = 'USD' THEN s.spend ELSE s.spend * fx.usd_per_unit END AS spend_usd,
    s.impressions, s.clicks, s.network_installs, s._source AS source
FROM s
LEFT JOIN fx
    ON fx.currency = s.currency AND s.date >= fx.valid_from
    AND (fx.valid_to IS NULL OR s.date < fx.valid_to)
