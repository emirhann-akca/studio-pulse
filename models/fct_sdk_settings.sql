-- Settings players change (setting_change), per app and setting:
--   players               installs with any event in the window
--   changed_players       installs that changed the setting at least once
--   turned_off_players    installs that set it to off (off / 0 / false / mute)
--   off_share             turned_off_players / players
--   off_in_first_session  share of those who did it in their first session
--   median_session_at_off / median_hours_at_off   when (session number, hours since first seen)
WITH sc AS (
    SELECT app_id, install_id, ts, snum,
           params->>'setting' AS setting,
           lower(params->>'to') AS to_value
    FROM stg_pulse_events
    WHERE is_prod AND name = 'setting_change'
),
off AS (
    SELECT app_id, setting, install_id, MIN(ts) AS off_at, arg_min(snum, ts) AS snum
    FROM sc WHERE to_value IN ('off', '0', 'false', 'mute', 'muted')
    GROUP BY 1, 2, 3
),
players AS (
    SELECT app_id, COUNT(*) AS players FROM stg_sdk_installs GROUP BY 1
),
o AS (
    SELECT off.app_id, off.setting,
           COUNT(*) AS turned_off_players,
           AVG(CASE WHEN off.snum = 1 THEN 1.0 ELSE 0.0 END) AS off_in_first_session,
           median(off.snum) AS median_session_at_off,
           median((epoch_ms(off.off_at) - epoch_ms(i.first_seen_at)) / 3600000.0) AS median_hours_at_off
    FROM off JOIN stg_sdk_installs i USING (app_id, install_id)
    GROUP BY 1, 2
),
c AS (
    SELECT app_id, setting, COUNT(DISTINCT install_id) AS changed_players FROM sc GROUP BY 1, 2
)
SELECT
    c.app_id,
    c.setting,
    p.players,
    c.changed_players,
    COALESCE(o.turned_off_players, 0) AS turned_off_players,
    CAST(COALESCE(o.turned_off_players, 0) AS DOUBLE) / NULLIF(p.players, 0) AS off_share,
    o.off_in_first_session,
    o.median_session_at_off,
    o.median_hours_at_off
FROM c
JOIN players p USING (app_id)
LEFT JOIN o USING (app_id, setting)
ORDER BY 1, 2
