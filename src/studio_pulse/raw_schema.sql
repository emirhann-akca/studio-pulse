-- Raw layer: connectors write here, SQL models in models/*.sql read from here.
-- Every table carries app_id (the studio-internal app key from the config) and
-- _source (which connector instance produced the row) so loads are idempotent:
-- a connector deletes its own (app_id, _source[, window]) slice before inserting.

-- GA4 export, aggregated in the source (BigQuery or local DuckDB). No user ids.
CREATE TABLE IF NOT EXISTS raw_ga4_active_users (
    app_id            VARCHAR NOT NULL,
    platform          VARCHAR,
    date              DATE NOT NULL,
    dau               BIGINT,
    wau               BIGINT,
    mau               BIGINT,
    new_users         BIGINT,
    sessions          BIGINT,
    engagement_sec    DOUBLE,
    crashed_users     BIGINT,
    crashed_sessions  BIGINT,
    iap_revenue_usd   DOUBLE,
    ad_revenue_usd    DOUBLE,
    payers            BIGINT,
    history_days      INTEGER,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_ga4_cohort_days (
    app_id            VARCHAR NOT NULL,
    platform          VARCHAR,
    cohort_date       DATE NOT NULL,
    day_n             INTEGER NOT NULL,
    users             BIGINT,
    sessions          BIGINT,
    iap_revenue_usd   DOUBLE,
    ad_revenue_usd    DOUBLE,
    new_payers        BIGINT,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_ga4_event_counts (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    event_name        VARCHAR,
    events            BIGINT,
    users             BIGINT,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Level funnel (puzzle games). action in ('start', 'complete', 'fail').
CREATE TABLE IF NOT EXISTS raw_level_daily (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    level             INTEGER,
    action            VARCHAR,
    events            BIGINT,
    users             BIGINT,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Per level: users whose highest started level is `level`, and how many of
-- them have been inactive for churn_window_days at as_of_date.
CREATE TABLE IF NOT EXISTS raw_level_max (
    app_id            VARCHAR NOT NULL,
    level             INTEGER,
    users_max_here    BIGINT,
    users_churned     BIGINT,
    as_of_date        DATE,
    churn_window_days INTEGER,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Client performance: one row per pulse_perf window (see docs/EVENTS.md).
CREATE TABLE IF NOT EXISTS raw_perf_windows (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    platform          VARCHAR,
    app_version       VARCHAR,
    device_tier       VARCHAR,
    scene             VARCHAR,
    window_s          DOUBLE,
    frames            BIGINT,
    ft_p50_ms         DOUBLE,
    ft_p95_ms         DOUBLE,
    ft_max_ms         DOUBLE,
    slow_frames       BIGINT,
    mem_alloc_mb      DOUBLE,
    mem_reserved_mb   DOUBLE,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- One row per pulse_load event. kind in ('cold_start', 'scene_load').
CREATE TABLE IF NOT EXISTS raw_load_times (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    platform          VARCHAR,
    app_version       VARCHAR,
    device_tier       VARCHAR,
    kind              VARCHAR,
    scene             VARCHAR,
    duration_ms       DOUBLE,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Crashlytics BigQuery export, aggregated per day and platform.
CREATE TABLE IF NOT EXISTS raw_crashlytics_daily (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    platform          VARCHAR,
    fatal_events      BIGINT,
    nonfatal_events   BIGINT,
    crashed_installs  BIGINT,
    fatal_issues      BIGINT,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- RevenueCat scheduled export (Transactions feed). Every received version of a
-- transaction is kept; stg_rc_transactions picks the latest one.
CREATE TABLE IF NOT EXISTS raw_revenuecat_transactions (
    app_id                                VARCHAR NOT NULL,
    store_transaction_id                  VARCHAR,
    original_store_transaction_id         VARCHAR,
    renewal_number                        INTEGER,
    rc_original_app_user_id               VARCHAR,
    product_identifier                    VARCHAR,
    store                                 VARCHAR,
    platform                              VARCHAR,
    country                               VARCHAR,
    start_time                            TIMESTAMP,
    end_time                              TIMESTAMP,
    is_auto_renewable                     BOOLEAN,
    is_trial_period                       BOOLEAN,
    is_trial_conversion                   BOOLEAN,
    is_sandbox                            BOOLEAN,
    ownership_type                        VARCHAR,
    refunded_at                           TIMESTAMP,
    price_in_usd                          DOUBLE,
    purchase_price_in_usd                 DOUBLE,
    purchased_currency                    VARCHAR,
    price_in_purchased_currency           DOUBLE,
    purchase_price_in_purchased_currency  DOUBLE,
    tax_percentage                        DOUBLE,
    commission_percentage                 DOUBLE,
    takehome_percentage                   DOUBLE,
    first_seen_time                       TIMESTAMP,
    updated_at                            TIMESTAMP,
    _file                                 VARCHAR NOT NULL,
    _file_ts                              TIMESTAMP,
    _row                                  BIGINT,
    _source                               VARCHAR NOT NULL,
    _loaded_at                            TIMESTAMP
);

-- AdMob Reporting API (network report), one row per day x AdMob app.
CREATE TABLE IF NOT EXISTS raw_admob_daily (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    admob_app_id      VARCHAR,
    earnings          DOUBLE,
    currency          VARCHAR,
    impressions       BIGINT,
    clicks            BIGINT,
    ad_requests       BIGINT,
    matched_requests  BIGINT,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Store reports (Google Play Console statistics CSV, App Store Connect Sales).
CREATE TABLE IF NOT EXISTS raw_store_installs (
    app_id            VARCHAR NOT NULL,
    store             VARCHAR NOT NULL,
    date              DATE NOT NULL,
    installs          BIGINT,
    uninstalls        BIGINT,
    redownloads       BIGINT,
    updates           BIGINT,
    iap_units         BIGINT,
    proceeds          DOUBLE,
    proceeds_currency VARCHAR,
    _file             VARCHAR,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_store_ratings (
    app_id            VARCHAR NOT NULL,
    store             VARCHAR NOT NULL,
    date              DATE NOT NULL,
    daily_avg_rating  DOUBLE,
    total_avg_rating  DOUBLE,
    _file             VARCHAR,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Fallback FX rates, used only when a source does not deliver USD values.
CREATE TABLE IF NOT EXISTS raw_fx_rates (
    currency          VARCHAR NOT NULL,
    usd_per_unit      DOUBLE NOT NULL,
    valid_from        DATE NOT NULL
);

-- Bookkeeping.
CREATE TABLE IF NOT EXISTS meta_apps (
    app_id            VARCHAR PRIMARY KEY,
    name              VARCHAR,
    genre             VARCHAR,
    platforms         VARCHAR,
    is_demo           BOOLEAN
);

CREATE TABLE IF NOT EXISTS meta_loads (
    app_id            VARCHAR,
    source            VARCHAR,
    table_name        VARCHAR,
    rows_loaded       BIGINT,
    min_date          DATE,
    max_date          DATE,
    loaded_at         TIMESTAMP,
    bytes_billed      BIGINT,
    detail            VARCHAR
);

CREATE TABLE IF NOT EXISTS meta_run (
    key               VARCHAR PRIMARY KEY,
    value             VARCHAR
);
