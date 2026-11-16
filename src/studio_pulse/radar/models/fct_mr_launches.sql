-- New launches (docs/RADAR.md, "New launches"): one row per day and Apple game released in the
-- launch_days (default 14) calendar days up to that day (age 0 to launch_days - 1) and seen
-- by the radar that day in a watched
-- publisher's catalogue (raw_mr_app source 'publisher'), in a tracked chart (stg_mr_chart),
-- or both. One row per (day, app) whatever the storefronts and sources.
-- release_date: the earliest Lookup release over every storefront and source (a game soft-
-- launched months ago is not new), else the chart feed's date; an app with neither is left
-- out, and so are pre-orders (release after the day) and apps that are not games.
-- found_via, first_seen, best_rank/list/country and countries say what the radar had seen of
-- the game up to that day; name, genre, icon and screenshots (at most 3, Apple's image CDN
-- only) come from the newest Apple row up to that day; tags from fct_mr_app_tags.
WITH p AS (
  SELECT CAST(COALESCE(MAX(value) FILTER (WHERE key = 'launch_days'), 14) AS INTEGER) AS launch_days
  FROM raw_mr_params
), obs AS (
  SELECT DISTINCT snapshot_date, app_id, 'publisher' AS src FROM raw_mr_app
  WHERE store = 'apple' AND source = 'publisher' AND app_id IS NOT NULL
  UNION
  SELECT DISTINCT snapshot_date, app_id, 'chart' AS src FROM stg_mr_chart WHERE store = 'apple'
), days AS (
  SELECT DISTINCT snapshot_date, app_id FROM obs
), rel AS (
  SELECT app_id, MIN(release_date) AS release_lookup
  FROM raw_mr_app WHERE store = 'apple' GROUP BY 1
), feed AS (
  SELECT app_id, MIN(release_date_feed) AS release_feed FROM stg_mr_chart GROUP BY 1
), win AS (
  SELECT d.snapshot_date, d.app_id,
         COALESCE(r.release_lookup, f.release_feed) AS release_date,
         CASE WHEN r.release_lookup IS NOT NULL THEN 'lookup'
              WHEN f.release_feed IS NOT NULL THEN 'feed' END AS release_source
  FROM days AS d
  CROSS JOIN p
  LEFT JOIN rel AS r ON r.app_id = d.app_id
  LEFT JOIN feed AS f ON f.app_id = d.app_id
  WHERE COALESCE(r.release_lookup, f.release_feed) <= d.snapshot_date
    AND COALESCE(r.release_lookup, f.release_feed) > d.snapshot_date - p.launch_days
), seen AS (
  SELECT w.snapshot_date, w.app_id,
         BOOL_OR(o.src = 'publisher') AS via_publisher,
         BOOL_OR(o.src = 'chart') AS via_chart,
         MIN(o.snapshot_date) AS first_seen,
         MIN(o.snapshot_date) FILTER (WHERE o.src = 'chart') AS chart_first_seen,
         BOOL_OR(o.src = 'chart' AND o.snapshot_date = w.snapshot_date) AS listed_today
  FROM win AS w
  JOIN obs AS o ON o.app_id = w.app_id AND o.snapshot_date <= w.snapshot_date
  GROUP BY 1, 2
), ranks AS (
  SELECT w.snapshot_date, w.app_id, c.snapshot_date AS seen_on, c.country, c.list_key,
         c.rank, 1 - ln(c.rank) / ln(c.list_len + 1) AS s_rank,
         (c.genre_id = '6014' OR c.genre_id LIKE '7%') AS games_list
  FROM win AS w
  JOIN stg_mr_chart AS c ON c.app_id = w.app_id AND c.snapshot_date <= w.snapshot_date
   AND c.snapshot_date >= w.release_date
), best AS (
  SELECT snapshot_date, app_id, rank AS best_rank, list_key AS best_list,
         country AS best_country, seen_on AS best_rank_on
  FROM ranks
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY snapshot_date, app_id ORDER BY s_rank DESC, seen_on DESC, list_key
  ) = 1
), ctry AS (
  SELECT snapshot_date, app_id, list_sort(list(DISTINCT country)) AS countries,
         BOOL_OR(games_list) AS in_games_list
  FROM ranks GROUP BY 1, 2
), chart_name AS (
  SELECT w.snapshot_date, w.app_id, c.name, c.developer, c.developer_id
  FROM win AS w
  JOIN stg_mr_chart AS c ON c.app_id = w.app_id AND c.snapshot_date <= w.snapshot_date
  WHERE c.name IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY w.snapshot_date, w.app_id ORDER BY c.snapshot_date DESC, c.fetched_at DESC,
    c.list_key
  ) = 1
), meta AS (
  SELECT w.snapshot_date, w.app_id, a.name, a.developer, a.developer_id, a.primary_genre,
         a.genres, a.icon_url, a.screenshot_urls, a.country AS meta_country
  FROM win AS w
  JOIN raw_mr_app AS a ON a.store = 'apple' AND a.app_id = w.app_id
   AND a.snapshot_date <= w.snapshot_date
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY w.snapshot_date, w.app_id
    ORDER BY a.snapshot_date DESC, CASE a.source WHEN 'lookup' THEN 0 ELSE 1 END,
      a.icon_url IS NULL, a.country, a.fetched_at DESC
  ) = 1
), tags AS (
  SELECT app_key,
         MAX(value) FILTER (WHERE dimension = 'mechanic_primary') AS mechanic_primary,
         list_sort(list(DISTINCT value)) AS mechanics,
         MAX(source) AS tag_source
  FROM fct_mr_app_tags WHERE dimension IN ('mechanic_primary', 'mechanic')
  GROUP BY 1
), xr AS (
  SELECT apple_id, play_id FROM raw_mr_xref WHERE status = 'mapped'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY apple_id ORDER BY verified_on DESC, play_id) = 1
), joined AS (
  SELECT
    w.snapshot_date, w.app_id, 'apple:' || w.app_id AS app_key,
    COALESCE(cn.name, m.name) AS name,
    COALESCE(cn.developer, m.developer) AS developer,
    COALESCE(m.developer_id, cn.developer_id) AS developer_id,
    w.release_date, w.release_source,
    date_diff('day', w.release_date, w.snapshot_date) AS age_days,
    CASE WHEN s.via_publisher AND s.via_chart THEN 'both'
         WHEN s.via_publisher THEN 'publisher' ELSE 'chart' END AS found_via,
    s.first_seen, s.chart_first_seen,
    COALESCE(s.chart_first_seen = w.snapshot_date, FALSE) AS first_chart_entry,
    COALESCE(s.listed_today, FALSE) AS listed_today,
    b.best_rank, b.best_list, b.best_country, b.best_rank_on,
    COALESCE(ct.countries, []::VARCHAR[]) AS countries,
    m.primary_genre,
    CASE
      WHEN m.primary_genre IS NOT NULL OR m.genres IS NOT NULL
        THEN COALESCE(m.primary_genre = 'Games', FALSE)
          OR COALESCE(m.genres LIKE '%"Games"%', FALSE)
      ELSE COALESCE(ct.in_games_list, FALSE)
    END AS is_game,
    CASE WHEN regexp_full_match(m.icon_url, 'https://is[1-5]-ssl\.mzstatic\.com/[^ "''<>\\`(){}]+')
         THEN m.icon_url END AS icon_url,
    to_json(list_slice(
      [u FOR u IN COALESCE(CAST(json_extract_string(m.screenshot_urls, '$[*]') AS VARCHAR[]), [])
       IF regexp_full_match(u, 'https://is[1-5]-ssl\.mzstatic\.com/[^ "''<>\\`(){}]+')],
      1, 3)) AS screenshot_urls,
    COALESCE(b.best_country, m.meta_country, 'us') AS store_country,
    t.mechanic_primary, COALESCE(t.mechanics, []::VARCHAR[]) AS mechanics, t.tag_source,
    xr.play_id
  FROM win AS w
  JOIN seen AS s USING (snapshot_date, app_id)
  LEFT JOIN best AS b USING (snapshot_date, app_id)
  LEFT JOIN ctry AS ct USING (snapshot_date, app_id)
  LEFT JOIN chart_name AS cn USING (snapshot_date, app_id)
  LEFT JOIN meta AS m USING (snapshot_date, app_id)
  LEFT JOIN tags AS t ON t.app_key = 'apple:' || w.app_id
  LEFT JOIN xr ON xr.apple_id = w.app_id
)
SELECT
  snapshot_date, app_id, app_key, name, developer, developer_id, release_date,
  release_source, age_days, found_via, first_seen, chart_first_seen, first_chart_entry,
  listed_today, best_rank, best_list, best_country, best_rank_on, countries, primary_genre,
  is_game, icon_url, screenshot_urls, store_country,
  CASE WHEN regexp_full_match(app_id, '[0-9]{5,12}') AND regexp_full_match(store_country, '[a-z]{2}')
       THEN 'https://apps.apple.com/' || store_country || '/app/id' || app_id END AS store_url,
  mechanic_primary, mechanics, tag_source, play_id
FROM joined
WHERE is_game
