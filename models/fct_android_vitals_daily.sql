-- Android vitals per app and day (Play Developer Reporting API, DAILY in
-- America/Los_Angeles). Play's bad-behaviour thresholds: user-perceived crash
-- rate 1.09%, user-perceived ANR rate 0.47%.
WITH v AS (
    SELECT * FROM raw_android_vitals
    QUALIFY row_number() OVER (PARTITION BY app_id, date, metric_set, metric
                               ORDER BY _loaded_at DESC, _source) = 1
)
SELECT
    app_id, date,
    MAX(CASE WHEN metric = 'crashRate' THEN value END) AS crash_rate,
    MAX(CASE WHEN metric = 'userPerceivedCrashRate' THEN value END) AS user_perceived_crash_rate,
    MAX(CASE WHEN metric = 'anrRate' THEN value END) AS anr_rate,
    MAX(CASE WHEN metric = 'userPerceivedAnrRate' THEN value END) AS user_perceived_anr_rate,
    MAX(CASE WHEN metric = 'distinctUsers' AND metric_set = 'anrRateMetricSet' THEN value END) AS distinct_users
FROM v
GROUP BY 1, 2
ORDER BY 1, 2
