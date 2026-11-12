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
counts for D7 only if active exactly on day 7. Rolling retention (active on day
7 or later) is always higher and is not comparable with the benchmarks below.

### Benchmarks

Game pages show the headline D1/D7/D30 against the public brackets from
GameAnalytics' "Mobile and PC Game Benchmarks 2026": 16,262 mobile games with
at least 1,000 MAU, iOS and Android, all genres combined (summary:
<https://gamedevreports.substack.com/p/gameanalytics-mobile-and-pc-game>).

| Day | Median | Top 25% | Top 10% |
|---|---|---|---|
| D1 | not given in the summary | just above 30% | 40% |
| D7 | just under 4% | 6-7% | 11-12% |
| D30 | 0.69-0.79% | about 1.6-1.8% | not given |

The bracket a game falls into uses the middle of each range
(`src/studio_pulse/benchmarks.py`). These figures mix every genre, so a puzzle
game should beat them; they are not drawn for non-game apps (genre `fitness`,
`health`, `app`, ...). The synthetic demo is calibrated to them: the good
puzzle game lands between the top 25% and top 10% on D1 and D7, the weaker
casual game just above the median on D7.

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
| Crashed users | Per platform, then summed. Where the platform's Crashlytics export is connected: distinct `installation_uuid` with an `is_fatal` event that day (`event_timestamp` converted to the app's time zone); otherwise GA4 users with `app_exception` and `fatal = 1`. `crash_basis` says which (`crashlytics`, `ga4_app_exception` or both) | `fct_app_daily.crashed_users` |
| Crash-free users | 1 - min(crashed users, DAU) / DAU; unknown (not 100%) when no crash source covers the day | `fct_app_daily.crash_free_users` |
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

## User acquisition (UA)

Spend comes from the ad networks (`raw_ua_spend`, one contract for every
network, see `src/studio_pulse/connectors/ua/common.py`). Amounts are kept in
the account currency and converted in `stg_ua_spend`: `spend_usd = spend` for
USD accounts, otherwise `spend x usd_per_unit` of the fallback FX table on that
day (`fx_rates` in the config); a currency without a rate leaves `spend_usd`
empty and fails the check `ua_spend_converted`.

| Metric | Formula | Model / column |
|---|---|---|
| CPI (network) | spend_usd / network-reported installs | `fct_ua_daily.cpi_usd`, `fct_ua_summary.network_cpi_30d_usd` |
| CTR | clicks / impressions | `fct_ua_daily.ctr` |
| IPM | network installs / impressions x 1000 | `fct_ua_daily.ipm` |
| Spend share | the campaign's spend / the app's spend that day | `fct_ua_daily.spend_share` |
| Cohort revenue by day N | see the two bases below; cumulative over days 0..N of the install cohort of the spend day | `stg_ua_cohort_cum.cum_revenue_usd` |
| Cohort ROAS DN | cohort revenue by day N / spend of that day; empty until day N is complete | `fct_ua_cohort_roas.roas_d0/d7/d30` |
| Pooled ROAS curve | sum over the cohorts that have reached day N of revenue by day N / their spend; points backed by fewer than 7 cohorts are dropped | `fct_ua_roas_curve.roas` |
| Payback day (observed) | the first N whose pooled ROAS reaches 100%; empty when the curve has not reached it within its observed days | `fct_ua_summary.payback_day` |
| Blended ROAS | revenue of *all* new users (paid and organic) of the spend days / spend | `fct_ua_roas_curve.blended_roas` (app rows) |
| CPI (basis) | spend / installs on the network's attribution basis, 30 days | `fct_ua_summary.cpi_30d_usd` |
| Payback day (predicted) | the first day the pLTV model's predicted cumulative ARPU reaches the CPI (UA page) | `fct_pltv_curve` |

Spend on day d is assumed to buy the installs of cohort d. Two attribution
bases, chosen per network in the config (`attribution:`; default `ga4` for
Google Ads, `network_installs` for the rest):

