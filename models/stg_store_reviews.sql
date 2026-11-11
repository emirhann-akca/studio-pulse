-- Store reviews, one row per (app, store, review): a review seen in a Play CSV
-- and in the Play API, or loaded twice, is kept once, in its latest edit.
SELECT
    app_id, store, review_id, submitted_at, updated_at, date, rating, title, body,
    language, territory, app_version, device, has_reply, origin
FROM raw_store_reviews
WHERE rating BETWEEN 1 AND 5 AND date IS NOT NULL
QUALIFY row_number() OVER (
    PARTITION BY app_id, store, review_id
    ORDER BY updated_at DESC NULLS LAST, CASE origin WHEN 'play_csv' THEN 0 ELSE 1 END, _loaded_at DESC
) = 1
