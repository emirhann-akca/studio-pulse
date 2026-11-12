-- Weekly mechanic share per country in the trend list (default: top free Games, 6014):
--   share = sum over the week's days of charted apps tagged m / sum of charted rows.
-- Tags: primary and secondary mechanics of fct_mr_app_tags. tagged_share says how much of
-- the list has a profile at all (an untagged app counts in the denominator only).
WITH tl AS (
  SELECT COALESCE(MAX(value), 'topfreeapplications:6014') AS list
  FROM raw_mr_config WHERE key = 'trend_list'
), fs AS (
  SELECT app_id, MIN(snapshot_date) AS first_seen FROM stg_mr_chart GROUP BY 1
), r AS (
  SELECT c.snapshot_date, CAST(date_trunc('week', c.snapshot_date) AS DATE) AS week,
         c.country, c.app_id, fs.first_seen
  FROM stg_mr_chart AS c
  JOIN tl ON c.chart || ':' || c.genre_id = tl.list
  JOIN fs USING (app_id)
), tags AS (
  SELECT DISTINCT app_key, value AS mechanic
  FROM fct_mr_app_tags WHERE dimension IN ('mechanic_primary', 'mechanic')
), tot AS (
  SELECT week, country, COUNT(DISTINCT snapshot_date) AS days, COUNT(*) AS row_days,
         COUNT(*) FILTER (WHERE 'apple:' || app_id IN (SELECT app_key FROM tags)) AS tagged_row_days
  FROM r GROUP BY 1, 2
), hit AS (
  SELECT r.week, r.country, t.mechanic, COUNT(*) AS app_days, COUNT(DISTINCT r.app_id) AS n_apps,
         COUNT(DISTINCT r.app_id) FILTER (
           WHERE r.first_seen >= r.week AND r.first_seen < r.week + 7
             AND r.first_seen > (SELECT MIN(snapshot_date) FROM stg_mr_chart)
         ) AS new_entries
  FROM r JOIN tags AS t ON t.app_key = 'apple:' || r.app_id
  GROUP BY 1, 2, 3
), mechs AS (SELECT DISTINCT mechanic FROM tags)
SELECT
  tot.week, tot.country, mechs.mechanic, tot.days, tot.row_days,
  COALESCE(hit.app_days, 0) AS app_days, COALESCE(hit.n_apps, 0) AS n_apps,
  COALESCE(hit.new_entries, 0) AS new_entries,
  COALESCE(hit.app_days, 0) / tot.row_days::DOUBLE AS share,
  tot.tagged_row_days / tot.row_days::DOUBLE AS tagged_share
FROM tot CROSS JOIN mechs
LEFT JOIN hit ON hit.week = tot.week AND hit.country = tot.country AND hit.mechanic = mechs.mechanic
