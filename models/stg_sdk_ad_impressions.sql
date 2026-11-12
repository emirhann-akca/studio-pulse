-- SDK ad_impression events (impression-level ad revenue) in USD.
WITH fx AS (
    SELECT currency, arg_max(usd_per_unit, valid_from) AS usd_per_unit FROM raw_fx_rates GROUP BY 1
)
SELECT
    e.app_id, e.platform, e.date, e.install_id,
    e.params->>'ad_platform' AS ad_platform,
    e.params->>'ad_format' AS ad_format,
    e.params->>'ad_source' AS ad_source,
    e.params->>'placement' AS placement,
    CAST(e.params->>'value' AS DOUBLE) AS value,
    upper(e.params->>'currency') AS currency,
    CAST(e.params->>'value' AS DOUBLE) * fx.usd_per_unit AS revenue_usd
FROM stg_pulse_events e
LEFT JOIN fx ON fx.currency = upper(e.params->>'currency')
WHERE e.is_prod AND e.name = 'ad_impression'
