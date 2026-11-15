-- Competitor creative library (radar/creatives). Rows hold metadata and links only: no video,
-- image or page of a competitor is downloaded or stored.

-- One row per creative and source (google_atc, meta, tiktok, youtube). `creative_key` is
-- '<source letter>-<id>' (g-CR..., m-<ad id>, t-<ad id>, y-<YouTube id>). A weekly Google
-- refresh replaces the google_atc rows; Meta and TikTok rows are upserted; YouTube rows are
-- added by hand (`pulse radar creatives add`).
CREATE TABLE IF NOT EXISTS mr_creative (
  creative_key VARCHAR, source VARCHAR, creative_id VARCHAR, advertiser_key VARCHAR,
  advertiser_id VARCHAR, advertiser_name VARCHAR, genre VARCHAR, format VARCHAR,
  regions VARCHAR[], regions_n INTEGER, first_shown DATE, last_shown DATE,
  times_shown_min BIGINT, times_shown_regions INTEGER, eu_reach BIGINT, reach_bucket VARCHAR,
  platforms VARCHAR[], languages VARCHAR[], ad_text VARCHAR, ad_title VARCHAR,
  youtube_id VARCHAR, note VARCHAR, extract_date DATE, updated_at TIMESTAMP
);

-- Tags per creative: the newest row per creative_key counts. tag_source: llm_video (Gemini
-- watched the public YouTube video), llm_text (ad text through the LLM, quotes checked),
-- keyword (ad text, whole-word keywords), metadata (format and dates only).
CREATE TABLE IF NOT EXISTS mr_creative_tag (
  creative_key VARCHAR, tag_source VARCHAR, hook VARCHAR, hook_confidence DOUBLE,
  ai_likelihood VARCHAR, ai_signals VARCHAR[], orientation VARCHAR, duration VARCHAR,
  mechanic VARCHAR, cta VARCHAR, language VARCHAR, kind VARCHAR, why_tr VARCHAR,
  why_en VARCHAR, evidence VARCHAR, rejected VARCHAR, input_hash VARCHAR,
  prompt_version VARCHAR, model VARCHAR, cost_usd DOUBLE, tagged_at TIMESTAMP
);

-- The advertisers the library follows (config, seed list, radar charts) and the Google Ads
-- Transparency advertiser ids each one resolved to.
CREATE TABLE IF NOT EXISTS mr_creative_advertiser (
  advertiser_key VARCHAR, name VARCHAR, genre VARCHAR, origin VARCHAR,
  google_ids VARCHAR[], google_names VARCHAR[], meta_page_ids VARCHAR[],
  resolved_at TIMESTAMP
);

-- Every refresh of every source, with the bytes BigQuery estimated and billed.
CREATE TABLE IF NOT EXISTS mr_creative_run (
  run_at TIMESTAMP, source VARCHAR, status VARCHAR, estimated_bytes BIGINT,
  billed_bytes BIGINT, cap_bytes BIGINT, rows BIGINT, cost_usd DOUBLE, message VARCHAR
);
