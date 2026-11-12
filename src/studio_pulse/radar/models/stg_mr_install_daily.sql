-- Google Play page data per day and package: install bucket, realInstalls (an undocumented
-- page field, labelled store data) and its velocity over the same 3-7 day window as ratings,
-- else the lifetime average since the Play release. A bucket change is flagged.
WITH a AS (
  SELECT snapshot_date, app_id AS play_id, installs_bucket, min_installs, real_installs,
         rating_count, release_date AS play_released
  FROM raw_mr_app
  WHERE store = 'play'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY snapshot_date, app_id ORDER BY fetched_at DESC) = 1
), w AS (
  SELECT a.*, b.snapshot_date AS base_date, b.real_installs AS base_installs
  FROM a
  LEFT JOIN a AS b
    ON b.play_id = a.play_id AND b.real_installs IS NOT NULL
   AND b.snapshot_date BETWEEN a.snapshot_date - 7 AND a.snapshot_date - 3
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY a.snapshot_date, a.play_id ORDER BY b.snapshot_date DESC NULLS LAST
  ) = 1
), prev AS (
  SELECT x.snapshot_date, x.play_id, y.installs_bucket AS prev_bucket, y.min_installs AS prev_min
  FROM a AS x
  JOIN a AS y ON y.play_id = x.play_id AND y.snapshot_date < x.snapshot_date
  QUALIFY ROW_NUMBER() OVER (PARTITION BY x.snapshot_date, x.play_id ORDER BY y.snapshot_date DESC) = 1
)
SELECT
  w.snapshot_date, w.play_id, w.installs_bucket, w.min_installs, w.real_installs,
  w.rating_count AS play_ratings, w.play_released, w.base_date, w.base_installs,
  prev.prev_bucket,
  date_diff('day', w.base_date, w.snapshot_date) AS window_days,
  COALESCE(prev.prev_min IS NOT NULL AND w.min_installs > prev.prev_min, FALSE) AS bucket_up,
  CASE
    WHEN w.real_installs IS NULL THEN NULL
    WHEN w.base_installs IS NULL THEN 'lifetime'
    WHEN w.real_installs < w.base_installs THEN 'drop'
    ELSE 'window'
  END AS iv_method,
  CASE
    WHEN w.real_installs IS NULL THEN NULL
    WHEN w.base_installs IS NULL AND w.play_released IS NOT NULL
      THEN w.real_installs / GREATEST(1, date_diff('day', w.play_released, w.snapshot_date))::DOUBLE
    WHEN w.base_installs IS NULL THEN NULL
    WHEN w.real_installs < w.base_installs THEN NULL
    ELSE (w.real_installs - w.base_installs) / date_diff('day', w.base_date, w.snapshot_date)::DOUBLE
  END AS iv
FROM w LEFT JOIN prev USING (snapshot_date, play_id)
