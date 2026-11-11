-- Reviews written per app, store (and store 'all') and day.
--   avg_rating       mean star rating of that day's reviews (1-5)
--   low_star_share   share of 1- and 2-star reviews
SELECT
    app_id,
    CASE WHEN GROUPING(store) = 1 THEN 'all' ELSE store END AS store,
    date,
    COUNT(*) AS reviews,
    AVG(rating) AS avg_rating,
    AVG(CASE WHEN rating <= 2 THEN 1.0 ELSE 0.0 END) AS low_star_share,
    AVG(CASE WHEN rating = 5 THEN 1.0 ELSE 0.0 END) AS five_star_share
FROM stg_store_reviews
GROUP BY GROUPING SETS ((app_id, store, date), (app_id, date))
ORDER BY app_id, store, date
