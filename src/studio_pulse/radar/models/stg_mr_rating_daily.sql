-- Apple rating count per day and storefront, and its velocity (ratings/day).
-- Window: the latest earlier snapshot taken 3 to 7 days before (3 days when the radar runs
-- daily). No earlier snapshot: lifetime average since release, labelled 'lifetime'.
-- A lower count than the window's base is a rating reset only when it persists: the latest
-- snapshot before this one (after the base) is lower too. A drop seen on one day only is
-- 'unconfirmed_drop' (a transient Lookup answer); either way the velocity is NULL, never
-- negative and never a transient zero.
-- Counts are each day's maximum read (run.py rechecks zeros, drops and disagreements). The
-- batched id Lookup is the primary source; a publisher-catalogue row only fills apps the
-- batched Lookup did not return.
WITH a AS (
  SELECT snapshot_date, country, app_id, rating_count, release_date
  FROM raw_mr_app
  WHERE store = 'apple' AND rating_count IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY snapshot_date, country, app_id
    ORDER BY CASE source WHEN 'lookup' THEN 0 ELSE 1 END, fetched_at DESC
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
), p AS (
  SELECT w.*, c.snapshot_date AS prev_date, c.rating_count AS prev_count
  FROM w
  LEFT JOIN a AS c
    ON c.country = w.country AND c.app_id = w.app_id
   AND c.snapshot_date < w.snapshot_date AND c.snapshot_date > w.base_date
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY w.snapshot_date, w.country, w.app_id ORDER BY c.snapshot_date DESC NULLS LAST
  ) = 1
), j AS (
  SELECT p.*,
    base_count IS NOT NULL AND rating_count < base_count AS dropped,
    base_count IS NOT NULL AND rating_count < base_count
      AND prev_count IS NOT NULL AND prev_count < base_count AS reset_confirmed
  FROM p
)
SELECT
  snapshot_date, country, app_id, rating_count, release_date, base_date, base_count,
  prev_date, prev_count,
  date_diff('day', base_date, snapshot_date) AS window_days,
  reset_confirmed AS rating_reset,
  dropped AND NOT reset_confirmed AS rating_drop_unconfirmed,
  CASE
    WHEN base_count IS NULL THEN 'lifetime'
    WHEN reset_confirmed THEN 'reset'
    WHEN dropped THEN 'unconfirmed_drop'
    ELSE 'window'
  END AS rv_method,
  CASE
    WHEN base_count IS NULL
      THEN rating_count / GREATEST(1, date_diff('day', release_date, snapshot_date))::DOUBLE
    WHEN dropped THEN NULL
    ELSE (rating_count - base_count) / date_diff('day', base_date, snapshot_date)::DOUBLE
  END AS rv
FROM j
