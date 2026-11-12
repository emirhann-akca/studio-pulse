-- Weekly mechanic trend: delta = share - mean(share of the 4 previous weeks),
-- z = delta / sqrt(p(1-p)/row_days) with p that mean (a zero p uses 0.5/row_days).
-- TREND when 4 previous weeks exist, delta >= 2 points, z >= 2 and >= 3 distinct apps.
WITH p AS (
  SELECT
    COALESCE(MAX(value) FILTER (WHERE key = 'trend_delta_min'), 0.02) AS delta_min,
    COALESCE(MAX(value) FILTER (WHERE key = 'trend_z_min'), 2) AS z_min,
    COALESCE(MAX(value) FILTER (WHERE key = 'trend_min_apps'), 3) AS min_apps,
    CAST(COALESCE(MAX(value) FILTER (WHERE key = 'trend_prior_weeks'), 4) AS INTEGER) AS prior_weeks
  FROM raw_mr_params
), j AS (
  SELECT w.week, w.country, w.mechanic, w.days, w.row_days, w.app_days, w.n_apps,
         w.new_entries, w.share, w.tagged_share,
         AVG(prev.share) AS prior_mean, COUNT(prev.week) AS prior_weeks_seen
  FROM fct_mr_mechanic_weekly AS w
  CROSS JOIN p
  LEFT JOIN fct_mr_mechanic_weekly AS prev
    ON prev.country = w.country AND prev.mechanic = w.mechanic
   AND prev.week >= w.week - 7 * p.prior_weeks AND prev.week < w.week
  GROUP BY ALL
), z AS (
  SELECT j.*, share - prior_mean AS delta,
    CASE WHEN prior_mean IS NULL THEN NULL
      ELSE (share - prior_mean) / sqrt(
        GREATEST(prior_mean, 0.5 / row_days) * (1 - LEAST(GREATEST(prior_mean, 0.5 / row_days), 1 - 0.5 / row_days))
        / row_days)
    END AS z
  FROM j
)
SELECT z.*,
  COALESCE(prior_weeks_seen >= p.prior_weeks AND delta >= p.delta_min AND z >= p.z_min
           AND n_apps >= p.min_apps, FALSE) AS is_trend
FROM z CROSS JOIN p
