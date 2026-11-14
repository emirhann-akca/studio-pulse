-- Google Play's realInstalls counter per package and snapshot day, with whether it changed
-- since the previous read and how the Play rating count moved meanwhile. A read where the
-- installs did not change but the ratings grew means the counter was not refreshed: such
-- reads are never a window endpoint for the download curves (estimate/downloads.py).
WITH p AS (
  SELECT app_id AS play_id, snapshot_date, fetched_at, real_installs, rating_count
  FROM raw_mr_app
  WHERE store = 'play' AND real_installs IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id, snapshot_date ORDER BY fetched_at DESC) = 1
)
SELECT
  play_id, snapshot_date, fetched_at, real_installs, rating_count,
  LAG(real_installs) OVER w AS prev_installs,
  LAG(rating_count) OVER w AS prev_ratings,
  date_diff('minute', LAG(fetched_at) OVER w, fetched_at) / 1440.0 AS days_since_prev,
  real_installs <> LAG(real_installs) OVER w AS refreshed,
  real_installs = LAG(real_installs) OVER w
    AND rating_count > LAG(rating_count) OVER w AS stale
FROM p
WINDOW w AS (PARTITION BY play_id ORDER BY fetched_at)
