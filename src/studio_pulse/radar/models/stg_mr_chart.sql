-- Apple chart rows within the requested list length, one row per day, list and rank
-- (the latest fetch wins). A feed that returns more rows than asked (newfreeapplications
-- ignores limit) keeps only ranks up to the requested length here; raw_mr_chart has all.
-- A games list (genre 6014 or a 7xxx games subgenre) keeps only rows whose own category is
-- Games: newfreeapplications ignores the genre and mixes in apps.
SELECT
  snapshot_date, store, country, chart, genre_id,
  country || ':' || chart || ':' || genre_id AS list_key,
  requested_limit AS list_len, rank, app_id, bundle_id, name, developer, developer_id,
  release_date_feed, feed_updated, fetched_at
FROM raw_mr_chart
WHERE rank <= requested_limit AND app_id IS NOT NULL
  AND (category_id IS NULL OR category_id = '6014'
       OR NOT (genre_id = '6014' OR genre_id LIKE '7%'))
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY snapshot_date, store, country, chart, genre_id, rank ORDER BY fetched_at DESC
) = 1
