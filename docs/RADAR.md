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
