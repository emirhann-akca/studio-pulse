-- SDK sessions, one row per session of a production build. A session is a
-- `sid` from the SDK, which starts a new one after 30 minutes in the
-- background; a sid whose events are more than 30 minutes apart is split here
-- as well. Length is foreground time (`fg_ms`, the SDK's clock of time in the
-- foreground), capped at 24 h; without fg_ms, the first-to-last event span.
-- crashed: the next launch reported a crash/ANR for this session (pulse_exit
-- prev_sid) or a fatal pulse_error happened in it.
WITH ev AS (
    SELECT app_id, platform, install_id, sid, ts, seq, date, snum, fg_ms, name, params, app_version
    FROM stg_pulse_events
    WHERE is_prod
),
gaps AS (
    SELECT *,
           CASE WHEN ts - lag(ts) OVER w > INTERVAL 30 MINUTE THEN 1 ELSE 0 END AS brk
    FROM ev
    WINDOW w AS (PARTITION BY app_id, install_id, sid ORDER BY ts, seq)
),
parts AS (
    SELECT *,
           SUM(brk) OVER (PARTITION BY app_id, install_id, sid ORDER BY ts, seq
                          ROWS UNBOUNDED PRECEDING) AS part
    FROM gaps
),
exits AS (
    SELECT DISTINCT app_id, install_id, params->>'prev_sid' AS sid
    FROM stg_pulse_events
    WHERE name = 'pulse_exit' AND params->>'reason' IN ('crash', 'crash_native', 'anr')
),
s AS (
    SELECT
        app_id, install_id, sid, part,
        arg_min(platform, ts) AS platform,
        arg_min(date, ts) AS date,
        MIN(ts) AS started_at,
        MAX(ts) AS ended_at,
        MIN(snum) AS snum,
        MAX(fg_ms) AS max_fg_ms,
        MIN(fg_ms) AS min_fg_ms,
        COUNT(*) AS events,
        COUNT(*) FILTER (WHERE name = 'pulse_error') AS errors,
        bool_or(name = 'pulse_error' AND CAST(params->>'fatal' AS BOOLEAN)) AS fatal_error,
        bool_or(name = 'level_start') AS played_levels,
        arg_min(app_version, ts) AS app_version
    FROM parts
    GROUP BY app_id, install_id, sid, part
)
SELECT
    s.app_id,
    s.platform,
    s.install_id,
    s.sid || CASE WHEN s.part > 0 THEN '#' || CAST(s.part AS VARCHAR) ELSE '' END AS session_key,
    s.sid,
    s.date,
    s.started_at,
    s.ended_at,
    s.snum,
    s.app_version,
    s.events,
    s.errors,
    s.played_levels,
    LEAST(
        86400000,
        CASE WHEN s.max_fg_ms IS NOT NULL
             THEN s.max_fg_ms - CASE WHEN s.part = 0 THEN 0 ELSE s.min_fg_ms END
             ELSE epoch_ms(s.ended_at) - epoch_ms(s.started_at) END
    ) AS length_ms,
    COALESCE(s.fatal_error, false) OR (s.part = 0 AND x.sid IS NOT NULL) AS crashed
FROM s
LEFT JOIN exits x ON x.app_id = s.app_id AND x.install_id = s.install_id AND x.sid = s.sid
