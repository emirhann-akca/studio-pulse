-- Data freshness per app and source: the newest data date and the time of the
-- last successful load (from the loader's bookkeeping table).
WITH latest AS (
    SELECT *
    FROM meta_loads
    QUALIFY row_number() OVER (PARTITION BY app_id, source, table_name ORDER BY loaded_at DESC) = 1
)
SELECT
    app_id,
    source,
    MAX(max_date) AS data_through,
    MIN(min_date) AS data_from,
    MAX(loaded_at) AS last_loaded_at,
    SUM(rows_loaded) AS rows_loaded,
    SUM(bytes_billed) AS bytes_billed
FROM latest
GROUP BY 1, 2
ORDER BY 1, 2
