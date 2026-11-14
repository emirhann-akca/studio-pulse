# Market radar

A daily scan of public store charts: which new games are rising, which mechanics are gaining
chart share, and what that suggests for the studio's next creative tests. Output: a Turkish and
an English digest, a local page in `pulse serve` and `market_trends.yaml` for creative-forge.

```bash
pulse radar run -c pulse.yaml                    # today's snapshot, models, digest
pulse radar run --replay tests/fixtures/radar/2026-11-12   # recorded responses, no network
pulse radar show -c pulse.yaml --lang en         # print the newest digest
pulse radar plan -c pulse.yaml                   # expected store requests per day
pulse radar watch add 6811006356 -c pulse.yaml   # look this game up daily (also: remove, list)
pulse serve -c pulse.yaml                        # /radar and /radar/oyun/<id>: game page
```

With `radar.enabled: true` the radar also runs inside every `pulse run`, so `pulse schedule
install` makes it daily; its data-quality checks are stored with the studio's own.

## Sources and rules

| Source | What | Volume per day (8 countries x 5 lists) | Status |
|---|---|---|---|
| App Store chart pages `apps.apple.com/{cc}/iphone/charts/{genre}` (default, `radar.apple.chart_source: web`) | one page per storefront and genre holds Top Free and Top Paid Games: the first 25 apps in full, the ids of ranks 26-200 in order (`radar/charts_web.py`) | 32 (4 genres x 8; the default top-grossing list does not exist on the web) | robots.txt allows the chart pages (it lists chart sitemaps); the page's own continuation API (`/v1/*`) is disallowed and never called. No top-grossing and no new-games chart |
| Apple legacy RSS `itunes.apple.com/{cc}/rss/{chart}/limit={<=100}/genre={id}/json` (`chart_source: rss`) | top free / grossing lists per genre | **replay only** | `itunes.apple.com/robots.txt` disallows `/*/rss/*` (found 14 Nov 2026): the radar refuses `rss` outside `--replay` of recorded fixtures. `newfreeapplications` ignores `limit` and `genre` |
| iTunes Lookup `itunes.apple.com/lookup?id=a,b,...&country=cc` | release date, rating count per storefront, version and its date, description, genres | ~28 (100 ids per request) + rechecks (<= 2 per storefront) + 1 per storefront for the watchlist | documented, ~20 calls/minute; answers come from Apple caches of different ages (see below) |
| Lookup `?id={artistId}&entity=software` | a watched publisher's whole catalogue | weekly | |
| Google Play `play.google.com/store/apps/details?id=...` | install bucket, `realInstalls` (undocumented page field), Play tags, ads/IAP flags, description | <= 200: new-game candidates, watched games, the calibration panel (`estimates.panel_size`, 80) | robots.txt allows this path and nothing else the radar uses |

Polite fetching (`radar/http.py`), enforced in code and tested:
- robots.txt is read (RFC 9309: longest match, `*` and `$`) before the first Play request;
  a disallowed URL is refused before anything is sent. `/_/` (batchexecute: top charts,
  reviews, permissions) and `/store/search` are refused even without a robots.txt.
- At least 3 s between requests to one host, a daily budget per host (Play at most 200; the
  budget counts earlier runs of the same day), an optional cap per run (`--max-requests`).
- 429/503: wait `Retry-After` and retry; a repeated 429/403 stops that host for the run.
- A plain `requests` session with an identifying User-Agent: no proxies, no rotating
  identities, no browser impersonation. No login anywhere.
- The raw Play page is never stored; parsed fields are, without the developer's e-mail,
  address, website or privacy-policy URL. Apple responses are cached per day for 3 days.
- Every request (cached or not) is a row in `raw_mr_request`.

Store text, screenshots and anything derived from them stay local: the radar refuses an
`out_dir` inside `docs/demo` (the public demo), and the dashboard never shows radar data.

## Unreliable single reads: rechecks and the maximum

One Lookup answer is not a measurement. On 12 Nov 2026 Apple's US Lookup gave UnGodly 0
ratings and, minutes later, 130 (four single calls and a batched one agreed on 130), and
Foodie Pop! 1,846 in a batched Lookup but 1,760 in the publisher catalogue the same hour.
So every rating-count read of the day is kept in `raw_mr_lookup_read` (`batch`, `recheck1`,
`recheck2`, `publisher`), and after the batched Lookup and the publisher catalogue:

