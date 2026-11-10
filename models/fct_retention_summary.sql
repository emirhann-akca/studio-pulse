-- Headline D1/D7/D14/D30 retention per app: cohort-size weighted average over
-- the 28 most recent cohorts that are complete for that day.
WITH ranked AS (
    SELECT *, row_number() OVER (PARTITION BY app_id, day_n ORDER BY cohort_date DESC) AS rk
    FROM fct_retention
    WHERE is_complete AND day_n IN (1, 7, 14, 30)
)
SELECT
    app_id,
    day_n,
    CAST(SUM(retained_users) AS DOUBLE) / NULLIF(SUM(cohort_size), 0) AS retention,
    SUM(cohort_size) AS cohort_users,
    COUNT(*) AS cohorts,
    MIN(cohort_date) AS first_cohort,
    MAX(cohort_date) AS last_cohort
FROM ranked
WHERE rk <= 28
GROUP BY 1, 2
ORDER BY 1, 2
