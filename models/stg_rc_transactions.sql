-- RevenueCat transactions, one row per transaction.
-- The scheduled export is incremental: a transaction is re-delivered whenever
-- one of its attributes changes (refund, unsubscribe, billing issue ...), so
-- the raw table holds several versions. RevenueCat identifies a transaction by
-- (store_transaction_id, renewal_number) and recommends `updated_at` to find
-- the latest version; file timestamp and row order break remaining ties.
-- Sandbox transactions are dropped. price_in_usd is already net of refunds.
-- Export timestamps are UTC; purchase_date / first_seen_date are calendar days
-- in the app's reporting time zone (meta_apps.timezone), like GA4 event_date.
WITH ranked AS (
    SELECT
        r.*,
        row_number() OVER (
            PARTITION BY app_id, store_transaction_id, COALESCE(renewal_number, 0)
            ORDER BY updated_at DESC NULLS LAST, _file_ts DESC NULLS LAST, _file DESC, _row DESC
        ) AS version_rank,
        count(*) OVER (
            PARTITION BY app_id, store_transaction_id, COALESCE(renewal_number, 0)
        ) AS versions_seen
    FROM raw_revenuecat_transactions r
    WHERE store_transaction_id IS NOT NULL
),
latest AS (
    SELECT
        r.*,
        CAST(timezone(COALESCE(a.timezone, 'UTC'), CAST(r.start_time AS TIMESTAMPTZ)) AS DATE) AS purchase_date,
        CAST(timezone(COALESCE(a.timezone, 'UTC'), CAST(r.first_seen_time AS TIMESTAMPTZ)) AS DATE) AS first_seen_date
    FROM ranked r
    LEFT JOIN meta_apps a ON a.app_id = r.app_id
    WHERE r.version_rank = 1 AND NOT COALESCE(r.is_sandbox, FALSE)
),
fx AS (
    SELECT
        currency,
        usd_per_unit,
        valid_from,
        lead(valid_from) OVER (PARTITION BY currency ORDER BY valid_from) AS valid_to
    FROM raw_fx_rates
),
priced AS (
    SELECT
        l.*,
        fx.usd_per_unit,
        COALESCE(l.price_in_usd, l.price_in_purchased_currency * fx.usd_per_unit) AS revenue_usd,
        COALESCE(
            l.purchase_price_in_usd,
            l.purchase_price_in_purchased_currency * fx.usd_per_unit,
            l.price_in_usd
        ) AS gross_usd
    FROM latest l
    LEFT JOIN fx
        ON fx.currency = upper(l.purchased_currency)
        AND l.purchase_date >= fx.valid_from
        AND (fx.valid_to IS NULL OR l.purchase_date < fx.valid_to)
)
SELECT
    app_id,
    store_transaction_id,
    original_store_transaction_id,
    renewal_number,
    rc_original_app_user_id,
    product_identifier,
    store,
    upper(platform) AS platform,
    country,
    purchase_date,
    first_seen_date,
    COALESCE(is_trial_period, FALSE) AS is_trial_period,
    COALESCE(is_trial_conversion, FALSE) AS is_trial_conversion,
    refunded_at IS NOT NULL AS is_refunded,
    upper(purchased_currency) AS purchased_currency,
    gross_usd,
    revenue_usd,
    CASE
        WHEN price_in_usd IS NOT NULL THEN 'revenuecat_usd'
        WHEN usd_per_unit IS NOT NULL THEN 'fx_table'
        ELSE 'unconverted'
    END AS usd_basis,
    CASE
        WHEN tax_percentage IS NOT NULL OR commission_percentage IS NOT NULL
            THEN revenue_usd * (1 - COALESCE(tax_percentage, 0) - COALESCE(commission_percentage, 0))
        WHEN takehome_percentage IS NOT NULL THEN revenue_usd * takehome_percentage
    END AS proceeds_usd,
    versions_seen
FROM priced
