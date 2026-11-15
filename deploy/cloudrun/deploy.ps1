# Deploy the Studio Pulse collector to Cloud Run (cloud mode, BigQuery). Windows PowerShell 5.1+ / pwsh.
#
# Prints every command and changes nothing unless -Apply is given. With -Apply a
# resource that exists already is kept, so a second run only builds and rolls out.
# Size and cost limits as in deploy.sh: $env:MIN_INSTANCES (0), MAX_INSTANCES (1),
# CONCURRENCY (40), MEMORY (1Gi), CPU (1), DEDUPE_CAPACITY (1000000).
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
$MinInstances = if ($env:MIN_INSTANCES) { $env:MIN_INSTANCES } else { '0' }
$MaxInstances = if ($env:MAX_INSTANCES) { $env:MAX_INSTANCES } else { '1' }
$Concurrency = if ($env:CONCURRENCY) { $env:CONCURRENCY } else { '40' }
$Memory = if ($env:MEMORY) { $env:MEMORY } else { '1Gi' }
$Cpu = if ($env:CPU) { $env:CPU } else { '1' }
$DedupeCapacity = if ($env:DEDUPE_CAPACITY) { $env:DEDUPE_CAPACITY } else { '1000000' }
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

# True when the resource is absent (always for the plan).
function Missing([string[]]$Check) {
  if (-not $Apply) { return $true }
  $rest = @()
  if ($Check.Length -gt 1) { $rest = $Check[1..($Check.Length - 1)] }
  $eap = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'   # PowerShell 5.1 turns redirected stderr into errors
  try { & $Check[0] @rest *> $null } catch { } finally { $ErrorActionPreference = $eap }
  if ($LASTEXITCODE) { return $true }
  Write-Host ('  (exists, kept: ' + ($Check -join ' ') + ')')
  return $false
}

if (Missing @('bq', 'show', "${Project}:$Dataset")) { Run @('bq', "--location=$BqLocation", 'mk', '--dataset', "--default_partition_expiration=$RetentionS", "${Project}:$Dataset") }
if (Missing @('bq', 'show', "${Project}:$Dataset.events")) { Run @('bq', 'mk', '--table', '--time_partitioning_field', 'received_at', '--time_partitioning_type', 'DAY', '--clustering_fields', 'app_id,name', "${Project}:$Dataset.events", "$Schemas/events.json") }
if (Missing @('bq', 'show', "${Project}:$Dataset.quarantine")) { Run @('bq', 'mk', '--table', '--time_partitioning_field', 'received_at', '--time_partitioning_type', 'DAY', "${Project}:$Dataset.quarantine", "$Schemas/quarantine.json") }
if (Missing @('bq', 'show', "${Project}:$Dataset.app_registry")) { Run @('bq', 'mk', '--table', "${Project}:$Dataset.app_registry", "$Schemas/app_registry.json") }
if (Missing @('bq', 'show', "${Project}:$Dataset.deletion_requests")) { Run @('bq', 'mk', '--table', "${Project}:$Dataset.deletion_requests", "$Schemas/deletion_requests.json") }
if (Missing @('gcloud', 'iam', 'service-accounts', 'describe', $Sa, '--project', $Project)) { Run @('gcloud', 'iam', 'service-accounts', 'create', $Service, '--project', $Project, '--display-name', "Studio Pulse collector") }
# dataset-level IAM through SQL (DCL): `bq add-iam-policy-binding -d` needs allowlisting
Run @('bq', 'query', "--project_id=$Project", "--location=$BqLocation", '--nouse_legacy_sql', "GRANT ``roles/bigquery.dataEditor`` ON SCHEMA ``$Project.$Dataset`` TO 'serviceAccount:$Sa'")
Run @('gcloud', 'projects', 'add-iam-policy-binding', $Project, "--member=serviceAccount:$Sa", '--role=roles/bigquery.jobUser')
Run @('gcloud', 'secrets', 'add-iam-policy-binding', 'pulse-write-keys', '--project', $Project, "--member=serviceAccount:$Sa", '--role=roles/secretmanager.secretAccessor')
Run @('gcloud', 'secrets', 'add-iam-policy-binding', 'pulse-read-key', '--project', $Project, "--member=serviceAccount:$Sa", '--role=roles/secretmanager.secretAccessor')
if (Missing @('gcloud', 'artifacts', 'repositories', 'describe', $Repo, '--project', $Project, '--location', $Region)) { Run @('gcloud', 'artifacts', 'repositories', 'create', $Repo, '--project', $Project, '--location', $Region, '--repository-format', 'docker') }
Run @('gcloud', 'builds', 'submit', '--project', $Project, '--config', 'deploy/cloudrun/cloudbuild.yaml', '--substitutions', "_IMAGE=$Image", '.')
Run @('gcloud', 'run', 'deploy', $Service, '--project', $Project, '--region', $Region, '--image', $Image, '--service-account', $Sa, '--allow-unauthenticated', "--min-instances=$MinInstances", "--max-instances=$MaxInstances", "--concurrency=$Concurrency", "--memory=$Memory", "--cpu=$Cpu", '--cpu-throttling', '--timeout=30', '--set-env-vars', "PULSE_COLLECTOR_MODE=bigquery,PULSE_BQ_PROJECT=$Project,PULSE_BQ_DATASET=$Dataset,PULSE_BQ_LOCATION=$BqLocation,PULSE_TRUSTED_PROXY_HOPS=1,PULSE_DEDUPE_CAPACITY=$DedupeCapacity", '--set-secrets', "PULSE_WRITE_KEYS=pulse-write-keys:latest,PULSE_READ_KEY=pulse-read-key:latest")

if (-not $Apply) { Write-Host '(plan only: nothing was changed; add -Apply to run these commands)' }
