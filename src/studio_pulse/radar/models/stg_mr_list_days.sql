-- Which list was observed on which day (a list missing on a day is unknown, not empty).
SELECT DISTINCT snapshot_date, store, country, chart, genre_id, list_key, list_len
FROM stg_mr_chart
