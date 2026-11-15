# Collector on Cloud Run (cloud mode)

`pulse collect` in cloud mode: Cloud Run in front, BigQuery behind it through
the Storage Write API. Nothing in this repository deploys it; CI only tests the
BigQuery sink with fake clients. Run the plan yourself:

```bash
PROJECT=my-project ./deploy/cloudrun/deploy.sh            # prints every command, changes nothing
PROJECT=my-project ./deploy/cloudrun/deploy.sh --apply    # creates dataset, tables, service account, image, service
```

```powershell
$env:PROJECT = "my-project"; .\deploy\cloudrun\deploy.ps1          # plan
$env:PROJECT = "my-project"; .\deploy\cloudrun\deploy.ps1 -Apply   # run it
```

Before `--apply`: create two Secret Manager secrets, `pulse-write-keys` (the
public write keys the SDK carries, each bound to its apps: `key=app_id|app_id`,
comma separated; two per app during a rotation) and `pulse-read-key` (for
`/live`). The keys never go into the image, the command line or this repository.
`bootstrap.sh` below makes both. With `--apply`, what exists already (dataset,
tables, service account, repository) is kept, so the same command redeploys.

## One command: bootstrap.sh

```bash
APPS="puzzle=com.example.puzzle runner=com.example.runner|com.example.runner.ios" \
  sh deploy/cloudrun/bootstrap.sh my-project --plan   # what would run
APPS="puzzle=com.example.puzzle runner=com.example.runner|com.example.runner.ios" \
  sh deploy/cloudrun/bootstrap.sh my-project          # do it
```

`APPS` is one `label=app_id|app_id` entry per game: the app ids are the bundle
ids / package names its builds send as `ctx.app_id` (iOS and Android may share
one). It enables the APIs and makes, with `make_keys.py`, one public write key
per entry **bound to those app ids** plus the `/live` read key, on your machine
(`<state dir>/collector.env`, mode 600, never printed: keys are shown by their
key id). A bound key answers `403 key_not_for_app` for any other app, so a key
pulled out of one game cannot create apps or forge events and revenue for
another; an entry without app ids is refused. A second run keeps the keys
already in the file and binds them to the apps given now (a key the file holds
without apps, from an earlier bootstrap, is refused until `APPS` names its
apps). The secrets get a new version only when a value changed. It then exports
`BRANCH` (default `main`) with `git archive` (so untracked files such as local
warehouses never reach Cloud Build), runs `deploy.sh --apply` from that export,
writes the service URL next to the keys and one `<state dir>/sdk/<label>.env`
per game with the URL and that game's write key only: what its release build
needs, never the read key. It changes IAM and opens a public endpoint in the
project, so run it yourself. After it: send one batch from a development build
and check the rows in `pulse_raw.events`.

## What the plan creates

| Step | Resource |
|---|---|
| 1 | dataset `pulse_raw` with a 396-day default partition expiration (raw events kept 13 months, docs/PRIVACY.md) |
| 1 | tables from `contract/generated/bigquery/*.json`: `events` (partitioned by `DATE(received_at)`, clustered by `app_id, name`), `quarantine`, `app_registry`, `deletion_requests` |
| 2 | service account `pulse-collector`: `bigquery.dataEditor` on the dataset only, `bigquery.jobUser` (the registry MERGE), read access to the two secrets |
| 3 | Artifact Registry repository + image built by Cloud Build from `deploy/cloudrun/Dockerfile` (`GEOIP=1` bakes in DB-IP Country Lite, checksum-verified, with its attribution) |
| 4 | Cloud Run service `pulse-collector`, `--min-instances=0 --max-instances=1 --concurrency=40 --memory=1Gi --cpu=1 --cpu-throttling`, public (`--allow-unauthenticated`: the SDK authenticates with the write key) |

The size, all overridable through the environment of `deploy.sh`
(`MIN_INSTANCES`, `MAX_INSTANCES`, `CONCURRENCY`, `MEMORY`, `CPU`,
`DEDUPE_CAPACITY`):

- `--min-instances=0`: nothing runs and nothing is billed while no game sends;
  the first request after a quiet spell waits for a cold start (the SDK queue
  keeps the events). `--cpu-throttling` bills CPU only while a request is in
  flight.
- `--max-instances=1` is deliberate: one instance holds the one consistent
  "active right now" window and the in-memory dedupe, and it is the cost cap. A
  restart refills the window within five minutes; the warehouse dedupes anyway.
  Measured with fake BigQuery clients, a batch costs ~0.5 ms of CPU in the
  collector, so one vCPU is not the limit at 100k DAU (below); the BigQuery
  append is waiting, not CPU. A second instance would split `/live`.
- `--concurrency=40` matches the 40 worker threads, so at most 40 bodies of up to
  1 MiB are held at once.
- `--memory=1Gi`: ~175 MB for Python and the Google clients, up to ~92 MB for the
  dedupe set (`PULSE_DEDUPE_CAPACITY`, 1 M keys, two per event: a retry within
  the last 250k-500k events is caught in memory) and ~13 MB per 1 MiB batch in
  flight, 40 at once. The earlier LRU of tuples took ~470 MB for its 2 M keys
  and did not fit 512 MiB next to the libraries.
- The registry is MERGEd when an app is first seen, promoted or shows a new
  version, otherwise at most every 15 minutes per app and platform (each query
  bills at least 10 MB), and read for `/live` at most once a minute.

## Environment

