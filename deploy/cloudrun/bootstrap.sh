#!/bin/sh
# bootstrap.sh - the collector on Cloud Run in one command, from a clean export of a branch.
#
#   APPS="puzzle=com.example.puzzle runner=com.example.runner|com.example.runner.ios" \
#     sh deploy/cloudrun/bootstrap.sh <project> [--plan]
#
# 1. enables the APIs the deploy needs (Cloud Run, Artifact Registry, Cloud Build,
#    Secret Manager, BigQuery, BigQuery Storage, IAM);
# 2. makes the keys on this machine (make_keys.py): one public write key per APPS entry
#    (the SDK of that game carries it), BOUND to the entry's app ids, and a read key for
#    /live. A bound key answers 403 for any other app, so a key pulled out of one game
#    cannot write for another; an entry without app ids is refused. Keys already in the
#    file are kept and bound. They go to Secret Manager (pulse-write-keys, pulse-read-key;
#    a new version only when the value changed) and to a local file readable by you only
#    (<state dir>/collector.env); they are never printed or committed;
# 3. exports BRANCH (default main) with `git archive` into a temporary folder, so
#    untracked local files (warehouses, exports) are never uploaded to Cloud Build;
# 4. runs deploy.sh --apply there (dataset, tables, service account, image, service;
#    what exists already is kept, so a second run only builds and rolls out);
# 5. writes the service URL to collector.env, one <state dir>/sdk/<label>.env per app
#    (the URL and that app's write key: what its release build needs) and checks /healthz.
#
# --plan prints what would run and changes nothing. Run it yourself: it creates a
# service account, grants it roles and opens a public endpoint in your project.
set -eu

PROJECT="${1:-}"
[ -n "$PROJECT" ] || { echo "usage: sh deploy/cloudrun/bootstrap.sh <project> [--plan]" >&2; exit 2; }
PLAN=0
[ "${2:-}" = "--plan" ] && PLAN=1
BRANCH="${BRANCH:-main}"
APPS="${APPS:-}"
[ -n "$APPS" ] || { echo "set APPS=\"label=app_id|app_id ...\" (one bound write key per game)" >&2; exit 2; }
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
KEYS="$ROOT/deploy/cloudrun/make_keys.py"
umask 077
set -f   # APPS entries are split on spaces, never globbed

run gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
    cloudbuild.googleapis.com secretmanager.googleapis.com bigquery.googleapis.com \
    bigquerystorage.googleapis.com iam.googleapis.com --project "$PROJECT"

say "keys in $ENVF (readable by you only; never printed)"
if [ "$PLAN" = 1 ]; then
    # shellcheck disable=SC2086
    python3 "$KEYS" keys "$ENVF" --plan $APPS
else
    mkdir -p "$STATE"
    # shellcheck disable=SC2086
    python3 "$KEYS" keys "$ENVF" $APPS
fi

secret() {  # name, value from collector.env; never on the command line
    name=$1; var=$2
    if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
        say "secret $name exists: a new version if its value changed"
        cmd=add
    else
        cmd=create
    fi
    printf '+ gcloud secrets %s %s --data-file=- (value from %s)\n' "$cmd" "$name" "$ENVF"
    [ "$PLAN" = 1 ] && return 0
    val=$(sed -n "s/^$var=//p" "$ENVF")
    if [ "$cmd" = add ] && [ "$(gcloud secrets versions access latest --secret "$name" --project "$PROJECT" 2>/dev/null || true)" = "$val" ]; then
        say "secret $name is up to date"
        return 0
    fi
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
(umask 022 && git -C "$ROOT" archive "$BRANCH" | tar -x -C "$WORK")   # readable in the image
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
    python3 "$KEYS" sdk "$ENVF" "$STATE/sdk"
    curl -fsS "$URL/health" && echo   # Cloud Run keeps /healthz (paths ending in z)
    say "keys and URL: $ENVF; for each game's release build: $STATE/sdk/<label>.env"
fi
case "$WORK" in /tmp/*|/var/folders/*|/private/*) rm -r "$WORK" ;; esac
