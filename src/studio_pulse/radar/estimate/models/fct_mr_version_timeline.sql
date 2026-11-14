-- Version / update timeline per app, store and storefront: one row per version seen, with
-- the store's own release date of that version (Lookup currentVersionReleaseDate, Play
-- "updated") and the first and last radar snapshot that showed it. Apple release notes of
-- the version are kept (store text: local only, like every radar table).
SELECT
  store, app_id, country, version,
  MIN(current_version_date) AS version_date,
  MIN(snapshot_date) AS first_seen,
  MAX(snapshot_date) AS last_seen,
  COUNT(DISTINCT snapshot_date) AS days_seen,
  arg_max(release_notes, snapshot_date) AS release_notes,
  arg_max(name, snapshot_date) AS name
FROM raw_mr_app
WHERE version IS NOT NULL AND app_id IS NOT NULL
GROUP BY store, app_id, country, version
