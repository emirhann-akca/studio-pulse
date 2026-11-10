-- Store reports per app, store and day: installs/uninstalls (Play statistics,
-- App Store Sales units) and ratings. Proceeds converted to USD.
WITH fx AS (
    SELECT currency, usd_per_unit, valid_from,
           lead(valid_from) OVER (PARTITION BY currency ORDER BY valid_from) AS valid_to
    FROM raw_fx_rates
),
inst AS (
    SELECT
        i.app_id, i.store, i.date,
        SUM(i.installs) AS installs,
        SUM(i.uninstalls) AS uninstalls,
        SUM(i.redownloads) AS redownloads,
        SUM(i.updates) AS updates,
        SUM(i.iap_units) AS iap_units,
        SUM(CASE WHEN upper(COALESCE(i.proceeds_currency, 'USD')) = 'USD' THEN i.proceeds
                 ELSE i.proceeds * fx.usd_per_unit END) AS proceeds_usd
    FROM raw_store_installs i
    LEFT JOIN fx
        ON fx.currency = upper(i.proceeds_currency)
        AND i.date >= fx.valid_from
        AND (fx.valid_to IS NULL OR i.date < fx.valid_to)
    GROUP BY 1, 2, 3
),
rat AS (
    SELECT app_id, store, date,
           avg(daily_avg_rating) AS daily_avg_rating,
           avg(total_avg_rating) AS total_avg_rating
    FROM raw_store_ratings
    GROUP BY 1, 2, 3
)
SELECT
    app_id, store, date,
    inst.installs, inst.uninstalls, inst.redownloads, inst.updates, inst.iap_units, inst.proceeds_usd,
    rat.daily_avg_rating, rat.total_avg_rating
FROM inst
FULL OUTER JOIN rat USING (app_id, store, date)
