-- Cumulative revenue per installed user (ARPU) by days since install, the
-- usual "LTV curve". For day N only cohorts that have reached day N are used:
--   cumulative_arpu(N) = sum over eligible cohorts of revenue up to day N
--                        / sum of their cohort sizes.
-- Revenue definitions are those of fct_cohort_ltv (RevenueCat proceeds when
-- connected, otherwise GA4 purchase values; plus GA4 ad revenue).
-- payer_rate(N) is the share of cohort users who paid at least once by day N.
-- Days backed by fewer than 7 complete cohorts are dropped (too noisy).
SELECT
    app_id,
    day_n,
    COUNT(*) AS cohorts,
    SUM(cohort_size) AS cohort_users,
    SUM(cum_iap_usd) / SUM(cohort_size) AS iap_arpu_usd,
    SUM(cum_ad_usd) / SUM(cohort_size) AS ad_arpu_usd,
    (SUM(cum_iap_usd) + SUM(cum_ad_usd)) / SUM(cohort_size) AS cumulative_arpu_usd,
    CAST(SUM(cum_payers) AS DOUBLE) / SUM(cohort_size) AS payer_rate
FROM fct_cohort_ltv
GROUP BY 1, 2
HAVING COUNT(*) >= 7
ORDER BY 1, 2
