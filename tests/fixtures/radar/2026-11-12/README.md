# Radar fixture: recorded store responses, 12 Nov 2026

Public store metadata recorded on 12 Nov 2026 (~13:49 UTC) by the market-radar research
experiment (29 live requests, no login), minimised for tests. CI never calls a store.

| File | Recorded request | Minimised |
|---|---|---|
| `apple/rss_us_topfree_6014.json` | `itunes.apple.com/us/rss/topfreeapplications/limit=100/genre=6014/json` | name, artist, ids, release date, category; no summary or images |
| `apple/rss_tr_topfree_6014.json` | same for `tr` | same |
| `apple/rss_us_topfree_7012.json` | `.../us/rss/topfreeapplications/limit=50/genre=7012/json` | same |
| `apple/rss_us_topgrossing_6014.json` | `.../us/rss/topgrossingapplications/limit=200/genre=6014/json` (returned 100: the cap); replayed for `limit=100` | same |
| `apple/rss_us_newfree_6014.json` | `.../us/rss/newfreeapplications/limit=25/genre=6014/json` (returned 107 rows, 22 of them games: the feed ignores limit and genre) | same |
| `apple/lookup_pool_us.json` | `itunes.apple.com/lookup?id=...&country=us` for 11 new games, 3 ids, and `bundleId=com.dreamgames.royalmatch` | fields the radar reads; description cut to 1,200 characters; no screenshots |
| `apple/lookup_pool_tr.json` | the same ids with `country=tr` | same |
| `apple/lookup_publisher_abi_us.json` | `lookup?id=1743349083&entity=software&country=us&limit=200` | no descriptions |
| `play/*.json` | google-play-scraper 1.2.7 `app(id, lang="en", country="us")` output for 7 packages | fields the radar reads; description cut; `developerEmail` replaced by a synthetic `contact@example.invalid` (the personal-data test checks it is never stored) |
| `apple/lookup_ungodly_us_0.json`, `apple/lookup_ungodly_us_130.json` | UnGodly (6740617332) in the US Lookup: 0 ratings at ~13:49 UTC (the research run) and 130 at ~14:58 UTC (studio-pulse's first live radar run), the same day | one app each; replayed as a sequence for the recheck test |
| `robots/play_robots.txt` | `play.google.com/robots.txt` | excerpt with the rules the radar depends on |

`replay.json` maps requests to these files (`pool` entries answer any batch of Lookup ids
from the recorded results; `play_fields` entries build a details page from the recorded
parser output so the real parser runs). `play_map.yaml` marks the young charted games whose
Play page was not recorded as having no known Play package, so a replay never asks for an
unrecorded page. `pulse.yaml` is the radar config for `pulse radar run --replay <this dir>`.