- an app whose count is 0 or missing, lower than its previous snapshot, or different between
  today's reads is re-queried (batched, through the same polite fetcher and budget, never from
  the day cache): a disagreement once, a zero or a drop up to twice;
- `raw_mr_app` keeps the maximum read of the day (counts only grow within a version);
- the recheck's ledger row (`raw_mr_request.note`) lists the reads that differed, and the
  `radar_lookup_disagreement` warning shows them in the digest;
- a count below the window's base is a rating reset only when the previous snapshot was lower
  too (it persisted across runs); a one-day drop is `unconfirmed_drop` (warning
  `radar_rating_drop_unconfirmed`). Neither feeds the velocity score.

## Tables and models

Raw (`radar/schema.sql`): `raw_mr_chart` (one row per day, country, list and rank),
`raw_mr_app` (Lookup rows per storefront, Play rows), `raw_mr_xref` (iOS app -> Play package:
`mapped`, `not_found` retried after 14 days, `manual_none`), `raw_mr_llm`, `raw_mr_profile`
(validated claims), `raw_mr_request`, `raw_mr_params`, `raw_mr_config`, `raw_mr_run`,
`raw_mr_check`. Models (`radar/models/*.sql`, run by the same runner as `models/`):

| Model | Grain | What |
|---|---|---|
| `stg_mr_chart` | day x list x rank | ranks within the requested length; games lists keep only games |
| `stg_mr_list_days` | day x list | which list was observed (a missing day is unknown, not empty) |
| `stg_mr_rating_daily` | day x storefront x app | the day's maximum read and ratings/day over the latest earlier snapshot 3-7 days back; a drop is a `rating_reset` only when it persisted to a second snapshot, else `unconfirmed_drop` (velocity NULL either way); no history: lifetime average, labelled |
| `stg_mr_install_daily` | day x Play package | bucket, `realInstalls`, installs/day (same window, else since the Play release), bucket change |
| `fct_mr_breakout_daily` | day x charted app | every component, the score, the gates and the class |
| `fct_mr_app_tags` | app x claim | the profile used: newest LLM profile, else keyword profile; store-derived monetisation |
| `fct_mr_mechanic_weekly` / `fct_mr_trend_weekly` | week x country x mechanic | chart share, the 4-week baseline, z and the trend flag |

Market estimates and history (`radar/estimate/schema.sql`, `radar/estimate/models/`):
`stg_mr_rank_input` / `stg_mr_rank_lists` (the estimates' only chart input: the RSS lists plus
`raw_mr_rank_extra`), `raw_mr_watch` (watchlist), `raw_mr_est_window` (Play counter windows with status),
`raw_mr_est_exposure` (a window's mean rank per list), `raw_mr_est_fit` (one curve per list and
kind, status ok / insufficient / no_source), `raw_mr_est_backtest` (out-of-sample accuracy),
`raw_mr_est_game` (the day's bands per game), `stg_mr_play_counter` (day x package: refreshed
or stale counter) and `fct_mr_version_timeline` (app x store x storefront x version).

## The breakout score

Notation: rank r in a list of length L (r̃ = L+1 when observed but not listed), day t.

```
climb_7  = log2(r̃_{t-7} / r̃_t)                 (best list; NULL when the list was not observed at t-7)
age      = t - min(release date over all storefronts' Lookup, first chart sighting)
rv       = ratings/day per storefront (3-7 day window; lifetime average without history)
iv       = Play realInstalls/day (same window; lifetime average without history)
spread   = countries where the app was in any watched list in the last 7 days

s_rank   = 1 - ln(r_best) / ln(L+1)
s_climb  = clip(climb_7 / 3, 0, 1)
s_vel    = max(clip(log10(1+rv_max)/log10(201)), clip(log10(1+iv)/log10(50001)))
s_spread = clip((spread - 1) / 4, 0, 1)
s_new    = 1 if first seen <= 7 days ago (and the list was observed before that), or released
           <= 7 days ago per Lookup; else 0

M = 0.40 s_vel + 0.25 s_climb + 0.15 s_rank + 0.10 s_spread + 0.10 s_new
```

