-- Where players die: level_fail (and level_quit) positions per level on a
-- 10 x 10 grid. pos_space 'norm' positions are already 0-1 of the level;
-- 'world' positions are scaled by the level's observed min/max first.
WITH f AS (
    SELECT app_id, name,
           CAST(params->>'level' AS INTEGER) AS level,
           COALESCE(params->>'pos_space', 'norm') AS pos_space,
           CAST(params->>'pos_x' AS DOUBLE) AS x,
           CAST(params->>'pos_y' AS DOUBLE) AS y
    FROM stg_pulse_events
    WHERE is_prod AND name IN ('level_fail', 'level_quit')
      AND params->>'pos_x' IS NOT NULL AND params->>'pos_y' IS NOT NULL
      AND params->>'level' IS NOT NULL
),
r AS (
    SELECT app_id, level, pos_space, MIN(x) AS x0, MAX(x) AS x1, MIN(y) AS y0, MAX(y) AS y1
    FROM f GROUP BY 1, 2, 3
),
b AS (
    SELECT f.app_id, f.level, f.pos_space, f.name, r.x0, r.x1, r.y0, r.y1,
           CASE WHEN f.pos_space = 'norm' THEN f.x ELSE (f.x - r.x0) / NULLIF(r.x1 - r.x0, 0) END AS nx,
           CASE WHEN f.pos_space = 'norm' THEN f.y ELSE (f.y - r.y0) / NULLIF(r.y1 - r.y0, 0) END AS ny
    FROM f JOIN r USING (app_id, level, pos_space)
)
SELECT
    app_id,
    level,
    pos_space,
    CAST(LEAST(9, GREATEST(0, floor(COALESCE(nx, 0) * 10))) AS INTEGER) AS bin_x,
    CAST(LEAST(9, GREATEST(0, floor(COALESCE(ny, 0) * 10))) AS INTEGER) AS bin_y,
    COUNT(*) FILTER (WHERE name = 'level_fail') AS fails,
    COUNT(*) FILTER (WHERE name = 'level_quit') AS quits,
    MIN(x0) AS x_min, MAX(x1) AS x_max, MIN(y0) AS y_min, MAX(y1) AS y_max
FROM b
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3, 4, 5
