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
    ft_src            VARCHAR,   -- 'ftm' FrameTimingManager | 'dt' unscaledDeltaTime
    cpu_main_p95_ms   DOUBLE,
    gpu_p95_ms        DOUBLE,
    mem_src           VARCHAR,   -- 'recorder' ProfilerRecorder | 'profiler_api'
    gc_reserved_mb    DOUBLE,
    sys_used_mb       DOUBLE,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Warehouses created before the ProfilerRecorder-based sampler.
ALTER TABLE raw_perf_windows ADD COLUMN IF NOT EXISTS ft_src VARCHAR;
ALTER TABLE raw_perf_windows ADD COLUMN IF NOT EXISTS cpu_main_p95_ms DOUBLE;
ALTER TABLE raw_perf_windows ADD COLUMN IF NOT EXISTS gpu_p95_ms DOUBLE;
ALTER TABLE raw_perf_windows ADD COLUMN IF NOT EXISTS mem_src VARCHAR;
ALTER TABLE raw_perf_windows ADD COLUMN IF NOT EXISTS gc_reserved_mb DOUBLE;
ALTER TABLE raw_perf_windows ADD COLUMN IF NOT EXISTS sys_used_mb DOUBLE;

-- Frames over the display's frame budget (Flutter package; optional).
ALTER TABLE raw_perf_windows ADD COLUMN IF NOT EXISTS janky_frames BIGINT;

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

