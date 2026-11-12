#!/bin/sh
# bootstrap.sh - the collector on Cloud Run in one command, from a clean export of a branch.
#
#   sh deploy/cloudrun/bootstrap.sh <project> [--plan]
#
# 1. enables the APIs the deploy needs (Cloud Run, Artifact Registry, Cloud Build,
#    Secret Manager, BigQuery, BigQuery Storage, IAM);
# 2. makes the keys on this machine, once: one public write key per app in APPS
#    (default "game app"; the SDKs carry these) and a read key for /live. They go to
#    Secret Manager (pulse-write-keys, pulse-read-key) and to a local file readable by
#    you only (<state dir>/collector.env); they are never printed or committed;
# 3. exports BRANCH (default main) with `git archive` into a temporary folder, so
#    untracked local files (warehouses, exports) are never uploaded to Cloud Build;
# 4. runs deploy.sh --apply there (dataset, tables, service account, image, service);
# 5. writes the service URL to collector.env and checks /healthz.
#
# --plan prints what would run and changes nothing. Run it yourself: it creates a
# service account, grants it roles and opens a public endpoint in your project.
set -eu

PROJECT="${1:-}"
[ -n "$PROJECT" ] || { echo "usage: sh deploy/cloudrun/bootstrap.sh <project> [--plan]" >&2; exit 2; }
PLAN=0
[ "${2:-}" = "--plan" ] && PLAN=1
BRANCH="${BRANCH:-main}"
APPS="${APPS:-game app}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-pulse-collector}"

say() { printf 'pulse-bootstrap: %s\n' "$*"; }
run() {
    printf '+ %s\n' "$*"
    if [ "$PLAN" = 0 ]; then "$@"; fi
}

case "$(uname -s)" in
    Darwin) STATE="${PULSE_HOME:-$HOME/Library/Application Support/studio-pulse}" ;;
    *) STATE="${PULSE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/studio-pulse}" ;;
esac
ENVF="$STATE/collector.env"
ROOT=$(git rev-parse --show-toplevel)

run gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
    cloudbuild.googleapis.com secretmanager.googleapis.com bigquery.googleapis.com \
    bigquerystorage.googleapis.com iam.googleapis.com --project "$PROJECT"

if [ ! -f "$ENVF" ]; then
    say "making keys in $ENVF (readable by you only)"
    if [ "$PLAN" = 0 ]; then
        mkdir -p "$STATE"
        umask 077
        python3 - "$ENVF" "$APPS" <<'PY'
import secrets, sys
path, apps = sys.argv[1], sys.argv[2].split()
keys = {a: f"pk_{a}_{secrets.token_hex(12)}" for a in apps}
with open(path, "w", encoding="utf-8") as fh:
    for a, k in keys.items():
        fh.write(f"PULSE_WRITE_KEY_{a.upper()}={k}\n")
    fh.write("PULSE_WRITE_KEYS=" + ",".join(keys.values()) + "\n")
    fh.write(f"PULSE_READ_KEY={secrets.token_urlsafe(32)}\n")
PY
    fi
fi

secret() {  # name, value from collector.env; never on the command line
    name=$1; var=$2
    if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
        say "secret $name exists: adding a new version"
        cmd=add
    else
        cmd=create
    fi
    printf '+ gcloud secrets %s %s --data-file=- (value from %s)\n' "$cmd" "$name" "$ENVF"
    [ "$PLAN" = 1 ] && return 0
    val=$(sed -n "s/^$var=//p" "$ENVF")
    if [ "$cmd" = create ]; then
        printf '%s' "$val" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic --project "$PROJECT" >/dev/null
    else
        printf '%s' "$val" | gcloud secrets versions add "$name" --data-file=- --project "$PROJECT" >/dev/null
    fi
}
secret pulse-write-keys PULSE_WRITE_KEYS
secret pulse-read-key PULSE_READ_KEY

WORK=$(mktemp -d)
say "exporting $BRANCH ($(git -C "$ROOT" rev-parse --short "$BRANCH")) to $WORK"
git -C "$ROOT" archive "$BRANCH" | tar -x -C "$WORK"
if [ "$PLAN" = 1 ]; then
    (cd "$WORK" && PROJECT="$PROJECT" REGION="$REGION" SERVICE="$SERVICE" sh deploy/cloudrun/deploy.sh)
else
    (cd "$WORK" && PROJECT="$PROJECT" REGION="$REGION" SERVICE="$SERVICE" sh deploy/cloudrun/deploy.sh --apply)
    URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
    grep -v '^PULSE_COLLECTOR_URL=' "$ENVF" > "$ENVF.tmp" || true
    echo "PULSE_COLLECTOR_URL=$URL" >> "$ENVF.tmp"
    mv "$ENVF.tmp" "$ENVF"
    chmod 600 "$ENVF"
    say "collector: $URL"
    curl -fsS "$URL/healthz" && echo
    say "keys and URL: $ENVF (the SDKs need the URL and their app's write key)"
fi
case "$WORK" in /tmp/*|/var/folders/*|/private/*) rm -r "$WORK" ;; esac
