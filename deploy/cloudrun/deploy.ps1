# Deploy the Studio Pulse collector to Cloud Run (cloud mode, BigQuery). Windows PowerShell 5.1+ / pwsh.
#
# Prints every command and changes nothing unless -Apply is given.
#   $env:PROJECT = "my-project"; .\deploy\cloudrun\deploy.ps1           # print the plan
#   $env:PROJECT = "my-project"; .\deploy\cloudrun\deploy.ps1 -Apply    # run it
#
# Needs gcloud and bq (Google Cloud SDK), logged in, and the Secret Manager
# secrets pulse-write-keys and pulse-read-key. Run from the repository root.
param([switch]$Apply)
$ErrorActionPreference = 'Stop'

if (-not $env:PROJECT) { throw 'set $env:PROJECT to your Google Cloud project id' }
$Project = $env:PROJECT
$Region = if ($env:REGION) { $env:REGION } else { 'us-central1' }
$BqLocation = if ($env:BQ_LOCATION) { $env:BQ_LOCATION } else { 'US' }
$Dataset = if ($env:DATASET) { $env:DATASET } else { 'pulse_raw' }
$Service = if ($env:SERVICE) { $env:SERVICE } else { 'pulse-collector' }
$Repo = if ($env:REPO) { $env:REPO } else { 'pulse' }
$Tag = if ($env:TAG) { $env:TAG } else { Get-Date -Format 'yyyyMMdd-HHmmss' }
$Image = "$Region-docker.pkg.dev/$Project/$Repo/${Service}:$Tag"
$Sa = "$Service@$Project.iam.gserviceaccount.com"
$RetentionS = 34214400   # 396 days
$Schemas = 'contract/generated/bigquery'

if (-not (Test-Path "$Schemas/events.json")) { throw 'run from the repository root' }

# Each command is one array, so "--flag" tokens reach the native tool untouched.
function Run([string[]]$Cmd) {
  Write-Host ('+ ' + ($Cmd -join ' '))
  if ($Apply) {
    $rest = @()
    if ($Cmd.Length -gt 1) { $rest = $Cmd[1..($Cmd.Length - 1)] }
    & $Cmd[0] @rest
    if ($LASTEXITCODE) { throw "failed: $($Cmd -join ' ')" }
  }
}

Run @('bq', "--location=$BqLocation", 'mk', '--dataset', "--default_partition_expiration=$RetentionS", "${Project}:$Dataset")
Run @('bq', 'mk', '--table', '--time_partitioning_field', 'received_at', '--time_partitioning_type', 'DAY', '--clustering_fields', 'app_id,name', "${Project}:$Dataset.events", "$Schemas/events.json")
Run @('bq', 'mk', '--table', '--time_partitioning_field', 'received_at', '--time_partitioning_type', 'DAY', "${Project}:$Dataset.quarantine", "$Schemas/quarantine.json")
Run @('bq', 'mk', '--table', "${Project}:$Dataset.app_registry", "$Schemas/app_registry.json")
Run @('bq', 'mk', '--table', "${Project}:$Dataset.deletion_requests", "$Schemas/deletion_requests.json")
Run @('gcloud', 'iam', 'service-accounts', 'create', $Service, '--project', $Project, '--display-name', "Studio Pulse collector")
Run @('bq', 'add-iam-policy-binding', "--member=serviceAccount:$Sa", '--role=roles/bigquery.dataEditor', "${Project}:$Dataset")
Run @('gcloud', 'projects', 'add-iam-policy-binding', $Project, "--member=serviceAccount:$Sa", '--role=roles/bigquery.jobUser')
Run @('gcloud', 'secrets', 'add-iam-policy-binding', 'pulse-write-keys', '--project', $Project, "--member=serviceAccount:$Sa", '--role=roles/secretmanager.secretAccessor')
Run @('gcloud', 'secrets', 'add-iam-policy-binding', 'pulse-read-key', '--project', $Project, "--member=serviceAccount:$Sa", '--role=roles/secretmanager.secretAccessor')
Run @('gcloud', 'artifacts', 'repositories', 'create', $Repo, '--project', $Project, '--location', $Region, '--repository-format', 'docker')
Run @('gcloud', 'builds', 'submit', '--project', $Project, '--config', 'deploy/cloudrun/cloudbuild.yaml', '--substitutions', "_IMAGE=$Image", '.')
Run @('gcloud', 'run', 'deploy', $Service, '--project', $Project, '--region', $Region, '--image', $Image, '--service-account', $Sa, '--allow-unauthenticated', '--max-instances=1', '--concurrency=200', '--memory=512Mi', '--cpu=1', '--timeout=30', '--set-env-vars', "PULSE_COLLECTOR_MODE=bigquery,PULSE_BQ_PROJECT=$Project,PULSE_BQ_DATASET=$Dataset,PULSE_BQ_LOCATION=$BqLocation,PULSE_TRUSTED_PROXY_HOPS=1", '--set-secrets', "PULSE_WRITE_KEYS=pulse-write-keys:latest,PULSE_READ_KEY=pulse-read-key:latest")

if (-not $Apply) { Write-Host '(plan only: nothing was changed; add -Apply to run these commands)' }
