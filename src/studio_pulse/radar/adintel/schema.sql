-- Weekly Google Ads Transparency extract (radar/adintel/extract.py). The latest extract only:
-- a refresh replaces raw_mr_adv and raw_mr_adv_region; raw_mr_adintel_run keeps every run with
-- the bytes BigQuery estimated and billed.

CREATE TABLE IF NOT EXISTS raw_mr_adv (
  extract_date DATE, advertiser_id VARCHAR, name VARCHAR, name_norm VARCHAR,
  creatives BIGINT, games_creatives BIGINT, video BIGINT, image BIGINT, text BIGINT,
  active_30d BIGINT, first_shown DATE, last_shown DATE, regions_n INTEGER
);

CREATE TABLE IF NOT EXISTS raw_mr_adv_region (
  extract_date DATE, advertiser_id VARCHAR, region_code VARCHAR, creatives BIGINT,
  first_shown DATE, last_shown DATE
);

CREATE TABLE IF NOT EXISTS raw_mr_adintel_run (
  run_at TIMESTAMP, extract_date DATE, status VARCHAR, estimated_bytes BIGINT,
  billed_bytes BIGINT, cap_bytes BIGINT, advertisers BIGINT, patterns INTEGER, message VARCHAR
);
