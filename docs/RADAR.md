# Market radar

A daily scan of public store charts: which new games are rising, which mechanics are gaining
chart share, and what that suggests for the studio's next creative tests. Output: a Turkish and
an English digest, a local page in `pulse serve` and `market_trends.yaml` for creative-forge.

```bash
pulse radar run -c pulse.yaml                    # today's snapshot, models, digest
pulse radar run --replay tests/fixtures/radar/2026-11-12   # recorded responses, no network
pulse radar show -c pulse.yaml --lang en         # print the newest digest
pulse serve -c pulse.yaml                        # /radar: "Pazar Radarı / Market Radar"
```

With `radar.enabled: true` the radar also runs inside every `pulse run`, so `pulse schedule
install` makes it daily; its data-quality checks are stored with the studio's own.

## Sources and rules

| Source | What | Volume per day (8 countries x 5 lists) | Status |
|---|---|---|---|
| Apple legacy RSS `itunes.apple.com/{cc}/rss/{chart}/limit={<=100}/genre={id}/json` | top free / grossing lists per genre (6014 Games, 7012 Puzzle, 7003 Casual, 7015 Simulation ...) | 40 | undocumented; the new RSS v2 has no games list (its games endpoint answers 404), so the legacy one is the only Apple list with genres. `newfreeapplications` ignores `limit` and `genre`: extra rows and non-games are kept raw but not scored |
| iTunes Lookup `itunes.apple.com/lookup?id=a,b,...&country=cc` | release date, rating count per storefront, description, genres | ~16 (100 ids per request) + rechecks | documented, ~20 calls/minute; answers come from Apple caches of different ages (see below) |
| Lookup `?id={artistId}&entity=software` | a watched publisher's whole catalogue | weekly | |
| Google Play `play.google.com/store/apps/details?id=...` | install bucket, `realInstalls` (undocumented page field), Play tags, ads/IAP flags, description | <= 200 | robots.txt allows this path and nothing else the radar uses |

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

The only estimate in v1: iOS daily installs ≈ rating velocity ÷ Play ratings-per-install, as
the range of that ratio over the day's Play pages (n of them, at least 3). No rank-to-downloads
estimate is shown. `digest.check_labels` fails the run if a market number lacks a basis.

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

## Configuration

