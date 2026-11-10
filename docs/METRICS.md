# Metric definitions

Every number on the dashboard comes from one of the SQL models in `models/`.
This page gives the exact formula, the model that computes it and the caveats.

## What "a day" is

"Day" is the GA4 `event_date`, which GA4 writes in the property's reporting
time zone. Each app sets that zone in the config (`timezone: Europe/Istanbul`,
default `UTC`), and every source that delivers UTC timestamps is converted to
it before it is cut into days: Crashlytics `event_timestamp`, RevenueCat
`start_time` / `first_seen_time`, the Unity package's `ts` and GA4's own
`user_first_touch_timestamp`. Without this, a crash at 23:30 in Istanbul is
counted on the next day and divided by the wrong DAU. The warehouse session
runs in UTC, so a run gives the same dates on any machine (DuckDB otherwise
converts time-zone-aware Parquet timestamps to the local zone of whoever runs
it). AdMob and the store reports already arrive as days in their own reporting
zone and are not shifted.

Late data: GA4 adds late events to a day's table for up to 72 hours, and
RevenueCat re-delivers changed transactions. Every run re-reads the whole
lookback window (60 days by default), so recent days are revised as data
arrives; the last two or three days can still move.

Duplicates: the GA4 export can contain the same event twice (client retries).
Counts of users and sessions are distinct counts and unaffected; purchase and
ad-impression rows that are identical in user, timestamp, name and value are
counted once before revenue is summed.

## Activity

| Metric | Formula | Model / column |
|---|---|---|
| Active user (on day d) | a `user_pseudo_id` with at least one event on d whose name is not passive (`app_remove`, `app_clear_data`, `app_update`, `os_update`, `notification_receive`, `notification_dismiss`, `firebase_campaign`) | extraction query `active_users` |
| DAU | distinct active users on d | `fct_app_daily.dau` |
| WAU | distinct active users in [d-6, d]; shown only with >= 7 days of history | `fct_app_daily.wau` |
| MAU | distinct active users in [d-29, d]; shown only with >= 30 days of history | `fct_app_daily.mau` |
| Stickiness | DAU / MAU | `fct_app_daily.stickiness` |
| New users | active users on d who logged `first_open` (or `first_visit`) on d | `fct_app_daily.new_users` |
| Store installs | Play: `Daily User Installs` (fallback `Install events`, `Daily Device Installs`); App Store: Sales units with product types 1, 1F, 1T, 1E, 1EP, 1EU, F1 | `fct_app_daily.store_installs` |
| Sessions | distinct (`user_pseudo_id`, `ga_session_id`) pairs on d | `fct_app_daily.sessions` |
| Sessions per DAU | sessions / DAU | `fct_app_daily.sessions_per_dau` |
| Engagement minutes per DAU | sum(`engagement_time_msec`) / 60 000 / DAU | `fct_app_daily.engagement_min_per_dau` |

Users are counted per platform and summed: a GA4 `user_pseudo_id` is an app
instance, so the same person on Android and iOS counts twice (as in GA4).

## Retention

