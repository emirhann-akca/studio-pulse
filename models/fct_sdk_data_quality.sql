-- SDK data quality per app (all builds), checked in checks.yaml:
--   duplicate_rate     raw rows the stg dedupe removed / raw rows (the collector drops most)
--   seq_loss_rate      1 - received / expected sequence numbers, per install between its
--                      lowest and highest seq in the window (contract v2 only)
--   skew_p95_ms        95th percentile of |received_at - sent_at| (device clock error)
--   skewed_share       events whose device clock was more than 10 minutes off (corrected)
--   late_share         events older than 30 days when they arrived
--   quarantine_rate    quarantined / (stored + quarantined) events
WITH raw AS (
    SELECT app_id, COUNT(*) AS raw_events FROM raw_pulse_events GROUP BY 1
),
st AS (
    SELECT app_id, COUNT(*) AS events,
           quantile_cont(abs(skew_ms), 0.95) AS skew_p95_ms,
           AVG(CASE WHEN abs(skew_ms) > 600000 THEN 1.0 ELSE 0.0 END) AS skewed_share,
           AVG(CASE WHEN late THEN 1.0 ELSE 0.0 END) AS late_share,
           COUNT(DISTINCT install_id) AS installs,
           COUNT(*) FILTER (WHERE NOT is_prod) AS non_prod_events
    FROM stg_pulse_events GROUP BY 1
),
seqs AS (
    SELECT app_id, install_id, MAX(seq) - MIN(seq) + 1 AS expected, COUNT(DISTINCT seq) AS got
    FROM stg_pulse_events WHERE schema <> '1' GROUP BY 1, 2
),
loss AS (
    SELECT app_id, SUM(expected) AS seq_expected, SUM(got) AS seq_received FROM seqs GROUP BY 1
),
q AS (
    SELECT app_id, SUM(events) AS quarantined FROM raw_pulse_quarantine GROUP BY 1
)
SELECT
    st.app_id,
    raw.raw_events,
    st.events,
    st.installs,
    st.non_prod_events,
    raw.raw_events - st.events AS duplicate_rows,
    CAST(raw.raw_events - st.events AS DOUBLE) / NULLIF(raw.raw_events, 0) AS duplicate_rate,
    loss.seq_expected,
    loss.seq_received,
    1 - CAST(loss.seq_received AS DOUBLE) / NULLIF(loss.seq_expected, 0) AS seq_loss_rate,
    st.skew_p95_ms,
    st.skewed_share,
    st.late_share,
    COALESCE(q.quarantined, 0) AS quarantined,
    CAST(COALESCE(q.quarantined, 0) AS DOUBLE) / NULLIF(st.events + COALESCE(q.quarantined, 0), 0)
        AS quarantine_rate
FROM st
JOIN raw USING (app_id)
LEFT JOIN loss USING (app_id)
LEFT JOIN q USING (app_id)
ORDER BY 1
