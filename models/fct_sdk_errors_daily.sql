-- Errors per session (SDK pulse_error, managed exceptions) and crashed sessions
-- (pulse_exit at the next launch, fatal errors) per app and day.
SELECT
    app_id,
    date,
    COUNT(*) AS sessions,
    SUM(errors) AS errors,
    COUNT(*) FILTER (WHERE errors > 0) AS sessions_with_error,
    CAST(SUM(errors) AS DOUBLE) / NULLIF(COUNT(*), 0) AS errors_per_session,
    COUNT(*) FILTER (WHERE crashed) AS crashed_sessions,
    1 - CAST(COUNT(*) FILTER (WHERE crashed) AS DOUBLE) / NULLIF(COUNT(*), 0) AS crash_free_sessions
FROM stg_sdk_sessions
GROUP BY 1, 2
ORDER BY 1, 2
