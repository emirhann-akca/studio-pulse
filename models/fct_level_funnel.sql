-- Level funnel for puzzle games.
--   starts / completes / fails   event counts over the loaded window
--   win_rate                     completes / (completes + fails)
--   attempts_per_completion      starts / completes
--   users_reached                users whose highest started level is >= level
--   churn_rate                   users who stopped at this level (highest
--                                level = this one and no activity in the last
--                                churn_window_days) / users_reached
WITH ev AS (
    SELECT app_id, level,
           SUM(CASE WHEN action = 'start' THEN events ELSE 0 END) AS starts,
           SUM(CASE WHEN action = 'complete' THEN events ELSE 0 END) AS completes,
           SUM(CASE WHEN action = 'fail' THEN events ELSE 0 END) AS fails
    FROM raw_level_daily
    WHERE level IS NOT NULL
    GROUP BY 1, 2
),
mx AS (
    SELECT app_id, level, users_max_here, users_churned, as_of_date, churn_window_days
    FROM raw_level_max
    WHERE level IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY app_id, level ORDER BY _loaded_at DESC) = 1
),
reach AS (
    SELECT *,
           SUM(users_max_here) OVER (
               PARTITION BY app_id ORDER BY level DESC ROWS UNBOUNDED PRECEDING
           ) AS users_reached
    FROM mx
)
SELECT
    COALESCE(ev.app_id, reach.app_id) AS app_id,
    COALESCE(ev.level, reach.level) AS level,
    COALESCE(ev.starts, 0) AS starts,
    COALESCE(ev.completes, 0) AS completes,
    COALESCE(ev.fails, 0) AS fails,
    CAST(ev.completes AS DOUBLE) / NULLIF(ev.completes + ev.fails, 0) AS win_rate,
    CAST(ev.starts AS DOUBLE) / NULLIF(ev.completes, 0) AS attempts_per_completion,
    reach.users_reached,
    reach.users_max_here,
    reach.users_churned,
    CAST(reach.users_churned AS DOUBLE) / NULLIF(reach.users_reached, 0) AS churn_rate,
    reach.churn_window_days
FROM ev
FULL OUTER JOIN reach ON reach.app_id = ev.app_id AND reach.level = ev.level
ORDER BY 1, 2
