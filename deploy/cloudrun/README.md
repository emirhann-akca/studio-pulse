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
public write keys the SDK carries, comma separated; two during a rotation) and
`pulse-read-key` (for `/live`). The keys never go into the image, the command
line or this repository.

## One command: bootstrap.sh

```bash
APPS="puzzle fitness" sh deploy/cloudrun/bootstrap.sh my-project --plan   # what would run
APPS="puzzle fitness" sh deploy/cloudrun/bootstrap.sh my-project          # do it
```

It enables the APIs, makes one public write key per app plus the `/live` read
key on your machine (once; `<state dir>/collector.env`, mode 600, never
printed), stores them in Secret Manager, exports `main` with `git archive` (so
untracked files such as local warehouses never reach Cloud Build), runs
`deploy.sh --apply` from that export and writes the service URL next to the
keys. It changes IAM and opens a public endpoint in the project, so run it
yourself. After it: send one batch from a development build and check the rows
in `pulse_raw.events` (the BigQuery sink is tested with fake clients only).

## What the plan creates

| Step | Resource |
|---|---|
| 1 | dataset `pulse_raw` with a 396-day default partition expiration (raw events kept 13 months, docs/PRIVACY.md) |
| 1 | tables from `contract/generated/bigquery/*.json`: `events` (partitioned by `DATE(received_at)`, clustered by `app_id, name`), `quarantine`, `app_registry`, `deletion_requests` |
| 2 | service account `pulse-collector`: `bigquery.dataEditor` on the dataset only, `bigquery.jobUser` (the registry MERGE), read access to the two secrets |
| 3 | Artifact Registry repository + image built by Cloud Build from `deploy/cloudrun/Dockerfile` (`GEOIP=1` bakes in DB-IP Country Lite, checksum-verified, with its attribution) |
| 4 | Cloud Run service `pulse-collector`, `--max-instances=1 --concurrency=200 --memory=512Mi --cpu=1`, public (`--allow-unauthenticated`: the SDK authenticates with the write key) |

`--max-instances=1` is deliberate: one instance holds the one consistent
"active right now" window and the in-memory dedupe. A restart refills the
window within five minutes; the warehouse dedupes anyway.

## Environment

| Variable | Value |
|---|---|
| `PULSE_COLLECTOR_MODE` | `bigquery` |
| `PULSE_BQ_PROJECT`, `PULSE_BQ_DATASET`, `PULSE_BQ_LOCATION` | where the tables are |
| `PULSE_WRITE_KEYS` | secret: accepted public write keys, comma separated; `key=app_id\|app_id` binds a key to its apps (see docs/COLLECTOR.md) |
| `PULSE_READ_KEY` | secret: required by `GET /live` |
| `PULSE_TRUSTED_PROXY_HOPS` | `1`: the client address is the rightmost `X-Forwarded-For` entry added by Google's front end (used in memory for the country, never stored). Check it on the first deploy |
| `PULSE_COUNTRY_HEADER` | optional, e.g. `CF-IPCountry` when Cloudflare is in front |
| `PULSE_BUNDLE_PREFIXES` | optional allowlist, e.g. `com.grich.` |

Point `pulse serve` at it with `collector: {url: https://pulse-collector-....run.app, read_key: ${PULSE_READ_KEY}}`.

## Cost (from the research, us-central1 list prices, 12 Nov 2026)

Assumptions: 0.5 KB per event as JSON, 20 events per request, 40 ms CPU per
request, 1 vCPU / 512 MiB, raw retention 13 months.

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

## Not verified here

- The sink sends Arrow record batches (`AppendRowsRequest.arrow_rows`) with
  `TIMESTAMP` columns as Arrow `timestamp[us, UTC]` and `params` as a JSON
  string; it is tested against fake clients only. Send one batch with
  `pulse simulate --url https://... --key ... --duration 30` after the first
  deploy and look at the rows.
- The `X-Forwarded-For` position on Cloud Run (see `PULSE_TRUSTED_PROXY_HOPS`).
- Live detection (`pulse apps refresh`) reads the local SQLite registry; in
  cloud mode the registry lives in BigQuery and that job is not wired up yet.