-- GA4 install cohorts split by the user's first-touch traffic source
-- (traffic_source.source / medium / name). Aggregated in the source; no ids.
CREATE TABLE IF NOT EXISTS raw_ga4_campaign_cohorts (
    app_id            VARCHAR NOT NULL,
    platform          VARCHAR,
    cohort_date       DATE NOT NULL,
    day_n             INTEGER NOT NULL,
    ts_source         VARCHAR,
    ts_medium         VARCHAR,
    ts_campaign       VARCHAR,
    users             BIGINT,
    iap_revenue_usd   DOUBLE,
    ad_revenue_usd    DOUBLE,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Ad-network spend (connectors/ua): one row per date x campaign x ad x country.
-- Amounts in the account currency; network_installs as the network reports them.
CREATE TABLE IF NOT EXISTS raw_ua_spend (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    network           VARCHAR NOT NULL,
    account_id        VARCHAR,
    campaign_id       VARCHAR,
    campaign_name     VARCHAR,
    adset_id          VARCHAR,
    adset_name        VARCHAR,
    ad_id             VARCHAR,
    ad_name           VARCHAR,
    creative_id       VARCHAR,   -- from the ad name (creative_parser), NULL when not one of ours
    creative_concept  VARCHAR,
    country           VARCHAR,   -- ISO 3166-1 alpha-2, NULL when the report has no country split
    platform          VARCHAR,   -- ANDROID | IOS | NULL
    spend             DOUBLE,
    currency          VARCHAR,
    impressions       BIGINT,
    clicks            BIGINT,
    network_installs  DOUBLE,
    _file             VARCHAR,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- How each app's UA networks are attributed (written from the config at ingest).
CREATE TABLE IF NOT EXISTS meta_ua_networks (
    app_id            VARCHAR NOT NULL,
    network           VARCHAR NOT NULL,
    attribution       VARCHAR NOT NULL,   -- ga4 | network_installs
    ga4_sources       VARCHAR[]           -- GA4 traffic_source.source values of this network
);

-- Experiment sufficient statistics per variant, computed inside the source
-- (BigQuery or DuckDB): no user ids. y = metric in the analysis window after
-- first exposure, x = the same metric in the pre-period (CUPED covariate).
CREATE TABLE IF NOT EXISTS raw_experiment_arms (
    app_id            VARCHAR NOT NULL,
    experiment_id     VARCHAR NOT NULL,
    variant           VARCHAR NOT NULL,
    users             BIGINT,
    conversions       BIGINT,
    y_sum             DOUBLE,
    y_sumsq           DOUBLE,
    x_sum             DOUBLE,
    x_sumsq           DOUBLE,
    xy_sum            DOUBLE,
    first_exposure    DATE,
    last_exposure     DATE,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Experiment settings from the config (control, expected split, alpha).
CREATE TABLE IF NOT EXISTS meta_experiments (
    app_id            VARCHAR NOT NULL,
    experiment_id     VARCHAR NOT NULL,
    name              VARCHAR,
    control           VARCHAR,
    split             VARCHAR,   -- JSON {variant: weight}
    alpha             DOUBLE,
    metric            VARCHAR,
    tuning            DOUBLE
);

-- Store reviews: Play Console monthly CSVs (reviews/), the Play Developer API
-- (last 7 days only) and App Store Connect customerReviews. No reviewer names.
CREATE TABLE IF NOT EXISTS raw_store_reviews (
    app_id            VARCHAR NOT NULL,
    store             VARCHAR NOT NULL,
    review_id         VARCHAR NOT NULL,
    submitted_at      TIMESTAMP,
    updated_at        TIMESTAMP,
    date              DATE,
    rating            INTEGER,
    title             VARCHAR,
    body              VARCHAR,
    language          VARCHAR,
    territory         VARCHAR,
    app_version       VARCHAR,
    device            VARCHAR,
    has_reply         BOOLEAN,
    origin            VARCHAR,   -- play_csv | play_api | asc_api
    _file             VARCHAR,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Android vitals (Play Developer Reporting API), one row per day x metric.
CREATE TABLE IF NOT EXISTS raw_android_vitals (
    app_id            VARCHAR NOT NULL,
    date              DATE NOT NULL,
    metric_set        VARCHAR NOT NULL,
    metric            VARCHAR NOT NULL,
    value             DOUBLE,
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
    is_demo           BOOLEAN,
    timezone          VARCHAR DEFAULT 'UTC'   -- the GA4 property's reporting time zone
);

-- Warehouses created before the timezone column existed.
ALTER TABLE meta_apps ADD COLUMN IF NOT EXISTS timezone VARCHAR DEFAULT 'UTC';

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

-- Data-quality results written by `pulse check` / `pulse run` (checks.yaml).
-- One row per check per run; the newest run_id is the current state.
CREATE TABLE IF NOT EXISTS meta_check_results (
    run_id            VARCHAR NOT NULL,
    checked_at        TIMESTAMP NOT NULL,
    name              VARCHAR NOT NULL,
    check_type        VARCHAR,
    table_name        VARCHAR,
    severity          VARCHAR,   -- error | warn
    status            VARCHAR,   -- pass | fail | error (the check itself could not run)
    failing_rows      BIGINT,
    message           VARCHAR,
    sample            VARCHAR    -- up to 5 failing rows as JSON
);

-- Alerts fired by each run, for the UI's alert list and history.
CREATE TABLE IF NOT EXISTS meta_alert_events (
    run_id            VARCHAR NOT NULL,
    fired_at          TIMESTAMP NOT NULL,
    rule              VARCHAR,
    app_id            VARCHAR,
    severity          VARCHAR,
    metric            VARCHAR,
    date              DATE,
    value             DOUBLE,
    baseline          DOUBLE,
    threshold         DOUBLE,
    message           VARCHAR
);

-- Studio Pulse SDK events (contract v2) loaded from the collector by the
-- pulse_sdk source (collector/ingest.py). One row per accepted event; date is
-- the event's day in the app's time zone, ts the collector's skew-corrected
-- time. No IP and no account id (uid) reach the warehouse.
CREATE TABLE IF NOT EXISTS raw_pulse_events (
    app_id            VARCHAR NOT NULL,
    bundle_id         VARCHAR,
    platform          VARCHAR,
    date              DATE NOT NULL,
    event_id          VARCHAR,
    seq               BIGINT,
    name              VARCHAR,
    ts                TIMESTAMP,
    ts_client         TIMESTAMP,
    received_at       TIMESTAMP,
    skew_ms           BIGINT,
    late              BOOLEAN,
    sid               VARCHAR,
    snum              BIGINT,
    fg_ms             BIGINT,
    params            JSON,
    app_version       VARCHAR,
    build             VARCHAR,
    build_env         VARCHAR,
    install_id        VARCHAR,
    install_store     VARCHAR,
    country           VARCHAR,
    device_tier       VARCHAR,
    os                VARCHAR,
    device_model      VARCHAR,
    sdk               VARCHAR,
    engine            VARCHAR,
    locale            VARCHAR,
    consent           VARCHAR,
    child             BOOLEAN,
    schema            VARCHAR,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- Events the collector quarantined, counted per day, reason and event name.
CREATE TABLE IF NOT EXISTS raw_pulse_quarantine (
    app_id            VARCHAR NOT NULL,
    bundle_id         VARCHAR,
    platform          VARCHAR,
    date              DATE,
    reason            VARCHAR,
    name              VARCHAR,
    events            BIGINT,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);

-- The collector's app registry (status new / testing / live, store listing).
CREATE TABLE IF NOT EXISTS raw_pulse_app_registry (
    app_id            VARCHAR NOT NULL,
    bundle_id         VARCHAR,
    platform          VARCHAR,
    display_name      VARCHAR,
    genre             VARCHAR,
    status            VARCHAR,
    first_seen        TIMESTAMP,
    last_seen         TIMESTAMP,
    first_version     VARCHAR,
    last_version      VARCHAR,
    status_changed_at TIMESTAMP,
    store_listed      BOOLEAN,
    store_version     VARCHAR,
    store_url         VARCHAR,
    store_checked_at  TIMESTAMP,
    _source           VARCHAR NOT NULL,
    _loaded_at        TIMESTAMP
);