Cohort = install day. A user's cohort date is the day of their `first_open`
inside the loaded window, or, if that event is outside the window, the date (in the app's time zone)
of `user_first_touch_timestamp`. Only cohorts that start inside the window are
used, so every cohort is complete from day 0.

| Metric | Formula | Model / column |
|---|---|---|
| Cohort size | users of the cohort active on day 0 | `fct_retention.cohort_size` |
| Retention D*N* (one cohort) | users of the cohort active on cohort_date + N / cohort size | `fct_retention.retention` |
| Complete cell | cohort_date + N <= last day with data | `fct_retention.is_complete` |
| Headline D1 / D7 / D14 / D30 | sum of retained users / sum of cohort sizes over the 28 most recent cohorts that are complete for that N (a cohort-size weighted mean) | `fct_retention_summary.retention` |

This is "classic" (day-N) retention, not rolling/unbounded retention: a user
counts for D7 only if active exactly on day 7.

## Revenue

All revenue is in USD. Two bases are kept apart and labelled on the dashboard
(`iap_basis`, `ad_basis`).

| Metric | Formula | Model / column |
|---|---|---|
| RevenueCat transaction | the latest received version of each (`store_transaction_id`, `renewal_number`), sandbox excluded (see below) | `stg_rc_transactions` |
| IAP revenue (RevenueCat) | `price_in_usd` (net of full and partial refunds); when it is missing, `price_in_purchased_currency` x the fallback FX rate of the purchase day | `stg_rc_transactions.revenue_usd` |
| IAP proceeds | revenue x (1 - `tax_percentage` - `commission_percentage`); with older exports that only have `takehome_percentage`: revenue x takehome; unknown otherwise | `stg_rc_transactions.proceeds_usd` |
| IAP revenue per day | RevenueCat proceeds by purchase date (`start_time`) when the app has RevenueCat data; otherwise the sum of GA4 `event_value_in_usd` of the configured purchase events (gross, before store fees) | `fct_app_daily.iap_revenue_usd` |
| Ad revenue per day | AdMob `ESTIMATED_EARNINGS` (converted to USD) when AdMob is connected; otherwise the sum of GA4 `ad_impression` `event_value_in_usd` | `fct_app_daily.ad_revenue_usd` |
| Net revenue | IAP revenue + ad revenue | `fct_app_daily.revenue_usd` |
| ARPDAU | net revenue / DAU | `fct_app_daily.arpdau` |
| Payers | RevenueCat: distinct customers with a transaction with revenue > 0 that day (renewals and trial conversions included, so for subscription apps this is "customers charged", not only users who bought in-app); GA4: distinct users with a purchase event of value > 0 | `fct_app_daily.payers` |
| Payer conversion (daily) | payers / DAU | `fct_app_daily.payer_conversion` |
| eCPM | AdMob earnings x 1000 / impressions | `fct_app_daily.ecpm_usd` |

Refunds reduce revenue on the original purchase date (RevenueCat's
`price_in_usd` already reflects them), so recent days can still move down when
a refund arrives later. Accounting reports from the stores remain the source of
truth for payouts.

### Incremental RevenueCat export

With "receive new and updated transactions only", each delivery contains the
rows created or changed since the previous one. A transaction that is later
refunded, unsubscribed or put into a billing retry appears again in a later
file. The connector stores every version (`raw_revenuecat_transactions`, one
row per file row, with the file name and delivery time) and the model keeps
exactly one version per (`store_transaction_id`, `renewal_number`) - the
identifier RevenueCat documents - ordered by `updated_at`, then delivery time,
then row number. Summing the raw rows instead would count a refunded purchase
at full price and every re-delivered row twice; the test
`tests/test_revenue_models.py::test_naive_sum_would_double_count` shows both
effects.

## LTV curve and cohort payer rate

For day N (0..90):

```
cumulative_arpu(N) = sum over eligible cohorts c of revenue_c(0..N) / sum over eligible cohorts of size_c
```

A cohort is eligible for N when cohort_date + N <= last day with data, and day N
is shown only when at least 7 cohorts are eligible. `revenue_c(0..N)` is
ad revenue (GA4 `ad_impression` values of the cohort's users) plus IAP: RevenueCat
proceeds of customers whose RevenueCat `first_seen_time` falls on cohort_date
(when the app has RevenueCat data), otherwise GA4 purchase values of the
cohort's users. Cohort sizes always come from GA4.

`payer_rate(N)` = users of eligible cohorts who paid at least once by day N /
sum of their cohort sizes.

Caveats: RevenueCat customers and GA4 users are matched by day, not by id; and
because older cohorts are the only ones eligible at large N, the tail of the
curve rests on fewer users (the number of cohorts per point is in the data
table).

## Stability

Crash-free users here is the share of DAU without a fatal crash that day
(Firebase's definition, computed per day), so a 7-day figure is a DAU-weighted
mean of daily rates, not the console's distinct-user count over 7 days.

| Metric | Formula | Model / column |
|---|---|---|
| Crashed users | Crashlytics export: distinct `installation_uuid` with an `is_fatal` event that day (`event_timestamp` converted to the app's time zone); without the export: GA4 users with `app_exception` and `fatal = 1` | `fct_app_daily.crashed_users` |
| Crash-free users | 1 - min(crashed users, DAU) / DAU | `fct_app_daily.crash_free_users` |
| Crash-free sessions | 1 - min(fatal crash events, sessions) / sessions (Crashlytics); 1 - sessions with a fatal `app_exception` / sessions (GA4) | `fct_app_daily.crash_free_sessions` |
| 7-day crash-free users | 1 - sum(min(crashed users, DAU)) / sum(DAU) over 7 days | `fct_app_summary.crash_free_users_7d` |

The Crashlytics export contains crashes only, no sessions, so its numerator is
divided by GA4 DAU/sessions. The numerator is capped at the denominator: a
device stuck in a crash loop can report more fatal crashes in a day than GA4
recorded sessions (often the app dies before `session_start` is sent), which
would otherwise push the rate below zero. The ids (Crashlytics installation vs GA4 app
instance) differ, so the result approximates the Firebase console's figure rather than reproducing it.

## Client performance

From `pulse_perf` windows (see `docs/EVENTS.md`).

| Metric | Formula | Model / column |
|---|---|---|
| Frame time p50 (tier, period) | median over windows of the window's `ft_p50_ms` | `fct_perf_by_tier.ft_p50_ms`, `fct_perf_daily.ft_p50_ms` |
| Frame time p95 (tier, period) | median over windows of the window's `ft_p95_ms` (the typical session's tail) | `.ft_p95_ms` |
| Slow-frame share | sum(`slow_frames`) / sum(`frames`) - exact share of frames slower than 33.3 ms | `.slow_frame_share` |
| CPU main thread / GPU p95 | median over windows of the window's p95 FrameTimingManager CPU main-thread / GPU time. A GPU p95 close to the frame p95 means GPU-bound | `fct_perf_by_tier.cpu_main_p95_ms`, `.gpu_p95_ms`, `fct_perf_by_version` |
| Frame timing coverage | share of windows whose frame times came from FrameTimingManager (`ft_src = 'ftm'`) rather than `Time.unscaledDeltaTime` | `fct_perf_by_tier.ftm_share` |
| Memory p95 | 95th percentile over windows of peak allocated MB (and reserved MB) | `.mem_alloc_p95_mb`, `.mem_reserved_p95_mb` |
| Process memory p95 | the same for System Used Memory, the OS view that low-memory kills act on | `.sys_used_p95_mb` |
| By version | the same per `app_version` and tier, with the first day each version appeared | `fct_perf_by_version` |
| Cold start p50 / p95 | percentiles of `duration_ms` over `cold_start` samples | `fct_load_times`, `fct_load_daily` |
| Scene load p50 / p95 | the same for `scene_load`, per scene | `fct_load_times` |

"Last 28 days" means the 28 days ending at the app's latest perf data day.
Percentiles use linear interpolation (`quantile_cont`). Medians of per-window
percentiles are not the population percentiles of all frames; exact
population percentiles would need per-window histograms, which the schema does
not carry yet. The slow-frame share is exact.

## Level funnel (puzzle games)

| Metric | Formula | Model / column |
|---|---|---|
| Starts / completes / fails | event counts of `level_start` / `level_complete` / `level_fail` (`level_end` with success counts as complete, without as fail) | `fct_level_funnel` |
| Win rate | completes / (completes + fails) | `.win_rate` |
| Attempts per completion | starts / completes | `.attempts_per_completion` |
| Users reached | users whose highest started level in the window is >= level | `.users_reached` |
| Churn at level | users whose highest started level is this level and who were not active in the last `churn_window_days` (default 7) before the window end / users reached | `.churn_rate` |

Levels are assumed to be played in order. Players who started before the
window may have reached levels earlier; only levels started inside the window
are seen.

## Portfolio

`fct_app_summary` has one row per app with the 7 days ending at the app's last
day with DAU data ("7d") and the 7 days before ("prior 7d"): mean DAU and its
change, sums of new users, installs and revenue, ARPDAU = sum(revenue) /
sum(DAU), payer conversion = sum(payers) / sum(DAU), crash-free users, the
latest WAU/MAU/stickiness, the headline retention, the low-tier and overall
frame time p50, the latest store rating and the LTV curve at the latest day <= 30.

## Data freshness

`fct_source_freshness`: for each app and source, the newest data date
(`data_through`), the oldest, the time of the last successful load, rows and
BigQuery bytes billed by the last load. The dashboard marks a source fresh
(<= 2 days behind the "as of" date), stale (<= 7) or old.