```yaml
radar:
  enabled: true
  countries: [us, gb, de, fr, br, tr, jp, kr]
  charts:                      # default: these five
    - {chart: topfreeapplications, genre: "6014"}
    - {chart: topfreeapplications, genre: "7012"}
    - {chart: topfreeapplications, genre: "7003"}
    - {chart: topfreeapplications, genre: "7015"}
    - {chart: topgrossingapplications, genre: "6014"}
  publishers: [{id: "1743349083", name: ABI Global}]
  apple: {spacing_s: 3, daily_budget: 120, lookup_batch: 100}
  play: {country: us, lang: en, spacing_s: 3, daily_budget: 200, map_file: play_map.yaml,
         watch: [com.example.package]}
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

## Game dossier (Oyun dosyası)

For one game seen rising: what it does, what it does right, and whether the studio should make
something like it.

```bash
pulse radar dossier 6812166638 -c pulse.yaml            # Apple id (or an App Store URL)
pulse radar dossier com.example.game -c pulse.yaml --lang en   # or a Play package
pulse radar dossier 6812166638 -c pulse.yaml --replay tests/fixtures/radar/dossier --no-llm --no-ads
pulse serve -c pulse.yaml                              # /radar/dosya and /radar/dosya/<key>
```

Output in `<radar out_dir>/dossier/<key>/` (`apple-<id>` or `play-<package>`): `<day>.tr.md`,
`<day>.en.md`, `<day>.facts.json` (every number with a label, checked like the digest) and
`<day>.market_trends.yaml`. Screenshots go to `<state_dir>/radar-dossier/<key>/` only. The
dossier refuses an out_dir inside `docs/demo`. `--forge` merges the first-test lesson into
creative-forge's `market_trends.yaml` (the radar rewrites that file on its next run).

### What it collects, and why each source is allowed (checked 14 Nov 2026)

| Source | What | Rule |
|---|---|---|
| iTunes Lookup `/lookup?id=` per storefront | texts, rating count/average, version and dates, screenshot URLs, seller | robots.txt of itunes.apple.com is now read before any iTunes request; `Disallow: /*/lookup?` needs a path segment before `/lookup`, so `/lookup?id=` is allowed |
| Play details page (the radar's own fetcher) | install bucket, `realInstalls`, rating, ads/IAP flags, tags, **the reviews embedded in the page** | robots.txt allows `/store/apps/details`; the page carries the 20 "most relevant" reviews of the page language in an `AF_initDataCallback` block, parsed from the same page. The review endpoints (`/_/`, `/store/getreviews`) stay refused |
| Apple customer-reviews feed `itunes.apple.com/{cc}/rss/customerreviews/...` | recent reviews | **refused**: itunes.apple.com/robots.txt has `Disallow: /*/rss/*`. The dossier asks the fetcher, which refuses before sending; the parser is kept and tested on the recorded shape. apps.apple.com pages are not used either (Apple's site terms forbid page-scraping) |
| Screenshot CDNs (`is*-ssl.mzstatic.com`, `play-lh.googleusercontent.com`) | up to `screenshots` images (default 6), from the Lookup or Play URLs | no robots.txt (403/400, which RFC 9309 §2.3.1.3 treats as allow-all); spaced like every request; cached locally, never published |
| The radar's warehouse | chart positions, rating reads, versions seen, breakout row, tags, weekly mechanic shares | no request |

Reviewer names, ids and avatars are never kept: a review is its stars, text, date, app version and
whether the developer replied.

Open question for the radar itself: its chart feeds (`itunes.apple.com/{cc}/rss/...`) fall under
the same `Disallow: /*/rss/*` line, and `pulse radar run` checks robots.txt only for Play today.

### LLM deconstruction (grounded)

One multimodal request (store texts as untrusted JSON plus screenshots s1..sN) through the
provider layer. The model writes short claims, in English and Turkish, in seven sections (core
loop, meta layer, monetisation, art style, onboarding, LiveOps signals, "Neyi doğru yapıyor? /
what it does right") and picks radar taxonomy mechanics; mechanics the taxonomy lacks go to
`proposed_tags`, listed separately. The verifier follows `classify.py`: a text evidence item is
kept only if its quote is found verbatim in the named field and holds no number; a screenshot
item only if that screenshot was sent (what it shows cannot be checked by code, so such claims are
labelled visual inference); a claim only with at least one kept item and no number in its text.
Dropped claims are listed with the reason. The check proves the quote exists, not that it
supports the claim: in the first live run the model cited "Colorful flowers, ropes, and
satisfying sorting animations" for a 3D art style.

Vertex AI rejects response schemas whose object arrays carry `maxItems` above five ("Request
contains an invalid argument"), so the dossier schemas have no length limits; the verifier
truncates instead.

### Review mining

The LLM labels each Play review with `(side, theme, quote)` from a fixed theme list
(`benchmarks.yaml`, `review_themes`); each label needs its own verbatim quote from that review.
Counts are computed by code from the kept labels. The 20 reviews are Play's "most relevant" for
the page language, not a random sample: the dossier says so, and when their mean differs from the
store average by 0.75 stars or more it prints both (Royal Match: 1.65 vs 4.63 on 14 Nov 2026).

### "Yapmalı mıyız? / Should we make it?"

Six factors, each a deterministic score in [0, 1] with its inputs and labels
(`radar/dossier/rubric.py`):

| Factor | Default weight | Inputs |
|---|---|---|
| momentum | 0.20 | radar class (+0.2 breakout, +0.1 watch or late riser) and the mechanic's weekly trend (Δ / 3 points × 0.3, at least 0.7 on a TREND); neutral until 4 previous weeks exist |
| saturation | 0.15 | similar games (same mechanic tag) in today's lists, how many are in a grossing list, top-3 publishers' share of their rows |
| monetization_fit | 0.15 | store ads/IAP flags (hybrid, ads, iap, premium) against the studio's models; features the studio avoids |
| capability_fit | 0.25 | a shipped mechanic 1.0, our genre 0.75, outside 0.3 (unknown mechanic: the store genre decides, 0.55 / 0.2); art style and avoided features subtract |
| production | 0.15 | person-months = base(mechanic) × (1 + adders for meta layers, LiveOps, 3D art, PvP) ± 35 % (a studio assumption, `benchmarks.yaml` effort) against team_size × months |
| cpi_risk | 0.10 | AppsFlyer Q3 2026 CPI of the genre in the launch market and platform against `max_cpi_usd` |

`total = Σ w·s / Σ w`. Gates (YAPMA): the production estimate's low end over capacity,
capability below 0.25, a gameplay feature on the avoid list. Caps (at most BEKLE): unknown
mechanic, no deconstruction, benchmark CPI at least `cpi_cap_ratio` (2) × `max_cpi_usd`. Else
YAP at `yap_min` (0.62), BEKLE at `bekle_min` (0.45), YAPMA below. The three reasons are the
factors furthest from neutral after weighting (gates first); the assumptions are listed with the
verdict. Retention is not scored (a competitor's is not observable): the GameAnalytics 2026
bands (`benchmarks.py`) are the gate of the first test's next step.

CPI benchmarks: AppsFlyer, "Gaming app market benchmarks for Q3 2026" (5 Nov 2026), genre ×
country × platform, transcribed into `radar/dossier/benchmarks.yaml` with the URL; the
mechanic-to-genre mapping is an approximation (AppsFlyer does not publish its definitions).

### First test ("İlk test") and creative-forge

A creative fake-door test: `variants` (4) video variants with the taxonomy's hooks for the
mechanic, in `test_market` (BR) on `test_platform` (Android) for `days` (4); budget = variants ×
`installs_per_variant` (40) × the benchmark CPI there (high end × 1.2), at least
`min_per_variant_usd` (25) each, labelled ESTIMATE; pass gate IPM ≥ `ipm_gate` (8, a studio
rule) and CPI ≤ the benchmark. Exported as a `studio-pulse/market_trends/v1` document with one
lesson (`id: DT-<day>-<key>`, `kind: market_trend`, `signal: dossier_first_test`, plus a
`test:` block), which `forge ideate` reads like the radar's lessons.

### Competitor ads ("Rakip reklamları")

**Google Ads Transparency Center** (`bigquery-public-data.google_ads_transparency_center`,
verified through the BigQuery API and `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`): tables
`creative_stats` (~164 M rows, ~141 GB, not partitioned, not clustered) and
`removed_creative_stats`. One row per creative: `advertiser_id`, `advertiser_disclosed_name`,
`advertiser_legal_name`, `ad_format_type`, `advertiser_location`, `topic`, `region_stats`
(REPEATED: `region_code`, `first_shown`, `last_shown`, `times_shown_*`, `surface_serving_stats`)
and audience fields. A query is billed for the full columns it names: advertiser_id 3.93 GB,
advertiser_disclosed_name 3.81 GB, ad_format_type 1.04 GB, region_code 2.25 GB, first_shown and
last_shown 6.27 GB each. Every column that identifies an advertiser is over 2 GB, and
`TABLESAMPLE` does not help: BigQuery checks `maximum_bytes_billed` against the unsampled size.

`radar/adintel/google_atc.py` checks the expected columns in INFORMATION_SCHEMA once a day (10 MB
billed, BigQuery's minimum), builds one query for all advertiser names of the run (names folded to
`[a-z0-9 ]`, generic words dropped, so nothing else reaches the SQL text), dry-runs it, refuses it
when the estimate is over the cap and otherwise runs it with `maximum_bytes_billed` set
(`BigQueryExecutor`). The exact per-advertiser query (creatives, formats, regions, first and last
shown) is estimated at 23.6 GB: with the default `max_gb: 2` it is refused and the dossier says
so. To run it, raise `radar.dossier.ads.max_gb` and `max_bytes_billed` knowingly (it falls in
BigQuery's free 1 TiB a month on most projects; after that about 0.15 USD at 6.25 USD/TiB);
results are cached for a week. The billing project is `PULSE_ADS_PROJECT`, `GOOGLE_CLOUD_PROJECT`
or ADC's project; it is never written to a file.

**Meta Ad Library API** (`radar/adintel/meta_library.py`): `GET graph.facebook.com/{version}/
ads_archive` with `ad_reached_countries` (EU countries: outside political ads only EU-delivered
ads are available), `search_terms` or `search_page_ids`, paging through `paging.next`. Token in
`PULSE_META_AD_LIBRARY_TOKEN`, never logged or stored, redacted from errors. NOT RUN LIVE: tested
only on recorded-shape synthetic fixtures (`tests/fixtures/radar/dossier/meta/`); without a token
the dossier shows "not configured".

### Configuration

```yaml
radar:
  dossier:
    studio: {team_size: 2, engine: unity, genres: [puzzle, casual], strengths: [color-sort],
             art: [2d, stylized-3d], monetization: [ads, hybrid], avoid: [pvp-race, battle-pass],
             months: 3, budget_usd: 3000, target_markets: [us], test_market: br,
             test_platform: android, launch_platform: android, max_cpi_usd: 1.5, ipm_gate: 8}
    rubric: {weights: {momentum: 0.2, saturation: 0.15, monetization_fit: 0.15,
                       capability_fit: 0.25, production: 0.15, cpi_risk: 0.1},
             yap_min: 0.62, bekle_min: 0.45, cpi_cap_ratio: 2}
    test: {variants: 4, installs_per_variant: 40, days: 4, min_per_variant_usd: 25}
    screenshots: 6
    reviews: true
    countries: [us, gb]          # Lookup storefronts (default: the radar's countries)
    ads: {google_atc: true, max_gb: 2, meta: {countries: [DE, FR], version: v24.0}}
    effort: {base: {color-sort: 2.0}}   # overrides benchmarks.yaml
```

The LLM budget is the shared `llm.budget_usd` per run. On 14 Nov 2026 three live dossiers cost
0.031 USD (Gemini 3.5 Flash-Lite: one multimodal request of about 7-11 k input tokens and one
review request each).
