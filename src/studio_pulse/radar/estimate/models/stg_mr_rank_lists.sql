-- Which list was observed on which day, from the estimates' rank input (a list missing on a
-- day is unknown, not empty).
SELECT DISTINCT snapshot_date, store, country, list_key, list_kind, all_games, list_len, source
FROM stg_mr_rank_input