Gates (all needed): age <= 60, listed today, and a signal (s_new = 1, climb_7 >= 1,
rv >= 10/day or iv >= 1,000/day). Classes: **breakout / YÜKSELİYOR** M >= 0.35, **watch /
İZLE** 0.20 <= M < 0.35, **late riser / GEÇ YÜKSELEN** 60 < age <= 120 and M >= 0.35. Every
weight, saturation point and threshold is a `radar.params` key. The first day of the radar
cannot say what is new (no earlier sighting): first sightings on that day are censored and
only a Lookup release within 7 days counts as new.

Release dates differ by storefront (a soft launch): Police Chief was released on 8 May 2026 in
the US storefront and on 24 Sep 2026 in the Turkish one, so the age uses the earliest date.

## Weekly mechanic trend

For mechanic m and country c in the trend list (default: top free Games):
share = sum over the week's days of charted apps tagged m / charted rows;
Δ = share − mean(share of the 4 previous weeks); z = Δ / sqrt(p(1−p)/rows) with p that mean
(0.5/rows when it is 0). TREND: 4 previous weeks, Δ >= 2 points, z >= 2 and at least 3
distinct apps. Until there are 5 weeks of snapshots the digest shows the current shares only.
`tagged_share` says how much of the list has a profile at all.

## Labels: every number carries its basis

| basis | Turkish | English | What |
|---|---|---|---|
| `observed` | GÖZLEM | OBSERVED | read from a chart or Lookup, or computed only from them |
| `store_reported` | MAĞAZA VERİSİ | STORE-REPORTED | Play page fields (bucket, `realInstalls`) and what is computed from them, including a score whose velocity came from Play |
| `estimate` | TAHMİN | ESTIMATE | always `low`, `high`, `method`, `n`; the renderer refuses an estimate without them |

Estimates: iOS daily installs ≈ rating velocity ÷ Play ratings-per-install, as the range of
that ratio over the day's Play pages (n of them, at least 3), and the rank-based download and
revenue bands of "Market estimates" below. `digest.check_labels` fails the run if a market
number lacks a basis or an estimate lacks its range, method or n.

## Classification with grounding

`radar/classify.py` sends the stored store text (Apple name, description, genres; Play
description, summary, tags) as untrusted JSON data to the configured LLM with a JSON schema
built from `radar/taxonomy.yaml` (GameRefinery's public puzzle subgenres plus sort, screw, jam
and block; twists, meta layers, themes, monetisation features). A deterministic verifier keeps
a claim only when its evidence quote is found verbatim in the named field (case, spacing and
quote marks normalized), the value is in the taxonomy and no LLM-written text contains a number
(mechanic names such as match-3 excepted). Rejected claims are stored with the reason. The
monetisation model (hybrid, IAP, ads, premium) comes from store fields, never from the model.
A result is cached per (app, input hash, prompt version, model). Apps without an LLM profile get
a keyword profile (whole-word taxonomy keywords, the match as the quote), so weekly trends cover
the whole chart at no cost.

Optional narration rephrases the facts; `verify_narration` extracts every number (Turkish and
English separators, k/bin/million units) and drops the narration if one is not in the facts.

## LLM provider layer (`studio_pulse/llm`)

| Provider | `llm.provider` | Configuration (environment wins over the config) |
|---|---|---|
| Gemini on Vertex AI | `gemini` | ADC (`gcloud auth application-default login`), `PULSE_LLM_PROJECT` (or `GOOGLE_CLOUD_PROJECT`), `PULSE_LLM_LOCATION` (default `global`), model default `gemini-3.5-flash-lite` |
| OpenAI-compatible | `openai` | `PULSE_LLM_BASE_URL`, `PULSE_LLM_MODEL`, optional `PULSE_LLM_API_KEY`, `PULSE_LLM_JSON_MODE` (`json_schema`, `json_object`, `none`). llama.cpp `llama-server` is the recommended local server (json_schema output, GGUF LoRA adapters via `--lora` without merging) |
| Anthropic Claude | `anthropic` | `ANTHROPIC_API_KEY`, `pip install 'studio-pulse[anthropic]'`; JSON via `output_config.format`, never a forced `tool_choice` |
| Fake | `fake` | deterministic, offline (tests, CI) |

`LLMClient`: content-hash cache, per-run `budget_usd` checked before each request against the
worst case (a model without a price is refused while a budget is set; prices in
`llm/pricing.py`, overridable under `llm.prices`), a JSONL ledger of every attempt (tokens,
cost, cached, repaired), one repair request for invalid JSON, retries with backoff for 429/5xx
and timeouts, and secrets redacted from errors, ledgers and caches.

