-- Retention cohort matrix: for each install cohort and day N (0..30), the
-- share of the cohort that was active on day N. A cell is complete when day N
-- has been observed (cohort_date + N <= last date with data for the app).
WITH cd AS (
    SELECT app_id, cohort_date, day_n, SUM(users) AS users
    FROM stg_cohort_days GROUP BY 1, 2, 3
),
sizes AS (
    SELECT app_id, cohort_date, users AS cohort_size FROM cd WHERE day_n = 0 AND users > 0
),
last_day AS (
    SELECT app_id, MAX(date) AS last_date FROM stg_active_users GROUP BY 1
    UNION ALL
    SELECT app_id, MAX(cohort_date + day_n) FROM cd
    WHERE app_id NOT IN (SELECT app_id FROM stg_active_users) GROUP BY 1
),
grid AS (
    SELECT s.app_id, s.cohort_date, s.cohort_size, g.day_n
    FROM sizes s CROSS JOIN range(0, 31) AS g(day_n)
)
SELECT
    g.app_id,
    g.cohort_date,
    g.cohort_size,
    CAST(g.day_n AS INTEGER) AS day_n,
    COALESCE(cd.users, 0) AS retained_users,
    CAST(COALESCE(cd.users, 0) AS DOUBLE) / g.cohort_size AS retention,
    (g.cohort_date + CAST(g.day_n AS INTEGER)) <= l.last_date AS is_complete
FROM grid g
JOIN last_day l ON l.app_id = g.app_id
LEFT JOIN cd ON cd.app_id = g.app_id AND cd.cohort_date = g.cohort_date AND cd.day_n = g.day_n
ORDER BY 1, 2, 4