| Variable | Value |
|---|---|
| `PULSE_COLLECTOR_MODE` | `bigquery` |
| `PULSE_BQ_PROJECT`, `PULSE_BQ_DATASET`, `PULSE_BQ_LOCATION` | where the tables are |
| `PULSE_WRITE_KEYS` | secret: accepted public write keys, comma separated; `key=app_id\|app_id` binds a key to its apps (see docs/COLLECTOR.md; bootstrap.sh binds every key) |
| `PULSE_READ_KEY` | secret: required by `GET /live` |
| `PULSE_TRUSTED_PROXY_HOPS` | `1`: the client address is the rightmost `X-Forwarded-For` entry added by Google's front end (used in memory for the country, never stored). Check it on the first deploy |
| `PULSE_COUNTRY_HEADER` | optional, e.g. `CF-IPCountry` when Cloudflare is in front |
| `PULSE_BUNDLE_PREFIXES` | optional allowlist, e.g. `com.example.` |
| `PULSE_DEDUPE_CAPACITY` | in-memory dedupe keys (default 1000000, ~92 MB; two per event) |

Point `pulse serve` at it with `collector: {url: https://pulse-collector-....run.app, read_key: ${PULSE_READ_KEY}}`.

## Cost by daily active users (us-central1 list prices, checked 15 Nov 2026)

Cloud Run with request-based billing charges an instance's CPU and memory for
every moment at least one request is in flight (rounded up to 100 ms), not the
CPU a request uses, plus $0.40 per million requests; free each month per billing
account: 180,000 vCPU-s, 360,000 GiB-s, 2 M requests. Assumptions: 150 events and
40 requests per daily active user per day (the SDK flushes every 30 s while
playing), ~0.1 s per request (the BigQuery append), 0.5 KB per event, 1 vCPU /
1 GiB, the free tiers used by nothing else, `pulse run` once a day over a 60-day
lookback.

| DAU | Cloud Run (busy time, requests) | BigQuery | Request logs | Total per month |
|---|---|---|---|---|
| 0 | $0: scaled to zero | $0 | $0 | **~$0** (the image: ~0.2 GB in Artifact Registry, 0.5 GB free) |
| 1k | $0: ~117k busy s, 1.2 M requests, inside the free tier | $0: 4.5 GB a month | $0 | **~$0** |
| 10k | ~$24: 37% busy (962k s: CPU $18.8, memory $1.5), 12 M requests $4.0 | storage $0 -> ~$3 after a year (22.5 GB a month); reads ~$2 | $0 (~12 GB, 50 GiB free) | **~$25 -> ~$30** |
| 100k | ~$111: busy all the time (CPU $58, memory $5.6), 120 M requests $47 | storage ~$2 -> ~$33 after a year (225 GB a month); reads ~$78 (450 GB per run) | ~$35 (~120 GB) | **~$225 -> ~$260** |

At 100k DAU three switches roughly halve it: instance-based billing
(`--no-cpu-throttling --min-instances=1`: one instance all month is ~$47 and
requests are free), a log exclusion for the collector's request logs, and a
shorter lookback for the SDK source. `pulse run` reads every column of the
lookback window from `pulse_raw.events`, so its dry run against
`max_bytes_billed` (2 GB in the example config) refuses the load once 60 days
hold more than 2 GB, around 450 DAU: raise the cap or shorten `lookback_days`.

The per-event table from the research (0.5 KB per event as JSON, 20 events per
request, 40 ms CPU per request, raw retention 13 months) counts CPU per request
and so reads low for Cloud Run:

| Item | 1 M events/month | 100 M events/month |
|---|---|---|
| Data volume | ~0.47 GiB | ~46.6 GiB |
| Cloud Run requests | 50k -> free | 5 M -> 3 M billable -> $1.20 |
| Cloud Run CPU / memory | 2k vCPU-s -> free | 200k vCPU-s -> 20k billable -> $0.48; memory free |
| Ingest: Storage Write API (used here) | free | free (first 2 TiB/month free) |
| Ingest alternative: Pub/Sub + BigQuery subscription | $0.02 | $3.71 |
| Ingest alternative: REST streaming inserts | $0.05 | $4.88 |
| BigQuery storage | free (< 10 GiB) | month 1 ~$0.84 -> month 12 ~$9.5 |
| Queries (incremental daily rollups) | free | free under 1 TiB/month |
| Live view (in memory) | $0 | $0 |
| **Total** | **~$0** | **~$2 now -> ~$12/month after a year** |

`/healthz` does not reach the service on Cloud Run (its front end reserves paths
ending in `z` and answers 404); use `/health`, the same answer.

## Verified on a first deploy (15 Nov 2026), and not

Deployed with `bootstrap.sh` into a real project (us-central1, dataset in `US`)
and checked by hand:

- The Arrow appends land: a gzip batch with a bound key answered 200 and its
  rows were in `events` with their `key_id`; the same key for another app
  answered 403 (`batch:key_not_for_app` in `quarantine`, no app attached), an
  unknown key 401, `/live` 401 without the read key and 200 with it, `/health`
  200 with `bound_keys`.
- `PULSE_TRUSTED_PROXY_HOPS=1` is right on Cloud Run: a batch carrying a forged
  `X-Forwarded-For` of another country got the real client's country.
- `pulse ingest --source pulse_sdk` read the rows back through the
  `collector.bigquery` block into `raw_pulse_events`; after `POST /v2/delete`
  the same read returned none (the rows stay in BigQuery until their partition
  expires and are left out of every load).
- `bq add-iam-policy-binding -d` needs allowlisting; deploy.sh grants the
  dataset role with a DCL `GRANT ... ON SCHEMA` instead.

Not verified:

- Memory and latency under real load (the sizing above is measured locally with
  fake clients).
- Live detection (`pulse apps refresh`) reads the local SQLite registry; in
  cloud mode the registry lives in BigQuery and that job is not wired up yet.