## Countries and request volume

Default storefronts: `us, gb, de, fr, br, tr, jp, kr` (unchanged); any two-letter App Store
storefront code can be added, for example `ca` and `au`. `pulse radar plan` prints the
expected volume for a config and exits 1 if it does not fit the budgets; the config is refused
outright when the lists plus one Lookup per country already exceed `radar.apple.daily_budget`.

| Host | Default: 8 countries x 5 lists of 100 | With `ca`, `au` (10 x 5) | Budget | Spacing |
|---|---|---|---|---|
| apps.apple.com, chart pages (robots.txt checked first) | 32 (one page per genre gives its free and paid lists; no grossing list on the web) | 40 | 60 a day (`apple.web_daily_budget`) | >= 3 s |
| itunes.apple.com, Lookup | ~28 (250-320 distinct apps per storefront, 100 ids a request) + up to 16 rechecks + 8 for the watchlist | ~35 + 20 + 10 | 120 a day (`apple.daily_budget`; raise it to ~150 for 10 countries with a watchlist) | >= 3 s |
| play.google.com | robots.txt + new-game candidates (~50) + watched games + calibration panel (`estimates.panel_size`, 80): at most 200 | same | 200, hard cap (`play.daily_budget` above 200 is lowered to 200) | >= 3 s |

At 3 s per request a full day takes about 10 minutes of wall time. The panel only uses Play
budget the radar's own candidates left, and keeps `estimates.play_reserve` (10) for a re-run.

Chart source. `itunes.apple.com/robots.txt` disallows `/*/rss/*` for every user agent (found
14 Nov 2026; the Lookup path is not matched), so the radar reads Apple's charts from the App
Store web pages, which robots.txt allows, and accepts the legacy RSS only to replay recorded
fixtures. Apple's current feed (`rss.marketingtools.apple.com`) has no games or genre lists.
The web pages have no top-grossing chart, so no revenue estimate depends on a grossing rank
(below). The estimates read ranks only from `stg_mr_rank_input` (day, country, list,
`list_kind` free / paid / grossing, `all_games`, rank, app, source), which is the radar's
Apple lists plus `raw_mr_rank_extra`, a table any other chart source can fill
(`tests/test_radar_estimates.py` calibrates from it with no Apple row at all).

## Market estimates

Every number below is labelled TAHMİN / ESTIMATE and carries `low`, `high` (an 80% band by
default), its middle, the method and n; `digest.check_labels` refuses one without them. A
list or country without enough data gets no number: the digest and the game page say
"yetersiz veri / insufficient data" (downloads) or "kamu kaynağı yok / no public source"
(revenue) instead. Code: `radar/estimate/`.

### Downloads: rank -> installs per day, calibrated on our own Play reads

**Ground truth.** Google Play's details page carries `realInstalls`, one worldwide counter per
package. It is refreshed irregularly. Between the radar's reads of 13 and 14 Nov 2026 (29 hours
apart) 18 of 30 packages kept exactly the same count, 13 of them while their Play rating count
grew (one gained 644 ratings and 0 installs), and four of the 12 that changed moved by only
26-87 installs, two of them while gaining 1,700-2,000 ratings. A day-over-day difference
therefore measures the counter's refresh, not the installs, and is never used. Instead
(`estimate/downloads.py`, `stg_mr_play_counter`, `raw_mr_est_window`):

- a **window** runs between two observed refreshes (reads where the count changed), at least
  `min_window_days` (7) and at most `max_window_days` (35) apart; its rate is the counter's
  increase over the elapsed time (installs/day, worldwide, Android);
- a window whose Play rating count grew by more than `max_ratings_per_install` (0.25) per new
  install is `implausible` (a counter lagging its ratings) and is not used; a counter that went
  down restarts the window;
- the window's chart exposure is the geometric mean rank of the game in each top-free list
  over the window's days (a day the list was observed but the game was not in it counts as
  list length + 1); a list counts when the game was in it on at least half of those days.
  Apple ids map to Play packages through the radar's verified mapping (`raw_mr_xref`), else
  the same bundle id.

