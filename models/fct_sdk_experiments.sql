-- Experiments the SDK assigned (experiment_exposure: Pulse.Experiment / StudioPulse.experiment),
-- one row per app, experiment and variation (-1: installs outside the experiment's coverage):
--   installs                installs that reported this variation (first exposure wins)
--   share                   installs / all installs in the experiment (NULL for -1)
--   switched_installs       installs that reported more than one variation: an assignment bug
--   hash_mismatch_installs  installs whose reported bucket differs from the bucket recomputed here
--                           from the experiment key and install id (GrowthBook hash v2: FNV-1a 32
--                           over the key + id, then over its decimal text, % 10000 / 10000; the
--                           same function as studio_pulse/assign.py and the two SDKs)
WITH e AS (
    SELECT app_id, install_id, ts,
           params->>'experiment_key' AS experiment_key,
           TRY_CAST(params->>'variation' AS INTEGER) AS variation,
           TRY_CAST(params->>'in_experiment' AS BOOLEAN) AS in_experiment,
           TRY_CAST(params->>'hash_value' AS DOUBLE) AS hash_value
    FROM stg_pulse_events
    WHERE is_prod AND name = 'experiment_exposure' AND params->>'experiment_key' IS NOT NULL
),
per_install AS (
    SELECT app_id, experiment_key, install_id,
           CASE WHEN arg_min(in_experiment, ts) THEN arg_min(variation, ts) ELSE -1 END AS variation,
           COUNT(DISTINCT CASE WHEN in_experiment THEN variation END) AS variations_seen,
           arg_min(hash_value, ts) AS hash_value,
           MIN(ts) AS first_exposed_at
    FROM e
    GROUP BY 1, 2, 3
),
checked AS (
    SELECT *, (list_reduce(list_prepend(2166136261::BIGINT, list_transform(range(1, length(CAST(list_reduce(list_prepend(2166136261::BIGINT, list_transform(range(1, length((experiment_key || install_id)) + 1), lambda i: unicode(substr((experiment_key || install_id), i::BIGINT, 1))::BIGINT)), lambda h, c: (xor(h, c) * 16777619) % 4294967296) AS VARCHAR)) + 1), lambda i: unicode(substr(CAST(list_reduce(list_prepend(2166136261::BIGINT, list_transform(range(1, length((experiment_key || install_id)) + 1), lambda i: unicode(substr((experiment_key || install_id), i::BIGINT, 1))::BIGINT)), lambda h, c: (xor(h, c) * 16777619) % 4294967296) AS VARCHAR), i::BIGINT, 1))::BIGINT)), lambda h, c: (xor(h, c) * 16777619) % 4294967296)) % 10000 / 10000.0 AS bucket
    FROM per_install
),
arms AS (
    SELECT app_id, experiment_key, variation,
           COUNT(*) AS installs,
           SUM(CASE WHEN variations_seen > 1 THEN 1 ELSE 0 END) AS switched_installs,
           SUM(CASE WHEN hash_value IS NOT NULL AND abs(hash_value - bucket) > 0.00005 THEN 1 ELSE 0 END)
               AS hash_mismatch_installs,
           MIN(first_exposed_at) AS first_exposed_at,
           MAX(first_exposed_at) AS last_exposed_at
    FROM checked
    GROUP BY 1, 2, 3
)
SELECT app_id, experiment_key, variation, installs,
       CASE WHEN variation >= 0 THEN
           installs / SUM(CASE WHEN variation >= 0 THEN installs ELSE 0 END)
               OVER (PARTITION BY app_id, experiment_key)
       END AS share,
       switched_installs, hash_mismatch_installs, first_exposed_at, last_exposed_at
FROM arms
