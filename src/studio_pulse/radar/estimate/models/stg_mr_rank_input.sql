-- The only chart input of the market estimates: one row per day, list and rank, whatever the
-- source. Today: the radar's Apple legacy RSS lists (stg_mr_chart) plus raw_mr_rank_extra.
-- list_kind says what a rank measures (free = downloads, grossing = revenue); all_games marks
-- an all-games list (Apple genre 6014) as opposed to a genre list. Calibration, bands, the
-- panel and the game page read this model only.
SELECT
  snapshot_date, store, country, list_key,
  CASE
    WHEN chart LIKE 'topfree%' THEN 'free'
    WHEN chart LIKE 'topgrossing%' THEN 'grossing'
    WHEN chart LIKE 'toppaid%' THEN 'paid'
    ELSE 'other'
  END AS list_kind,
  genre_id = '6014' AS all_games,
  list_len, rank, app_id, bundle_id, name,
  'apple_rss' AS source
FROM stg_mr_chart
UNION ALL BY NAME
SELECT
  snapshot_date, store, country, list_key, list_kind, COALESCE(all_games, FALSE) AS all_games,
  list_len, rank, app_id, bundle_id, name, source
FROM raw_mr_rank_extra
WHERE rank IS NOT NULL AND app_id IS NOT NULL AND rank <= COALESCE(list_len, rank)
