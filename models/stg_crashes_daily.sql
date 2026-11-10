-- Crashlytics export per app, platform and day (latest load wins).
SELECT app_id, COALESCE(platform, 'UNKNOWN') AS platform, date,
       fatal_events, nonfatal_events, crashed_installs, fatal_issues
FROM raw_crashlytics_daily
QUALIFY row_number() OVER (
    PARTITION BY app_id, platform, date ORDER BY _loaded_at DESC, _source
) = 1
