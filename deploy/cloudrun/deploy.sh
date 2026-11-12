#!/bin/sh
# Deploy the Studio Pulse collector to Cloud Run (cloud mode, BigQuery).
#
# Prints every command and changes nothing unless --apply is given.
#   PROJECT=my-project ./deploy/cloudrun/deploy.sh            # print the plan
#   PROJECT=my-project ./deploy/cloudrun/deploy.sh --apply    # run it
#
# Needs: gcloud and bq (Google Cloud SDK), logged in; two Secret Manager
# secrets holding the keys: pulse-write-keys (comma separated public write
# keys) and pulse-read-key. Run from the repository root.
set -eu

PROJECT="${PROJECT:?set PROJECT to your Google Cloud project id}"
REGION="${REGION:-us-central1}"
BQ_LOCATION="${BQ_LOCATION:-US}"
DATASET="${DATASET:-pulse_raw}"
SERVICE="${SERVICE:-pulse-collector}"
REPO="${REPO:-pulse}"
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/$SERVICE:$TAG"
SA="$SERVICE@$PROJECT.iam.gserviceaccount.com"
RETENTION_S=34214400   # 396 days: raw events kept 13 months (docs/PRIVACY.md)
SCHEMAS=contract/generated/bigquery

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

run() {
  echo "+ $*"
  if [ "$APPLY" = 1 ]; then "$@"; fi
}

[ -f "$SCHEMAS/events.json" ] || { echo "run from the repository root" >&2; exit 2; }

# 1. Dataset (raw partitions expire after 13 months) and tables from the contract
run bq --location="$BQ_LOCATION" mk --dataset --default_partition_expiration="$RETENTION_S" "$PROJECT:$DATASET"
run bq mk --table --time_partitioning_field received_at --time_partitioning_type DAY \
  --clustering_fields app_id,name "$PROJECT:$DATASET.events" "$SCHEMAS/events.json"
run bq mk --table --time_partitioning_field received_at --time_partitioning_type DAY \
  "$PROJECT:$DATASET.quarantine" "$SCHEMAS/quarantine.json"
run bq mk --table "$PROJECT:$DATASET.app_registry" "$SCHEMAS/app_registry.json"
run bq mk --table "$PROJECT:$DATASET.deletion_requests" "$SCHEMAS/deletion_requests.json"

# 2. A service account that can only write this dataset and run its MERGE
run gcloud iam service-accounts create "$SERVICE" --project "$PROJECT" --display-name "Studio Pulse collector"
run bq add-iam-policy-binding --member="serviceAccount:$SA" --role=roles/bigquery.dataEditor "$PROJECT:$DATASET"
run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role=roles/bigquery.jobUser
run gcloud secrets add-iam-policy-binding pulse-write-keys --project "$PROJECT" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
run gcloud secrets add-iam-policy-binding pulse-read-key --project "$PROJECT" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor

# 3. Image
run gcloud artifacts repositories create "$REPO" --project "$PROJECT" --location "$REGION" --repository-format docker
run gcloud builds submit --project "$PROJECT" --config deploy/cloudrun/cloudbuild.yaml --substitutions "_IMAGE=$IMAGE" .

# 4. Service: one instance keeps one consistent live view and in-memory dedupe
run gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA" --allow-unauthenticated \
  --max-instances=1 --concurrency=200 --memory=512Mi --cpu=1 --timeout=30 \
  --set-env-vars "PULSE_COLLECTOR_MODE=bigquery,PULSE_BQ_PROJECT=$PROJECT,PULSE_BQ_DATASET=$DATASET,PULSE_BQ_LOCATION=$BQ_LOCATION,PULSE_TRUSTED_PROXY_HOPS=1" \
  --set-secrets "PULSE_WRITE_KEYS=pulse-write-keys:latest,PULSE_READ_KEY=pulse-read-key:latest"

if [ "$APPLY" = 0 ]; then
  echo "(plan only: nothing was changed; add --apply to run these commands)"
fi
