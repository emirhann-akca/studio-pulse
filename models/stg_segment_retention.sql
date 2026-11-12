-- Install cohorts by the day-0 value of a GA4 user property (brain B3b), with
-- a day-0 sessions stratum ('1', '2', '3+'; NULL = all users of the value).
SELECT
    app_id,
    cohort_date,
    property,
    value,
    day0_sessions,
    users,
    CASE WHEN observed_days >= 1 THEN d1_users END AS d1_users,
    CASE WHEN observed_days >= 7 THEN d7_users END AS d7_users
FROM raw_ga4_segment_retention
QUALIFY row_number() OVER (
    PARTITION BY app_id, cohort_date, property, value, day0_sessions
    ORDER BY _loaded_at DESC, _source
) = 1
