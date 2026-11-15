# Reklam Merkezi (ad centre, `/reklam`)

One screen per game for its paid user acquisition: what every campaign and creative is doing, what changed and
whether it mattered, and, from the same screen, making new creatives and publishing them through
creative-forge. Turkish by default, English with `?lang=en`.
The sidebar has it under Araçlar / Tools, and the command palette (⌘K) lists it.

Nothing is game-specific: every app in the config (and every app the SDK collector registers) is in the game
switcher with the same page. The last game chosen is remembered per viewer (an HttpOnly cookie, `reklam-app`).

## What the screen shows

| Part | Source | Notes |
|---|---|---|
| Period | `?range=today|7d|30d|custom&from=&to=` | "Son gün" is the newest day with ad data ("Bugün" once a refresh has read today); every period is compared with the one of the same length right before it; custom is 1-180 days |
| Freshness per network | `fct_source_freshness` + the app's sources | the newest day and the last load per connected network (`kayıtlı yanıt` marks recorded demo responses), a missing key by variable name, and every network not connected with its config key and variable |
| KPIs | `stg_ua_spend`, `fct_ua_cohort_roas`, `fct_ua_summary` | spend, impressions, video views (+ view and completion rate), network installs, CPI, D0 and D7 ROAS, observed payback day; definitions below |
| AI summary | `serve/reklam_ai.py` | grounded, see below; loaded after the page (htmx) and cached |
| Charts | `serve/reklam_charts.py` | daily spend and daily installs (two charts, one axis each, one line per network), CPI per network, cohort ROAS D0 / D7 by spend day, creative fatigue (IPM and CTR by week live for the period's five biggest creatives), the network split (bars with installs and CPI as text) |
| Change markers | creative-forge's audit log (`forge snapshot`) and `reklam.changes` files | a numbered hairline on each changed day; the same numbers head the "Değişiklikler" list, so a marker is never only a tooltip |
| Campaign table | `stg_ua_spend` | network → campaign → creative; status (delivering on the newest day or not; a creative-forge campaign shows its own ACTIVE / PAUSED), spend and its change, impressions, views, completion, CTR, IPM, installs, CPI, D7 ROAS (networks and campaigns; networks do not attribute revenue to ads), a spend sparkline; sortable inside the hierarchy, rows open and close |
| Creative drawer | `stg_ua_spend` + creative-forge | the video (when creative-forge has the file for that creative id) with its QA and review state, the fatigue verdict, period and all-time numbers, IPM and spend over time with the change markers, and every campaign it ran in |

Colours follow the network on every chart (Meta, Google Ads, TikTok, AppLovin, Unity Ads, ... keep slots
`--s1..--s5` of the shared palette, validated for colour-blind separation in light and dark); every chart has a
legend for two or more series, direct end labels when they fit, a hover readout of every series for the day and a
"Veri tablosu" table view.

### Definitions

Network-reported numbers, as in [METRICS.md](METRICS.md) (User acquisition, Creative scorecard):

| Metric | Formula |
|---|---|
| CPI | spend (USD) / network installs |
| CTR | clicks / impressions |
| IPM | network installs / impressions × 1000 |
| Video views | the network's own view (Meta: 3-second views, the `video_view` action; TikTok: 2-second views, `video_watched_2s`); empty for networks that do not report views |
| View rate | video views / impressions of the rows that report views |
| Completion | completed views (Meta `video_p100_watched_actions`, TikTok `video_views_p100`) / video views |
| D0 / D7 ROAS | cohort revenue by day N / spend, over the period's spend days whose day N is complete (`fct_ua_cohort_roas`; the revenue basis is the network's attribution setting) |
| Payback | first day the pooled ROAS curve reaches 100% (`fct_ua_summary.payback_day`, last 30 days) |
| Fatigue | IPM of a creative's first 7 days live against its newest 7 (14+ days live, 2,000+ impressions each side): "tired" at -20% or worse with a two-proportion test p < 0.05; a drop of 10% or more that is not significant is shown as "watch" |

### The AI summary, and "beyond normal noise"

