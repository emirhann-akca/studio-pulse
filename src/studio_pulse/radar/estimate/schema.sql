-- Market estimates and the watchlist (docs/RADAR.md "Market estimates", "Game history").
-- Created next to the radar's own tables; nothing here is written to the public demo.

-- Games looked up every day even when they are not charted. store: apple | play.
-- A removed game keeps its row with removed_at set (history stays readable).
CREATE TABLE IF NOT EXISTS raw_mr_watch (
  store VARCHAR, app_id VARCHAR, added_at TIMESTAMP, removed_at TIMESTAMP, note VARCHAR
);

-- Google Play install-counter windows (ground truth for the download curves). realInstalls is
-- refreshed irregularly: a window runs between two observed refreshes (the counter changed)
-- at least min_window_days apart. status: ok | implausible (more new ratings than
-- max_ratings_per_install per new install) | too_long | decreased.
CREATE TABLE IF NOT EXISTS raw_mr_est_window (
  computed_on DATE, play_id VARCHAR, apple_ids VARCHAR, t0 TIMESTAMP, t1 TIMESTAMP,
  days DOUBLE, installs0 BIGINT, installs1 BIGINT, ratings0 BIGINT, ratings1 BIGINT,
  rate DOUBLE, status VARCHAR
);

-- Chart exposure of a window: geometric mean rank over the window's observed list days
-- (a day unlisted counts as list length + 1), share of those days the game was listed.
CREATE TABLE IF NOT EXISTS raw_mr_est_exposure (
  computed_on DATE, play_id VARCHAR, t0 TIMESTAMP, country VARCHAR, list_key VARCHAR,
  days_observed INTEGER, days_listed INTEGER, mean_rank DOUBLE
);

-- One curve per (kind, list): status ok | insufficient | no_source. a = log(value at rank 1),
-- b = slope (value ~ rank^-b); for revenue the values are medians of the Monte Carlo draws.
CREATE TABLE IF NOT EXISTS raw_mr_est_fit (
  computed_on DATE, kind VARCHAR, country VARCHAR, list_key VARCHAR, status VARCHAR,
  n_games INTEGER, n_obs INTEGER, min_games INTEGER, a DOUBLE, b DOUBLE, b_low DOUBLE,
  b_high DOUBLE, sigma DOUBLE, rank_min DOUBLE, rank_max DOUBLE, method VARCHAR, note VARCHAR
);

-- Out-of-sample accuracy. scheme: logo (leave one game out) | time (fit on earlier windows,
-- predict later ones) | loo_anchor (leave one revenue anchor out) | holdout_anchor.
-- data: live | synthetic. mape = median |pred - actual| / actual of the band's middle.
CREATE TABLE IF NOT EXISTS raw_mr_est_backtest (
  computed_on DATE, kind VARCHAR, country VARCHAR, list_key VARCHAR, scheme VARCHAR,
  data VARCHAR, n INTEGER, mape DOUBLE, median_abs_log DOUBLE, coverage DOUBLE,
  nominal DOUBLE, note VARCHAR
);

-- Per-game bands of the day. kind: android_installs_ww | ios_downloads_ww | ios_revenue.
-- basis is always 'estimate'; low/high are the configured band quantiles.
CREATE TABLE IF NOT EXISTS raw_mr_est_game (
  snapshot_date DATE, app_id VARCHAR, play_id VARCHAR, country VARCHAR, list_key VARCHAR,
  kind VARCHAR, rank INTEGER, low DOUBLE, mid DOUBLE, high DOUBLE, unit VARCHAR,
  method VARCHAR, n INTEGER, basis VARCHAR
);

-- Ranks from any other chart source (another feed, another store, a licensed export). The
-- estimates read ranks only through stg_mr_rank_input, which is the radar's own Apple charts
-- plus this table, so a source can be added or replaced without touching the calibration.
-- list_kind: free | grossing | paid | other; all_games: the list covers all games (not a genre).
CREATE TABLE IF NOT EXISTS raw_mr_rank_extra (
  snapshot_date DATE, source VARCHAR, store VARCHAR, country VARCHAR, list_key VARCHAR,
  list_kind VARCHAR, all_games BOOLEAN, list_len INTEGER, rank INTEGER, app_id VARCHAR,
  bundle_id VARCHAR, name VARCHAR
);
