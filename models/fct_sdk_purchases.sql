-- SDK purchases per app and product: revenue in USD (no sandbox, no restores).
SELECT
    app_id,
    product_id,
    COUNT(*) FILTER (WHERE revenue_usd > 0) AS purchases,
    COUNT(DISTINCT install_id) FILTER (WHERE revenue_usd > 0) AS payers,
    SUM(revenue_usd) AS revenue_usd,
    COUNT(*) FILTER (WHERE is_sandbox) AS sandbox_purchases,
    COUNT(*) FILTER (WHERE is_restore) AS restores,
    COUNT(*) FILTER (WHERE revenue_usd IS NULL) AS without_fx_rate
FROM stg_sdk_purchases
GROUP BY 1, 2
ORDER BY 1, 2