To get windows across the whole rank range, the radar reads the Play pages of a
**calibration panel** every day: up to `panel_size` charted games, round-robin over rank bands
1-10, 11-25, 26-50, 51-100 of the top-free lists, games already in the panel first. Bundle ids
that cannot be Android application ids (a `-`, a segment starting with a digit, an `ios`
segment) are not tried.

**Model.** Per top-free list (country x genre):
`log(installs/day) = a - b log(mean rank)`, least squares, with a cluster bootstrap over games
(400 replicates; one game's windows are not independent). The band is a prediction band: each
replicate's curve plus one of its own residuals, so it carries the game-to-game scatter, not
only the uncertainty of the curve. A list needs `min_games` (10) distinct games with windows
and ranks spanning at least a factor of 2, else status `insufficient`.

What the curve says: "games at this rank in this country's iPhone top-free list get this many
Android installs a day **worldwide**" (Play publishes no per-country count, so no per-country
download number is printed). iOS downloads are that times the iOS:Android download ratio of the
countries where the game charts today (`estimate/ios_android.yaml`: StatCounter usage share
per country, Oct 2026 and the 12-month mean, times download/usage factors 0.58-1.17 from the
three places where both are published; Sensor Tower's store split of game downloads for JP
and KR). The band uses the extremes, so a game charting in Turkey and the US gets a ratio of
0.16-2.26: that conversion is public data our own reads cannot check, and the band says so.

### Revenue: from downloads, not from a rank

No compliant source publishes a top-grossing rank: apps.apple.com has no grossing chart and
the legacy iTunes RSS is disallowed by robots.txt. So revenue is estimated from a game's
download band (`estimate/rpd.py`, sources in `estimate/revenue_per_download.yaml`, each with
URL and date):

```
revenue/day = Android installs/day x RPD_android(g) + iOS downloads/day x RPD_ios(g)
            + (Android + iOS installs/day) x ad revenue per install      (if it shows ads)
```

- **RPD** (revenue per download = in-app purchase revenue / downloads of the same period) of
  the game's genre g, worldwide 2025, both stores (AppMagic Mobile Market Landscape 2026):
  RPG 6.9, strategy 5.1, casino 4.1, shooter 1.10, sports 0.81, puzzle 0.79, simulation 0.44,
  action 0.18, arcade 0.10 USD, net of store fees, grossed up by 1/0.85-1/0.70; match-3 6.85,
  merge 2.96 and tile-match ("Match 3D") 3.94 when the radar's profile names that mechanic
  (AppMagic casual report H1 2025). The genre comes from the Lookup genres (first match:
  Role Playing, Strategy, Casino, Sports, Racing, Puzzle, Word, Board, Card, Trivia, Simulation,
  Action, Adventure, Arcade, Casual); none of them: the all-games $1.62 (Sensor Tower 2025).
- **Store split**: like the whole market, an App Store download earns 4.15x and a Google Play
  download 0.44x the two-store average (Sensor Tower 2025: $52.5B / 7.8B and $30B / 42.4B).
- **One game vs its genre**: anywhere from a third to three times the genre average (five-fold
  for an unknown genre). No public source gives that spread: it is a stated assumption.
- **Ads**: AppsFlyer's D90 ad revenue per install (casual $0.55, casino $0.47, midcore $0.40,
  hypercasual $0.22; Jan 2025-Mar 2026) to twice that for the lifetime, added when the Play page
  says the game has ads, scaled by a uniform 0-1 when that is unknown, left out when it has none.

