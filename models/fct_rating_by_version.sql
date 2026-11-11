-- Store rating per app version (versions are known for Google Play reviews; App
-- Store reviews carry none). A release that hurts shows as a low-rated row.
SELECT
    app_id, store, app_version,
    MIN(date) AS first_date,
    MAX(date) AS last_date,
    COUNT(*) AS reviews,
    AVG(rating) AS avg_rating,
    AVG(CASE WHEN rating <= 2 THEN 1.0 ELSE 0.0 END) AS low_star_share
FROM stg_store_reviews
WHERE app_version IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY app_id, store, first_date
