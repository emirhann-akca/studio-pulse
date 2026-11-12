-- SDK purchase events, one row per transaction_id (a re-sent receipt counts
-- once). revenue_usd is the price paid converted with raw_fx_rates (the
-- config's fx_rates); sandbox purchases (TestFlight, test cards) and restores
-- carry no revenue.
WITH p AS (
    SELECT
        app_id, platform, date, ts, install_id,
        params->>'transaction_id' AS transaction_id,
        params->>'product_id' AS product_id,
        CAST(params->>'price_micros' AS BIGINT) AS price_micros,
        upper(params->>'currency') AS currency,
        COALESCE(CAST(params->>'is_sandbox' AS BOOLEAN), false) AS is_sandbox,
        COALESCE(CAST(params->>'is_restore' AS BOOLEAN), false) AS is_restore
    FROM stg_pulse_events
    WHERE is_prod AND name = 'purchase'
    QUALIFY row_number() OVER (PARTITION BY app_id, params->>'transaction_id' ORDER BY ts, event_id) = 1
),
fx AS (
    SELECT currency, arg_max(usd_per_unit, valid_from) AS usd_per_unit FROM raw_fx_rates GROUP BY 1
)
SELECT
    p.*,
    p.price_micros / 1e6 AS price,
    CASE WHEN p.is_sandbox OR p.is_restore THEN 0
         ELSE p.price_micros / 1e6 * fx.usd_per_unit END AS revenue_usd
FROM p
LEFT JOIN fx USING (currency)
