-- One profile per app: the newest LLM-classified profile (claims with verified quotes),
-- else the keyword profile; store-derived rows (monetization model) are always kept.
-- Long format: (app_key, dimension, value) with the field and quote that support it.
WITH profiles AS (
  SELECT DISTINCT app_key, profile_id, source, analyzed_at
  FROM raw_mr_profile WHERE source IN ('llm', 'keyword')
), chosen AS (
  SELECT app_key, profile_id, source
  FROM profiles
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY app_key
    ORDER BY CASE source WHEN 'llm' THEN 0 ELSE 1 END, analyzed_at DESC, profile_id
  ) = 1
), store_rows AS (
  SELECT * FROM raw_mr_profile WHERE source = 'store'
  QUALIFY DENSE_RANK() OVER (PARTITION BY app_key ORDER BY analyzed_at DESC, profile_id) = 1
)
SELECT r.app_key, r.profile_id, r.source, r.dimension, r.value, r.field, r.quote, r.visual
FROM raw_mr_profile AS r
JOIN chosen USING (app_key, profile_id)
UNION ALL
SELECT app_key, profile_id, source, dimension, value, field, quote, visual FROM store_rows
