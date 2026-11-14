-- The only chart input of the market estimates: one row per day, list and rank, whatever the
-- source. Today: the radar's Apple lists (stg_mr_chart: apps.apple.com chart pages, or the
-- legacy iTunes RSS in replays of recorded fixtures) plus raw_mr_rank_extra. list_kind says
-- what a rank measures (free = downloads, grossing = revenue); all_games marks an all-games
-- list (Apple genre 6014). `source` is apple_web, apple_rss (robots-disallowed: its grossing
-- ranks are never used for revenue) or the extra table's own label. Calibration, bands, the
-- panel and the game page read this model only.
WITH src AS (
  SELECT snapshot_date, country, chart, genre_id, any_value(source_url) AS url
  FROM raw_mr_chart GROUP BY ALL
)
SELECT
  c.snapshot_date, c.store, c.country, c.list_key,
  CASE
    WHEN c.chart LIKE 'topfree%' THEN 'free'
    WHEN c.chart LIKE 'topgrossing%' THEN 'grossing'
    WHEN c.chart LIKE 'toppaid%' THEN 'paid'
    ELSE 'other'
  END AS list_kind,
  c.genre_id = '6014' AS all_games,
  c.list_len, c.rank, c.app_id, c.bundle_id, c.name,
  CASE
    WHEN src.url LIKE 'https://apps.apple.com/%' THEN 'apple_web'
    WHEN src.url LIKE 'https://itunes.apple.com/%' THEN 'apple_rss'
    ELSE 'other'
  END AS source
FROM stg_mr_chart AS c
LEFT JOIN src USING (snapshot_date, country, chart, genre_id)
UNION ALL BY NAME
SELECT
  snapshot_date, store, country, list_key, list_kind, COALESCE(all_games, FALSE) AS all_games,
  list_len, rank, app_id, bundle_id, name, source
FROM raw_mr_rank_extra
WHERE rank IS NOT NULL AND app_id IS NOT NULL AND rank <= COALESCE(list_len, rank)