RPD is a steady-state ratio (old players' spending is on the revenue side), not what a new
install pays; the result is gross consumer spend plus ad revenue, worldwide, per day. A game
without a download band gets no revenue band. Nothing of this could be checked against a
game's real revenue yet: the download curves are not calibrated (below).

The earlier grossing-rank model (`estimate/revenue.py`, anchors in `revenue_anchors.yaml`:
Roblox, Playtika titles, Lineage M, with published rank-revenue exponents) stays in the code
but is **inactive**: it runs only when a compliant grossing list reaches `stg_mr_rank_input`
(`raw_mr_rank_extra`), never on the legacy RSS. Its US anchor ranks were read from the legacy
RSS on 13-14 Nov 2026, before the robots finding, and must be re-observed in a compliant
source before use.

### Data-quality checks of the estimates

`radar/estimate/checks.yaml`, run with the radar's own checks: a watchlist entry at most once,
a usable window has a positive rate, every band has low <= middle <= high with basis, method
and n, and download bands only for lists whose curve is `ok` (errors); watched games not read
today, a list that lost its download curve since the previous computation, a curve whose
held-out windows fell inside its band less than 60% of the time, a day where nine in ten Play
counters were not refreshed (the field may have stopped updating), and a version date after its
first sighting (warnings). Lists that were never calibrated are listed in the digest's
Estimates section, not as a daily warning.

## How accurate is this

What ran where (14 Nov 2026):

| Part | Data | Result |
|---|---|---|
| Download curves, live | the radar's own Play reads: 2 days (13-14 Nov), one 29-hour interval | **no curve**: 0 windows of 7+ days, every top-free list `insufficient`, so no download number and no revenue number is shown. With the panel (80 games a day) the first lists should reach 10 games after ~2-3 weeks of daily runs |
| Download method, synthetic | `tests/radar_synth.py`: 140 invented games, 35 days, true slope 0.8, counters refreshed every 1-6 days, 12% frozen, 6% lagging (+20-90 installs per refresh while ratings grow) | 412 windows, 20 dropped as implausible. US list: leave-one-game-out MAPE 26%, 79% of held-out windows inside the 80% band (n=280); time split (fit on the first 70% of windows) MAPE 27%, 79% inside (n=84); slope recovered 0.77 (0.68-0.85). TR list (installs driven mostly by US ranks, so a flat curve): 37% / 80% and 39% / 81% |
| Revenue from downloads | public RPD and ad-ARPU benchmarks | **not checked**: it needs download bands, and none exists yet; when they do, the disclosed anchors of `revenue_anchors.yaml` (Roblox, Playtika titles) are the first check to run |
| Grossing-rank model (inactive) | ranks from the legacy RSS (13-14 Nov, before the robots finding) | computed once on 14 Nov before the switch: leave one US anchor out MAPE 27%, 3 of 3 inside; Lineage M held out (KR) inside, 29% off; the curve summed over ranks 1-1000 was 2.1x the public US iOS market (fails). Not used: no compliant grossing source |

Live checks (real network, scratch configs, outputs kept local): on 14 Nov 2026 one run on the
legacy RSS (`--max-requests 75`, before the robots finding: 68 Apple and 7 Play requests) and,
after the switch, one on the apps.apple.com pages (`--max-requests 45`, 4 storefronts, the
default lists, empty warehouse): 17 apps.apple.com requests (robots.txt and 16 chart pages,
one per storefront and genre), 21 Lookups, 7 Play requests; 1,600 chart rows (16 lists of 100,
every name filled from the Lookup), the top-grossing list skipped with a note, a watched game
looked up where it is not charted, 20 breakout/watch candidates, no estimate band (no Play
history). The panel's selection fix and the grossing-model gate were applied after that run
and re-computed on its warehouse without new requests.

MAPE here is the median |middle - actual| / actual of held-out cases; "inside" is the share of
held-out cases inside the band (nominal 80%). The game page and the digest show the same
numbers from the latest run (`raw_mr_est_backtest`, `data = live`); the synthetic numbers
come from `pytest tests/test_radar_estimates.py` (seed 11) and only show that the method
recovers a known curve with honest bands; they are not a claim about real games.

Known limits: realInstalls is an undocumented field and may change meaning; the iOS
conversion and every revenue step rely on public figures from different years and sources;
revenue per download is a market-wide ratio applied to one game, with a stated, unmeasured
spread; the apps.apple.com page is undocumented and its layout may change (the parser fails
loudly: `ChartPageError`); its ranks 26-200 are ids only until the Lookup names them.

## Game history and the watchlist

Kept per game, daily: ranks in every watched list (`stg_mr_chart`), App Store rating count
and velocity per storefront (`stg_mr_rating_daily`), the Play counter with refresh flags
(`stg_mr_play_counter`), versions with the store's own date and Apple release notes
(`fct_mr_version_timeline`, from Lookup `version` / `currentVersionReleaseDate` and Play
`updated`), and the day's estimate bands (`raw_mr_est_game`).

`pulse radar watch add|remove|list <apple id | play package | store URL>` keeps
`raw_mr_watch`; a removed game stays as history. A watched Apple id is looked up in every
configured storefront each day (one batched Lookup per country, rows with `source = 'watch'`)
even when it is not charted; a watched package (or a watched Apple id with a known Play
package) has its Play page read daily, ahead of the panel.

`pulse serve` -> `/radar/oyun/<apple id | play package>` (linked from the radar table): rank
timeline per country (one line per list, log scale, hover for date and rank), the day's bands
with method, n, calibration status and out-of-sample accuracy, the version timeline, rating
velocity and the Play counter with its usable windows. Turkish first (`?lang=en`). Like the
rest of the radar it reads the local warehouse only and is never written to the public demo.

## Configuration

```yaml
radar:
  enabled: true
  countries: [us, gb, de, fr, br, tr, jp, kr]   # default; add ca, au ... (`pulse radar plan`)
  charts:                      # default: these five
    - {chart: topfreeapplications, genre: "6014"}
    - {chart: topfreeapplications, genre: "7012"}
    - {chart: topfreeapplications, genre: "7003"}
    - {chart: topfreeapplications, genre: "7015"}
    - {chart: topgrossingapplications, genre: "6014"}
  publishers: [{id: "1743349083", name: ABI Global}]
  apple: {chart_source: web, spacing_s: 3, daily_budget: 120, web_daily_budget: 60,
          lookup_batch: 100}   # chart_source rss: only with --replay of recorded fixtures
  play: {country: us, lang: en, spacing_s: 3, daily_budget: 200, map_file: play_map.yaml,
         watch: [com.example.package]}
  estimates:                   # defaults shown; `enabled: false` keeps only the watchlist
    enabled: true
    min_games: 10              # games with Play windows a list needs for a download curve
    min_window_days: 7
    max_window_days: 35
    max_ratings_per_install: 0.25
    bootstrap: 400
    draws: 2000                # Monte Carlo draws of the revenue band
    band: [0.1, 0.9]
    panel_size: 80             # charted games whose Play page is read daily
    play_reserve: 10
  llm: {max_apps: 25, screenshots: 0, narrate: false}
  params: {breakout_min: 0.35}
  out_dir: local/radar
  creative_forge_library: ~/Library/Application Support/creative-forge/library
llm:
  provider: gemini
  budget_usd: 0.50
```

`play_map.yaml` (`apple_to_play: {"<apple id>": <package or null>}`) overrides the bundleId
mapping.

## market_trends.yaml (studio-pulse/market_trends/v1)

Written to `out_dir` and, when set, to `radar.creative_forge_library`. creative-forge's
`forge ideate` loads it from `<data dir>/library/market_trends.yaml` (or
`CREATIVE_FORGE_MARKET_TRENDS`) as library entries of kind `market_trend`.

| Key | Type | Meaning |
|---|---|---|
| `version`, `schema` | 1, `studio-pulse/market_trends/v1` | |
| `generated_at`, `as_of` | ISO time, date | |
| `countries` | list | storefronts in the run |
| `lessons[]` | list | creative-forge reads `id`, `title`, `text` |
| `lessons[].kind` | `market_trend` | |
| `lessons[].mechanic` / `mechanic_shown` / `gamerefinery` | strings | radar value / creative-forge tag value / GameRefinery subgenre |
| `lessons[].themes`, `twists`, `hooks` | lists | hooks are a suggestion (inference) from the taxonomy |
| `lessons[].apps[]` | `{app_key, name, class}` | |
| `lessons[].tags` | `llm` / `keyword` / `mixed` | how the mechanic was assigned: an LLM claim with a quote checked against the store text, or a keyword guess; the text says so per game, and a keyword-only lesson's title ends "(keyword tags only)" |
| `lessons[].evidence` | list | `rss:{cc}:{chart}:{genre}#{rank}@{day}`, `lookup:{cc}:{id}@{day}`, `play:{package}@{day}`, `trend:{cc}:{mechanic}@{week}` |
| `lessons[].signal` | `daily_breakout` or `weekly_trend` | |
| `lessons[].basis` | map | label of each kind of number in `text` |

## Data-quality checks (`radar/checks.yaml`)

Rank unique per list and day and app id present (error); robots compliance of every Play
request and the 200-a-day Play budget (error); a stale snapshot, a frozen feed timestamp
(36 h), a feed over its limit or with non-games in a games list, missing countries, short
lists, Lookup coverage below 90 %, disagreeing Lookup reads, unconfirmed drops, confirmed
rating resets, `realInstalls >= minInstalls` and Play field fill below 95 % (warnings).
