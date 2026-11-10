-- Cumulative revenue per installed user (ARPU) by days since install, the
-- usual "LTV curve". For day N only cohorts that have reached day N are used:
--   cumulative_arpu(N) = sum over eligible cohorts of revenue up to day N
--                        / sum of their cohort sizes.
-- Ad revenue comes from GA4 ad_impression values per cohort. IAP comes from
-- RevenueCat proceeds (cohorted by the customer's first_seen date) when the
-- app has RevenueCat data, otherwise from GA4 purchase event values (gross).
-- payer_rate(N) is the share of cohort users who paid at least once by day N.
-- Days backed by fewer than 7 complete cohorts are dropped (too noisy).
WITH sizes AS (
    SELECT app_id, cohort_date, SUM(users) AS cohort_size
    FROM stg_cohort_days WHERE day_n = 0 GROUP BY 1, 2 HAVING SUM(users) > 0
),
last_day AS (
    SELECT app_id, MAX(cohort_date + day_n) AS last_date FROM stg_cohort_days GROUP BY 1
),
rc_apps AS (SELECT DISTINCT app_id FROM stg_rc_transactions),
rc_first_pay AS (
    SELECT app_id, rc_original_app_user_id, MIN(first_seen_date) AS first_seen_date,
           MIN(purchase_date) AS first_pay_date
    FROM stg_rc_transactions WHERE revenue_usd > 0 AND first_seen_date IS NOT NULL
    GROUP BY 1, 2
),
rev AS (
    SELECT app_id, cohort_date, day_n,
           SUM(ad_revenue_usd) AS ad_usd,
           SUM(CASE WHEN app_id IN (SELECT app_id FROM rc_apps) THEN 0 ELSE iap_revenue_usd END) AS iap_usd,
           SUM(CASE WHEN app_id IN (SELECT app_id FROM rc_apps) THEN 0 ELSE new_payers END) AS new_payers
    FROM stg_cohort_days GROUP BY 1, 2, 3
    UNION ALL
    SELECT app_id, first_seen_date, CAST(date_diff('day', first_seen_date, purchase_date) AS INTEGER),
           0, SUM(COALESCE(proceeds_usd, 0)), 0
    FROM stg_rc_transactions
    WHERE first_seen_date IS NOT NULL AND purchase_date >= first_seen_date
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT app_id, first_seen_date, CAST(date_diff('day', first_seen_date, first_pay_date) AS INTEGER),
           0, 0, COUNT(*)
    FROM rc_first_pay
    WHERE first_pay_date >= first_seen_date
    GROUP BY 1, 2, 3
),
rev_c AS (
    SELECT app_id, cohort_date, day_n, SUM(ad_usd) AS ad_usd, SUM(iap_usd) AS iap_usd,
           SUM(new_payers) AS new_payers
    FROM rev GROUP BY 1, 2, 3
),
grid AS (
    SELECT s.app_id, s.cohort_date, s.cohort_size, CAST(g.day_n AS INTEGER) AS day_n
    FROM sizes s
    CROSS JOIN range(0, 91) AS g(day_n)
    JOIN last_day l ON l.app_id = s.app_id
    WHERE s.cohort_date + CAST(g.day_n AS INTEGER) <= l.last_date
),
cum AS (
    SELECT
        g.app_id, g.cohort_date, g.cohort_size, g.day_n,
        SUM(COALESCE(r.ad_usd, 0)) OVER w AS cum_ad_usd,
        SUM(COALESCE(r.iap_usd, 0)) OVER w AS cum_iap_usd,
        SUM(COALESCE(r.new_payers, 0)) OVER w AS cum_payers
    FROM grid g
    LEFT JOIN rev_c r ON r.app_id = g.app_id AND r.cohort_date = g.cohort_date AND r.day_n = g.day_n
    WINDOW w AS (PARTITION BY g.app_id, g.cohort_date ORDER BY g.day_n ROWS UNBOUNDED PRECEDING)
)
SELECT
    app_id,
    day_n,
    COUNT(*) AS cohorts,
    SUM(cohort_size) AS cohort_users,
    SUM(cum_iap_usd) / SUM(cohort_size) AS iap_arpu_usd,
    SUM(cum_ad_usd) / SUM(cohort_size) AS ad_arpu_usd,
    (SUM(cum_iap_usd) + SUM(cum_ad_usd)) / SUM(cohort_size) AS cumulative_arpu_usd,
    CAST(SUM(cum_payers) AS DOUBLE) / SUM(cohort_size) AS payer_rate
FROM cum
GROUP BY 1, 2
HAVING COUNT(*) >= 7
ORDER BY 1, 2
