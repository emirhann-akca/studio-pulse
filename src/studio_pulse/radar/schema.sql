-- Market radar raw tables (docs/RADAR.md). One snapshot per day; a re-run of the same day
-- replaces that day's slice. Personal contact fields of store listings (developer e-mail,
-- address, privacy policy URL) are never stored.

CREATE TABLE IF NOT EXISTS raw_mr_chart (
  snapshot_date DATE, fetched_at TIMESTAMP, store VARCHAR, country VARCHAR, chart VARCHAR,
  genre_id VARCHAR, requested_limit INTEGER, n_rows INTEGER, rank INTEGER, app_id VARCHAR,
  bundle_id VARCHAR, name VARCHAR, developer VARCHAR, developer_id VARCHAR,
  release_date_feed DATE, category_id VARCHAR, feed_updated TIMESTAMP, source_url VARCHAR,
  payload_sha256 VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_mr_app (
  snapshot_date DATE, fetched_at TIMESTAMP, store VARCHAR, country VARCHAR, app_id VARCHAR,
  bundle_id VARCHAR, name VARCHAR, developer VARCHAR, developer_id VARCHAR,
  release_date DATE, current_version_date DATE, version VARCHAR,
  primary_genre VARCHAR, genres VARCHAR, store_tags VARCHAR,
  rating_count BIGINT, rating_avg DOUBLE, rating_count_cur_ver BIGINT,
  installs_bucket VARCHAR, min_installs BIGINT, real_installs BIGINT, histogram VARCHAR,
  price DOUBLE, currency VARCHAR, offers_iap BOOLEAN, iap_range VARCHAR, contains_ads BOOLEAN,
  description VARCHAR, release_notes VARCHAR, summary VARCHAR, screenshot_urls VARCHAR,
  video_url VARCHAR, store_url VARCHAR, source VARCHAR, meta_sha256 VARCHAR
);

-- iOS app -> Play package. status: mapped | not_found | manual_none
CREATE TABLE IF NOT EXISTS raw_mr_xref (
  apple_id VARCHAR, bundle_id VARCHAR, play_id VARCHAR, method VARCHAR, status VARCHAR,
  verified_on DATE
);

CREATE TABLE IF NOT EXISTS raw_mr_llm (
  app_key VARCHAR, analyzed_at TIMESTAMP, provider VARCHAR, model VARCHAR,
  prompt_version VARCHAR, input_sha256 VARCHAR, output VARCHAR, valid BOOLEAN,
  rejected_claims VARCHAR, cost_usd DOUBLE, cached BOOLEAN
);

-- Validated claims, one row per (profile, dimension, value). source: llm | keyword | store
CREATE TABLE IF NOT EXISTS raw_mr_profile (
  app_key VARCHAR, profile_id VARCHAR, source VARCHAR, analyzed_at TIMESTAMP,
  input_sha256 VARCHAR, dimension VARCHAR, value VARCHAR, field VARCHAR, quote VARCHAR,
  visual BOOLEAN
);

CREATE TABLE IF NOT EXISTS raw_mr_request (
  run_id VARCHAR, ts TIMESTAMP, snapshot_date DATE, host VARCHAR, path VARCHAR,
  purpose VARCHAR, status INTEGER, bytes BIGINT, cached BOOLEAN, ms DOUBLE, error VARCHAR,
  note VARCHAR
);
ALTER TABLE raw_mr_request ADD COLUMN IF NOT EXISTS note VARCHAR;

-- Every Apple rating-count read of a day (batched Lookup, rechecks, publisher catalogue).
-- Single reads are not reliable (Apple answers from caches of different ages: UnGodly read
-- 0 then 130 within minutes, Foodie Pop! 1,846 vs 1,760); raw_mr_app keeps the maximum.
CREATE TABLE IF NOT EXISTS raw_mr_lookup_read (
  snapshot_date DATE, run_id VARCHAR, ts TIMESTAMP, country VARCHAR, app_id VARCHAR,
  source VARCHAR, rating_count BIGINT
);

-- Scoring parameters (key -> number) and settings (key -> text) of the latest run.
CREATE TABLE IF NOT EXISTS raw_mr_params (key VARCHAR, value DOUBLE);
CREATE TABLE IF NOT EXISTS raw_mr_config (key VARCHAR, value VARCHAR);

CREATE TABLE IF NOT EXISTS raw_mr_run (
  run_id VARCHAR, snapshot_date DATE, started_at TIMESTAMP, finished_at TIMESTAMP,
  status VARCHAR, requests INTEGER, llm_cost_usd DOUBLE, summary VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_mr_check (
  run_id VARCHAR, checked_at TIMESTAMP, name VARCHAR, severity VARCHAR, status VARCHAR,
  failing_rows INTEGER, message VARCHAR
);