- **`ga4` (GA4 first touch).** GA4's user-scoped `traffic_source.source /
  medium / name` (the user's first-touch campaign) is aggregated in the
  extraction query `campaign_cohorts` (paid media only, day 0..90, no ids).
  A network's cohort is the users whose source is in its `ga4_sources`
  (default `google`) and whose campaign name equals the spend's campaign name
  (case-insensitive, trimmed); installs = those users, revenue = their GA4
  purchase and `ad_impression` values. It works where GA4 sees the campaign
  (Google Ads linked to Firebase). GA4 purchase values are gross (before store
  fees), so a subscription app whose revenue arrives through RevenueCat should
  use the other basis.
- **`network_installs` (modeled).** Installs = the network's own count;
  revenue = installs x the cumulative ARPU of the app's whole install cohort of
  that day (`fct_cohort_ltv`, the LTV curve's revenue definition). It assumes
  bought users monetise like the average new user of the same day, so it
  overstates campaigns that buy cheap, low-ARPU countries; without an MMP
  there is no per-user attribution for Meta, TikTok or iOS. Every figure on
  the UA page names its basis.

## Creative scorecard

`fct_creative_scorecard`, one row per creative and network plus a network
`(all)` row. A creative is the `creative_id` parsed from its ad name when the
name is one of ours, otherwise `network:ad_id`.

| Metric | Formula |
|---|---|
| Impressions, clicks, installs, spend | sums over every day and ad of the creative (network-reported) |
| IPM and 95% interval | installs / impressions x 1000; Wilson score interval for the proportion installs / impressions, x 1000 (`ipm_low`, `ipm_high`) |
| CTR, CPI | clicks / impressions, spend / installs |
| D7 ROAS (modeled) | sum over spend days complete at day 7 of installs x the app cohort's D7 cumulative ARPU / the spend of those days (networks do not attribute revenue to ads) |
| Status and rank | `insufficient data` below the minimum impressions (`ua: {min_creative_impressions: 5000}`), never ranked; otherwise ranked by IPM within app and network |

Creative ids: the default parser (`connectors/ua/creative.py`) finds a
[creative-forge](https://github.com/emirhann-akca/creative-forge) ad name
anywhere in the network's name and checks its id:
`cf1.<spec>.<concept>.<hook>.<segment>.<end_card>.<format>.<language>.<creative_id>`
with `creative_id` = the first 10 hex digits of
`sha256("cf1|spec|concept|hook|segment|end_card|format|language")`; a name
whose id does not match is not ours. Contract for another parser
(`ua: {creative_parser: "package.module:function"}` or
`"regex:<pattern with (?P<creative_id>...)>"`): a callable
`parse(ad_name) -> mapping | None` returning at least `creative_id`, optionally
`concept`.

## Experiments

The extraction query `experiment_arms` (`connectors/ga4.py`, run inside
BigQuery; `pulse experiment --sql` prints it) returns per variant: users,
conversions, sum(y), sum(y^2), sum(x), sum(x^2), sum(x y), where for each
user exposed to exactly one variant, from the first exposure day (day 0):
y = the metric over `window_days` (count of `metric_event`, or with
`metric_value: value` the sum of its `event_value_in_usd`), x = the same metric
over the `pre_days` before day 0, converted = at least one `conversion_event`
in the window. Users exposed to two variants are dropped; users whose window is
not complete yet are not counted. Exposure is an event (default
`experiment_exposure`) whose parameters hold the experiment id and the variant.

From those sums (`src/studio_pulse/stats/experiments.py`, stdlib only):

| Test | Formula |
|---|---|
| Sample ratio mismatch | chi-square goodness of fit of users per variant against the configured split, k - 1 degrees of freedom; flagged at p < 0.001 (GrowthBook's threshold) |
| Conversion | two-proportion z-test: z = (p_t - p_c) / sqrt(p (1 - p) (1/n_c + 1/n_t)) with the pooled p; Wald interval with unpooled variances |
| Mean (Welch) | mean = sum(y) / n, var = (sum(y^2) - sum(y)^2 / n) / (n - 1); t = (m_t - m_c) / sqrt(v_t/n_t + v_c/n_c), Welch-Satterthwaite degrees of freedom |
| CUPED | theta = cov(x, y) / var(x) over the users of both arms; per arm adjusted mean = mean(y) - theta mean(x), adjusted variance = var(y) + theta^2 var(x) - 2 theta cov(x, y); then Welch. Variance reduction = 1 - se_cuped^2 / se_welch^2 (about rho^2) |
| Always-valid (sequential) | rho = sqrt((-2 ln alpha + ln(-2 ln alpha + 1)) / 5000); with n = users of both arms and s2 = se^2 n: half-width = sqrt(s2) sqrt(2 (n rho^2 + 1) ln(sqrt(n rho^2 + 1) / alpha) / (n rho)^2); p = min(1, sqrt(n rho^2 + 1) / exp(rho^2 (est^2 n / se^2) / (2 (n rho^2 + 1)))) (Waudby-Smith et al., arXiv:2103.06476, eq. 9 and 155), on the CUPED difference when a covariate exists |
| Relative lift | (m_t - m_c) / m_c with a delta-method interval |

The CUPED, Welch and sequential formulas are ported from GrowthBook's
statistics engine (`growthbook/growthbook`, `packages/stats/gbstats`:
`models/statistics.py` RegressionAdjustedStatistic / compute_theta,
`frequentist/tests.py` TwoSidedTTest, sequential_rho,
sequential_interval_halfwidth, SequentialTwoSidedTTest.p_value; commit
df3f4e2, 11 Nov 2026; MIT licence, Copyright (c) 2025 GrowthBook, Inc.). The
`gbstats` package on PyPI is not used: its latest release (0.8.0, June 2024)
pins pandas < 2 and numpy < 2.

Validation (`tests/test_experiments.py`): Student t and chi-square functions
against scipy to 1e-9 or better; Welch against `scipy.stats.ttest_ind`; the
z-test and its interval against statsmodels; CUPED against Welch on the
adjusted raw rows and against an OLS `y ~ treatment + x`; the sequential
formulas against a numpy transcription of GrowthBook's code, plus a
simulation in which 20 looks at A/A tests make the fixed-horizon test find an
"effect" in over 15% of experiments and the sequential test in at most 5%;
and the published example of the NIST/SEMATECH e-Handbook of Statistical
Methods, section 7.3.1 (two assembly processes, n = 11 and 9, unequal
variances: t = 2.2694, about 15.5 degrees of freedom), reproduced to four
decimals.

Reading it: a flagged SRM invalidates the experiment. A fixed-horizon p-value
is valid only at the planned end; if you look every day, decide on the
always-valid interval (the verdict says which applies).

## Seasonality-aware alerts (`forecast_band`)

The rule forecasts the evaluated day from the `history_days` (default 56)
before it and fires when the value leaves the prediction interval (`direction`
drop, rise or both). Engines (`src/studio_pulse/stats/forecast.py`):

- `seasonal` (no extra dependency): least squares of log(value) on a linear
  trend and weekday effects (additive when a value is <= 0); points more than
  4 robust standard deviations (1.4826 MAD) off the first fit are dropped and
  the model refitted; interval = t(n - 8) s sqrt(1 + x0'(X'X)^-1 x0), the
  classical prediction interval, at least 2% wide.
- `statsforecast` (extra `studio-pulse[forecast]`, Apache-2.0): Nixtla
  statsforecast AutoETS with a 7-day season and its `level` interval.
- `auto` (default): statsforecast when installed, otherwise seasonal.

`holidays:` days never alert and are left out of the history. Default levels,
from the backtest in `tests/test_forecast.py` (120 days, weekends -20%,
+0.2%/day trend, 3% noise, a listed holiday at -25%, a -30% step from day
100): the seasonal engine at 99.5% caught the step on its first day with 0-1
false alerts over seeds 1-8; AutoETS at 99.5% gave 1-3 and at 99.9% 0-2, so
its default is 99.9%. A trailing 14-day mean rule (drop 15%) on the same series
fired 7-12 times, on ordinary weekends.

## Store reviews and ratings

| Metric | Formula | Model / column |
|---|---|---|
| Review | one row per (app, store, review id), the latest edit; Play CSV and Play API rows of the same review merge | `stg_store_reviews` |
| Rating of new reviews | mean stars of the reviews written (or last edited) that day; the dashboard shows a 7-day review-weighted mean | `fct_review_daily.avg_rating` |
| Low-star share | share of 1- and 2-star reviews | `fct_review_daily.low_star_share` |
| Rating by version | mean stars and low-star share per app version (Play reviews carry the version, App Store reviews do not) | `fct_rating_by_version` |
| Topics | share of the last 30 days' reviews whose title + text match a keyword pattern (English words, Turkish stems), against the 30 days before; a review can match several topics | `fct_review_topics` |

The cumulative store rating (Play's `Total Average Rating`) remains in
`stg_store_daily`. Reviews are dated in the app's time zone.

## Android vitals

`fct_android_vitals_daily`: Play Developer Reporting API, DAILY (America/Los_
Angeles days): `crash_rate`, `user_perceived_crash_rate`, `anr_rate`,
`user_perceived_anr_rate`, `distinct_users`. Play's bad-behaviour thresholds
are a user-perceived crash rate of 1.09% and ANR rate of 0.47%; the rule
`play_crash_rate_bad` uses the first.

## Predicted LTV v1

`src/studio_pulse/stats/pltv.py`, tables `fct_pltv_summary`,
`fct_pltv_curve`, `fct_pltv_backtest`. Install cohorts are pooled by week; a
group's curve is its pooled cumulative ARPU (`fct_cohort_ltv`). Two models are
fitted on days 0..7: power `a (t + 1)^b` (least squares in log-log) and log
`a + b ln(t + 1)`. A raw extrapolation is calibrated by the median of actual /
raw D30 over earlier groups. Backtest: each weekly group with a known D30 is
predicted from its days 0..7, calibrated only on groups whose D30 was known
when that group's day 7 arrived; MAPE = mean |predicted - actual| / actual. The
model with the lower MAPE predicts the latest 28 cohorts at D14/30/60/90/180,
with an interval from the 10th-90th percentile of the calibration ratios,
widened to at least +-MAPE, and never below the observed day-7 value. On the
demo: games backtest at 2.5-4% (log curve); the subscription app at about 35%,
because trial conversions on day 7 and renewals from day 37 make a stepped
curve that neither shape follows (a Shifted-BG subscription model is the v2).

## SDK apps (Studio Pulse SDK, collector)

Apps that send the SDK's events ([CONTRACT.md](CONTRACT.md), [COLLECTOR.md](COLLECTOR.md))
get the same metrics as GA4 apps: `stg_sdk_active_users` and `stg_sdk_cohort_days`
have the GA4 shapes and are unioned into `stg_active_users` / `stg_cohort_days`, so
every formula above applies unchanged. What differs is where the inputs come from:

| Metric | SDK definition |
|---|---|
| User | an install (`install_id`, random per install); production builds only (`build_env = release`, not editor or simulator) |
| Day | the event's skew-corrected time (`ts = received_at - (sent_at - ts_client)`) in the app's time zone |
| DAU / WAU / MAU | distinct installs with any event that day / in the 7 / 30 days up to it, per platform (summed for the app) |
| New users, install cohort | installs whose `pulse_install` happened that day; installs older than the loaded window have no cohort (they would look new on the window's first day) |
| Session | one `sid` (the SDK starts a new one after 30 minutes in the background), split again where 30 minutes pass without an event |
| Session length | the session's foreground time (`fg_ms`, the SDK's clock), capped at 24 h; without `fg_ms`, first-to-last event |
| Crashed session / user | the next launch's `pulse_exit` with reason `crash`, `crash_native` or `anr` for that session, or a fatal `pulse_error` in it; crash-free = 1 - crashed / total as above |
| IAP revenue | `purchase` events, one per `transaction_id`, `price_micros / 1e6` x the config's `fx_rates` (USD per unit); sandbox purchases and restores count 0 |
| Ad revenue | `ad_impression.value` x `fx_rates` for its currency |
| Payers | installs with a purchase that has revenue that day |

Level funnel (`fct_sdk_level_funnel`, production builds):

- `fail_rate` = fails / (completes + fails); `attempts_per_completion` = starts / completes;
- `avg_attempts_to_win` = mean of each player's `attempt` on their first `level_complete`;
- `quit_inferred` = sessions whose last level event is a `level_start` with no outcome
  (the app was killed or left mid-level);
- `players_reached` = installs whose highest started level is at least this one;
  `churned_here` = churned installs (no event in the 7 days before the app's newest
  day) whose highest started level is this one: the level they quit at;
  `churn_rate` = churned_here / players_reached.
- The existing `fct_level_funnel` (GA4 definitions above) includes SDK apps through
  `stg_level_daily` / `stg_level_max`.

Death heatmap (`fct_sdk_fail_heatmap`): `level_fail` and `level_quit` positions per
level on a 10 x 10 grid. `pos_space = norm` positions are fractions of the level
(0-1); `world` positions are scaled by the level's observed min and max first.

Settings (`fct_sdk_settings`): `off_share` = installs that set a setting to `off`
(`off`, `0`, `false`, `mute`) / installs with any event; `off_in_first_session` = the
share of them who did it in session 1; `fct_sdk_settings_when` buckets by session
number (1, 2, 3-5, 6-10, 11+).

Errors: `errors_per_session` = `pulse_error` events / sessions; per fingerprint in
`fct_sdk_errors`.

Data quality (`fct_sdk_data_quality`, all builds): `duplicate_rate` = raw rows the
warehouse dedupe removed / raw rows; `seq_loss_rate` = 1 - received / expected
sequence numbers, per install between its lowest and highest `seq` in the window
(contract v2 rows only; a trailing gap is not visible until a later event arrives);
`skewed_share` = events whose device clock was more than 10 minutes off;
`quarantine_rate` = quarantined / (stored + quarantined) events.
