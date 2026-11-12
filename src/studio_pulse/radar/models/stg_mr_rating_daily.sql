-- Apple rating count per day and storefront, and its velocity (ratings/day).
-- Window: the latest earlier snapshot taken 3 to 7 days before (3 days when the radar runs
-- daily); a drop in the count is a rating reset: flagged, velocity NULL (never negative).
-- No earlier snapshot: lifetime average since release, labelled 'lifetime'.
WITH a AS (
  SELECT snapshot_date, country, app_id, rating_count, release_date
  FROM raw_mr_app
  WHERE store = 'apple' AND rating_count IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY snapshot_date, country, app_id ORDER BY fetched_at DESC
  ) = 1
), w AS (
  SELECT a.*, b.snapshot_date AS base_date, b.rating_count AS base_count
  FROM a
  LEFT JOIN a AS b
    ON b.country = a.country AND b.app_id = a.app_id
   AND b.snapshot_date BETWEEN a.snapshot_date - 7 AND a.snapshot_date - 3
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY a.snapshot_date, a.country, a.app_id ORDER BY b.snapshot_date DESC NULLS LAST
  ) = 1
)
SELECT
  snapshot_date, country, app_id, rating_count, release_date, base_date, base_count,
  date_diff('day', base_date, snapshot_date) AS window_days,
  base_count IS NOT NULL AND rating_count < base_count AS rating_reset,
  CASE
    WHEN base_count IS NULL THEN 'lifetime'
    WHEN rating_count < base_count THEN 'reset'
    ELSE 'window'
  END AS rv_method,
  CASE
    WHEN base_count IS NULL
      THEN rating_count / GREATEST(1, date_diff('day', release_date, snapshot_date))::DOUBLE
    WHEN rating_count < base_count THEN NULL
    ELSE (rating_count - base_count) / date_diff('day', base_date, snapshot_date)::DOUBLE
  END AS rv
FROM w