Code writes the paragraph and a model may only reword it (`brain.narrate.restyle`, the analyst's narrator): every
number is formatted by `brain/fmt.py` and checked by `brain/verify.py` against the facts it was computed from, a
sentence whose numbers do not verify is dropped, and a model's rewrite is kept only if all its numbers still verify,
it adds no link or instruction and keeps its Turkish letters. Names from the networks are quoted as data.

It says: spend, installs and CPI against the previous period; IPM against the previous period; D7 ROAS when complete
cohorts exist; the best and the weakest creative on the same network (5,000+ impressions each) with the IPM's 95%
Wilson interval and why the weak one is weak (IPM = CTR × installs per click: a low CTR points at the first seconds,
a low install rate per click at the promise against the store page) and whether the two intervals overlap; tired
creatives; and up to three changes in or just before the period with the campaign's IPM (CPI for a budget change)
over up to seven days before and after.

"Günlük dalgalanmanın ötesinde" (beyond day-to-day noise) means a Welch test on the **daily** values with at least
three days on each side and p < 0.05. Ad delivery swings far more from day to day than a binomial count of installs
suggests, so pooled counts alone would call almost every wobble significant; below three days a side the summary
says "henüz anlamlı değil" instead of testing.

The model is the one configured for the brain (`brain:` / `llm:`, same providers and fallback); `reklam: {ai: {...}}`
overrides it for this page. Its spend is capped by `reklam.ai.budget_usd` (default 0.05 USD per server run), and the
paragraph and the model's answer are cached by content hash under `<state_dir>/reklam/ai`. With no model configured
the template stands (the badge says "şablon").

## Refresh

"Şimdi yenile" starts `pulse-bg run --app <game> --source <its ad network sources> --through-today` in the
background (the same lock, heartbeat and snapshot as every refresh; models, checks and alerts run on everything),
and the page reloads when it has finished. "15 dk'da bir otomatik yenile" presses it every 15 minutes while the page
is open (remembered per viewer in the browser). `pulse run` takes the same `--app`, `--source` and `--through-today`
options; without `--through-today` the window ends yesterday as before.

## Making and publishing (creative-forge)

### How the two tools talk

Studio Pulse runs the `forge` command with JSON output (`serve/forge_bridge.py`) rather than calling creative-forge's
local web app:

- creative-forge's pages carry a per-process CSRF token that only its own pages can read; using it from another
  program would weaken exactly that protection. The CLI needs no token.
- The CLI works whether or not the creative-forge app is open, and every write goes through the same engine code and
  gates as when a person types the command.
- Arguments are a list (no shell), every call has a timeout, the output is versioned JSON
  (`creative-forge/snapshot/v1`, `…/make/v1`, …), and a missing or broken `forge` turns only the make / publish tabs
  off; the numbers stay.

Which `forge`: `reklam.forge` in the config, else `PULSE_FORGE_BIN`, else `forge` on `PATH`, else
`~/.local/bin/forge`. `reklam.forge_root`, `forge_state` and `forge_db` point it at a runs folder, campaign state and
performance store; without them creative-forge uses its own defaults (the desktop app's runs folder and its user data
folder), so both apps see the same work. The demo config sets all three under its own folder, so trying the flow in
the demo never writes into a real creative-forge data folder.

| Step on the page | creative-forge command | Gate (enforced by creative-forge) |
|---|---|---|
| everything listed | `forge snapshot --json` (cached 30 s) | read only |
| Yeni kreatif | `forge make --app … --brief … --variants N --language … --providers fake\|vertex [--budget-usd --gameplay] --progress FILE --json` | trial runs use fake providers and cost nothing; a paid run needs a budget cap (reserved before every call) and a real gameplay capture; nothing is approved |
| Onayla / Reddet | `forge review RUN --approve\|--reject FILE --note … --json` | the decision is bound to the file's SHA-256; a rejection needs a note |
| Kampanyayı hazırla | `forge publish RUN --to …` → `forge campaign init …` → `forge campaign create CONFIG --json` | only approved, QA-clean files; every object created PAUSED; AppLovin, Unity Ads and Mintegral get a manual upload pack (downloadable as a zip, with the steps); a new config is written in mock mode |
| Yayına al | `forge campaign activate REF --confirm "<typed phrase>" --json` | the typed phrase must name the daily amount; the caps are checked again; a live-mode campaign also needs the "Gerçek hesap" box, and Studio Pulse passes `--live` only then |
| Durdur | `forge campaign pause REF --json` | always allowed |
| Oynanabilir | `forge playable sample …`, `forge playable pack SRC --network N --cta-adapter --json` | each build is checked against the network's published rules |

Files are served to the page only from paths the last snapshot listed, by an opaque token; a playable preview runs in
a sandboxed frame with its own Content-Security-Policy (`sandbox allow-scripts`, no network).

The competitor creative library (`/radar/kreatifler`) links to
`/reklam?brief=…&hook=…&mechanic=…&orientation=…&genre=…&ref=<key>&lang=…`; the wizard opens prefilled (checked and
escaped), and nothing runs until "Üret".

## Demo and real data

`pulse demo` fills the whole screen with fictional games ("Örnek veri" on the page): network reports in each
network's own format (recorded Meta, Google Ads and TikTok API responses, AppLovin and Unity Ads CSV exports) with
video views for Meta and TikTok, creatives that start and stop part-way, a budget moved between two networks, ad
fatigue, and those changes as dated events in `ua/<app>/changes.json` (listed under `reklam.changes`). Trying "Yeni
kreatif" in the demo renders with creative-forge's fake providers into the demo's own `forge/` folder; campaigns there
go to creative-forge's mock network.

Real data needs, per network, the source under `apps.<id>.sources` and its key in the environment (Bağlantılar /
Connections lists each one): `meta_ads` + `PULSE_META_ACCESS_TOKEN`, `google_ads` + a developer token and OAuth
(`PULSE_GOOGLE_ADS_*`), `tiktok_ads` + `PULSE_TIKTOK_ACCESS_TOKEN`, `applovin_ads` + `PULSE_APPLOVIN_REPORT_KEY`,
`unity_ads` + `PULSE_UNITY_ADS_KEY_ID` / `PULSE_UNITY_ADS_SECRET_KEY`, or `ua_csv` for an export. Changes come from
creative-forge's audit log as soon as campaigns are made there. Publishing to a real account is creative-forge's own
step: real ids in its campaign config, `mode: sandbox` or `live`, then the typed activation.
