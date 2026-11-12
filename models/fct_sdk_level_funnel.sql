-- SDK level funnel per app and level (production builds):
--   starts / completes / fails / quits   event counts
--   players_started / players_completed  distinct installs
--   fail_rate                            fails / (completes + fails)
--   attempts_per_completion              starts / completes
--   avg_attempts_to_win                  mean `attempt` of each player's first win
--   quit_inferred                        sessions whose last level event is a start
--                                        with no outcome (app killed or abandoned)
--   players_reached                      installs whose highest started level >= level
--   churned_here                         churned installs whose highest level is this one:
--                                        the inferred quit level
--   churn_rate                           churned_here / players_reached
WITH ev AS (
    SELECT app_id, install_id, sid, ts, seq, name,
           CAST(params->>'level' AS INTEGER) AS level,
           CAST(params->>'attempt' AS INTEGER) AS attempt,
           CAST(params->>'duration_ms' AS BIGINT) AS duration_ms
    FROM stg_pulse_events
    WHERE is_prod AND name IN ('level_start', 'level_complete', 'level_fail', 'level_quit')
      AND params->>'level' IS NOT NULL
),
per AS (
    SELECT app_id, level,
           COUNT(*) FILTER (WHERE name = 'level_start') AS starts,
           COUNT(*) FILTER (WHERE name = 'level_complete') AS completes,
           COUNT(*) FILTER (WHERE name = 'level_fail') AS fails,
           COUNT(*) FILTER (WHERE name = 'level_quit') AS quits,
           COUNT(DISTINCT install_id) FILTER (WHERE name = 'level_start') AS players_started,
           COUNT(DISTINCT install_id) FILTER (WHERE name = 'level_complete') AS players_completed,
           median(duration_ms) FILTER (WHERE name = 'level_complete') AS median_win_ms
    FROM ev GROUP BY 1, 2
),
first_win AS (
    SELECT app_id, level, install_id, MIN(attempt) AS attempts_to_win
    FROM ev WHERE name = 'level_complete' GROUP BY ALL
),
wins AS (
    SELECT app_id, level, AVG(attempts_to_win) AS avg_attempts_to_win FROM first_win GROUP BY 1, 2
),
last_ev AS (
    SELECT app_id, level, name,
           row_number() OVER (PARTITION BY app_id, install_id, sid ORDER BY ts DESC, seq DESC) AS rk
    FROM ev
),
inferred AS (
    SELECT app_id, level, COUNT(*) AS quit_inferred
    FROM last_ev WHERE rk = 1 AND name = 'level_start' GROUP BY 1, 2
),
stop AS (
    SELECT app_id, max_level_started AS level, COUNT(*) AS players_max_here,
           COUNT(*) FILTER (WHERE churned) AS churned_here
    FROM stg_sdk_installs WHERE max_level_started IS NOT NULL GROUP BY 1, 2
),
reach AS (
    SELECT *, SUM(players_max_here) OVER (
               PARTITION BY app_id ORDER BY level DESC ROWS UNBOUNDED PRECEDING) AS players_reached
    FROM stop
)
SELECT
    p.app_id,
    p.level,
    p.starts,
    p.completes,
    p.fails,
    p.quits,
    p.players_started,
    p.players_completed,
    CAST(p.fails AS DOUBLE) / NULLIF(p.completes + p.fails, 0) AS fail_rate,
    CAST(p.starts AS DOUBLE) / NULLIF(p.completes, 0) AS attempts_per_completion,
    w.avg_attempts_to_win,
    p.median_win_ms,
    COALESCE(i.quit_inferred, 0) AS quit_inferred,
    r.players_reached,
    COALESCE(r.churned_here, 0) AS churned_here,
    CAST(r.churned_here AS DOUBLE) / NULLIF(r.players_reached, 0) AS churn_rate
FROM per p
LEFT JOIN wins w USING (app_id, level)
LEFT JOIN inferred i USING (app_id, level)
LEFT JOIN reach r USING (app_id, level)
ORDER BY 1, 2
