-- Breakout score per day and charted Apple app (docs/RADAR.md, research report 2.3):
--   M = w_vel*s_vel + w_climb*s_climb + w_rank*s_rank_best + w_spread*s_spread + w_new*s_new
-- with gates (age <= 60 days, listed today, and a signal: new entry, climb >= 1, ratings
-- >= 10/day or installs >= 1,000/day) and classes breakout (M >= 0.35), watch (0.20-0.35),
-- late_riser (60 < age <= 120 and M >= 0.35). Weights, saturation points and thresholds
-- come from raw_mr_params (defaults below equal the report's).
WITH p AS (
  SELECT
    COALESCE(MAX(value) FILTER (WHERE key = 'w_vel'), 0.40) AS w_vel,
    COALESCE(MAX(value) FILTER (WHERE key = 'w_climb'), 0.25) AS w_climb,
    COALESCE(MAX(value) FILTER (WHERE key = 'w_rank'), 0.15) AS w_rank,
    COALESCE(MAX(value) FILTER (WHERE key = 'w_spread'), 0.10) AS w_spread,
    COALESCE(MAX(value) FILTER (WHERE key = 'w_new'), 0.10) AS w_new,
    COALESCE(MAX(value) FILTER (WHERE key = 'rv_saturation'), 200) AS rv_sat,
    COALESCE(MAX(value) FILTER (WHERE key = 'iv_saturation'), 50000) AS iv_sat,
    COALESCE(MAX(value) FILTER (WHERE key = 'climb_saturation'), 3) AS climb_sat,
    COALESCE(MAX(value) FILTER (WHERE key = 'spread_saturation'), 4) AS spread_sat,
    CAST(COALESCE(MAX(value) FILTER (WHERE key = 'climb_days'), 7) AS INTEGER) AS climb_days,
    CAST(COALESCE(MAX(value) FILTER (WHERE key = 'spread_days'), 7) AS INTEGER) AS spread_days,
    CAST(COALESCE(MAX(value) FILTER (WHERE key = 'new_days'), 7) AS INTEGER) AS new_days,
    CAST(COALESCE(MAX(value) FILTER (WHERE key = 'new_lookback_days'), 30) AS INTEGER) AS new_lookback,
    COALESCE(MAX(value) FILTER (WHERE key = 'age_max'), 60) AS age_max,
    COALESCE(MAX(value) FILTER (WHERE key = 'late_age_max'), 120) AS late_age_max,
    COALESCE(MAX(value) FILTER (WHERE key = 'breakout_min'), 0.35) AS breakout_min,
    COALESCE(MAX(value) FILTER (WHERE key = 'watch_min'), 0.20) AS watch_min,
    COALESCE(MAX(value) FILTER (WHERE key = 'gate_climb'), 1) AS gate_climb,
    COALESCE(MAX(value) FILTER (WHERE key = 'gate_rv'), 10) AS gate_rv,
    COALESCE(MAX(value) FILTER (WHERE key = 'gate_iv'), 1000) AS gate_iv
  FROM raw_mr_params
), listed AS (
  SELECT
    c.snapshot_date AS t, c.app_id, c.country, c.list_key, c.list_len, c.rank,
    1 - ln(c.rank) / ln(c.list_len + 1) AS s_rank,
    CASE
      WHEN ld.list_key IS NULL THEN NULL
      ELSE log2(COALESCE(prev.rank, ld.list_len + 1)::DOUBLE / c.rank)
    END AS climb
  FROM stg_mr_chart AS c
  CROSS JOIN p
  LEFT JOIN stg_mr_list_days AS ld
    ON ld.list_key = c.list_key AND ld.snapshot_date = c.snapshot_date - p.climb_days
  LEFT JOIN stg_mr_chart AS prev
    ON prev.list_key = c.list_key AND prev.snapshot_date = c.snapshot_date - p.climb_days
   AND prev.app_id = c.app_id
), best AS (
  SELECT t, app_id, list_key AS best_list, country AS best_country, rank AS best_rank,
         list_len AS best_list_len, s_rank AS s_rank_best
  FROM listed
  QUALIFY ROW_NUMBER() OVER (PARTITION BY t, app_id ORDER BY s_rank DESC, list_key) = 1
), climbs AS (
  SELECT t, app_id, COUNT(*) AS n_lists, MAX(climb) AS climb_max
  FROM listed GROUP BY 1, 2
), climb_at AS (
  SELECT t, app_id, list_key AS climb_list
  FROM listed WHERE climb IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY t, app_id ORDER BY climb DESC, list_key) = 1
), fs AS (
  SELECT app_id, MIN(snapshot_date) AS first_seen FROM stg_mr_chart GROUP BY 1
), fsk AS (
  SELECT f.app_id, f.first_seen, COUNT(ld.snapshot_date) > 0 AS first_seen_known
  FROM fs AS f
  CROSS JOIN p
  JOIN stg_mr_chart AS c ON c.app_id = f.app_id AND c.snapshot_date = f.first_seen
  LEFT JOIN stg_mr_list_days AS ld
    ON ld.list_key = c.list_key AND ld.snapshot_date < f.first_seen
   AND ld.snapshot_date >= f.first_seen - p.new_lookback
  GROUP BY 1, 2
), rel AS (
  SELECT app_id, MIN(release_date) AS release_lookup
  FROM raw_mr_app WHERE store = 'apple' GROUP BY 1
), names AS (
  SELECT app_id, name, developer, bundle_id, MIN(release_date_feed) OVER (PARTITION BY app_id) AS release_feed
  FROM stg_mr_chart
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC, fetched_at DESC, list_key) = 1
), spread AS (
  SELECT d.t, c.app_id, COUNT(DISTINCT c.country) AS spread,
         list_sort(list(DISTINCT c.country)) AS spread_countries
  FROM (SELECT DISTINCT t FROM listed) AS d
  CROSS JOIN p
  JOIN stg_mr_chart AS c ON c.snapshot_date BETWEEN d.t - (p.spread_days - 1) AND d.t
  GROUP BY 1, 2
), rv AS (
  SELECT snapshot_date AS t, app_id, MAX(rv) AS rv_max, BOOL_OR(rating_reset) AS rating_reset_any
  FROM stg_mr_rating_daily GROUP BY 1, 2
), rv_at AS (
  SELECT snapshot_date AS t, app_id, country AS rv_country, rv_method, rating_count AS rv_rating_count
  FROM stg_mr_rating_daily WHERE rv IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY snapshot_date, app_id ORDER BY rv DESC, country) = 1
), xr AS (
  SELECT apple_id, play_id FROM raw_mr_xref WHERE status = 'mapped'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY apple_id ORDER BY verified_on DESC, play_id) = 1
), inst AS (
  SELECT xr.apple_id AS app_id, i.snapshot_date AS t, i.play_id, i.installs_bucket,
         i.min_installs, i.real_installs, i.play_released, i.iv, i.iv_method, i.bucket_up,
         i.window_days AS iv_window_days
  FROM stg_mr_install_daily AS i JOIN xr ON xr.play_id = i.play_id
), base AS (
  SELECT
    b.t AS snapshot_date, b.app_id, n.name, n.developer, n.bundle_id,
    b.best_list, b.best_country, b.best_rank, b.best_list_len, b.s_rank_best,
    cl.n_lists, cl.climb_max, ca.climb_list,
    fsk.first_seen, fsk.first_seen_known,
    rel.release_lookup, n.release_feed,
    COALESCE(rel.release_lookup, n.release_feed) AS release_date,
    CASE WHEN rel.release_lookup IS NOT NULL THEN 'lookup'
         WHEN n.release_feed IS NOT NULL THEN 'feed' END AS release_source,
    date_diff('day', LEAST(COALESCE(rel.release_lookup, n.release_feed, fsk.first_seen), fsk.first_seen), b.t) AS age_days,
    COALESCE(rel.release_lookup, n.release_feed) > b.t AS is_preorder,
    sp.spread, sp.spread_countries,
    rv.rv_max, ra.rv_country, ra.rv_method, ra.rv_rating_count,
    COALESCE(rv.rating_reset_any, FALSE) AS rating_reset_any,
    i.play_id, i.installs_bucket, i.min_installs, i.real_installs, i.play_released,
    i.iv, i.iv_method, i.iv_window_days, COALESCE(i.bucket_up, FALSE) AS bucket_up
  FROM best AS b
  JOIN climbs AS cl USING (t, app_id)
  LEFT JOIN climb_at AS ca USING (t, app_id)
  JOIN fsk ON fsk.app_id = b.app_id
  JOIN names AS n ON n.app_id = b.app_id
  LEFT JOIN rel ON rel.app_id = b.app_id
  LEFT JOIN spread AS sp USING (t, app_id)
  LEFT JOIN rv USING (t, app_id)
  LEFT JOIN rv_at AS ra USING (t, app_id)
  LEFT JOIN inst AS i USING (t, app_id)
), scored AS (
  SELECT
    base.*,
    CASE
      WHEN first_seen_known AND date_diff('day', first_seen, snapshot_date) <= p.new_days THEN 1
      WHEN release_source = 'lookup' AND release_date <= snapshot_date
       AND date_diff('day', release_date, snapshot_date) <= p.new_days THEN 1
      ELSE 0
    END AS s_new,
    GREATEST(
      COALESCE(LEAST(GREATEST(log10(1 + rv_max) / log10(1 + p.rv_sat), 0), 1), 0),
      COALESCE(LEAST(GREATEST(log10(1 + iv) / log10(1 + p.iv_sat), 0), 1), 0)
    ) AS s_vel,
    COALESCE(LEAST(GREATEST(climb_max / p.climb_sat, 0), 1), 0) AS s_climb,
    COALESCE(LEAST(GREATEST((spread - 1) / p.spread_sat, 0), 1), 0) AS s_spread,
    p.w_vel, p.w_climb, p.w_rank, p.w_spread, p.w_new, p.age_max, p.late_age_max,
    p.breakout_min, p.watch_min, p.gate_climb, p.gate_rv, p.gate_iv
  FROM base CROSS JOIN p
), m AS (
  SELECT
    scored.*,
    w_vel * s_vel + w_climb * s_climb + w_rank * s_rank_best + w_spread * s_spread
      + w_new * s_new AS momentum,
    age_days <= age_max AS gate_age,
    TRUE AS gate_listed,
    (s_new = 1 OR COALESCE(climb_max, 0) >= gate_climb OR COALESCE(rv_max, 0) >= gate_rv
      OR COALESCE(iv, 0) >= gate_iv) AS gate_signal
  FROM scored
)
SELECT
  snapshot_date, app_id, 'apple:' || app_id AS app_key, name, developer, bundle_id,
  best_list, best_country, best_rank, best_list_len, n_lists, climb_max, climb_list,
  first_seen, first_seen_known, release_date, release_source, release_lookup, release_feed,
  age_days, is_preorder, spread, spread_countries,
  rv_max, rv_country, rv_method, rv_rating_count, rating_reset_any,
  play_id, installs_bucket, min_installs, real_installs, play_released, iv, iv_method, iv_window_days, bucket_up,
  s_vel, s_climb, s_rank_best, s_spread, s_new, momentum,
  gate_age, gate_listed, gate_signal,
  CASE
    WHEN gate_listed AND gate_signal AND gate_age AND momentum >= breakout_min THEN 'breakout'
    WHEN gate_listed AND gate_signal AND gate_age AND momentum >= watch_min THEN 'watch'
    WHEN gate_listed AND gate_signal AND age_days > age_max AND age_days <= late_age_max
     AND momentum >= breakout_min THEN 'late_riser'
  END AS radar_class
FROM m
