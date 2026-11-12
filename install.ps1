# install.ps1 - studio-pulse for Windows 10/11 (x64). No admin rights and no system Python needed:
# uv installs Python and the tool for this user.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/emirhann-akca/studio-pulse/main/install.ps1 | iex"
#
# Source: the wheel of the v0.2.0 GitHub release when it exists; when that download answers 404
# (no release published), the main-branch archive, built locally by uv. It says which one it used.
#
# Options (environment variables), as in install.sh:
#   PULSE_SOURCE       wheel or source archive (path or URL) to install; skips the release lookup
#   PULSE_ARCHIVE_URL  archive used when there is no release (default: the main branch zip)
#   PULSE_RELEASE_BASE where release wheels are looked up (default: this repository's releases)
#   PULSE_EXTRAS   extras to install, default "gcp,ua"; add "forecast" or "stores"; "none" for none
#   PULSE_PYTHON   Python version for the tool, default 3.13
#   PULSE_DEMO=1   build the demo dashboard afterwards (in PULSE_DEMO_DIR, default %USERPROFILE%\studio-pulse-demo)
#   PULSE_NO_SHORTCUT=1   do not add the "Studio Pulse" Desktop and Start menu shortcuts
#   UV_NO_MODIFY_PATH=1   leave the user PATH alone
#
# Rules for an irm | iex script: ASCII only, everything inside one scriptblock (nothing leaks into
# the caller's session), and never `exit` (it would close the user's window); errors are thrown.
& {
    $ErrorActionPreference = 'Stop'
    $Version = '0.2.0'
    $UvVersion = '0.13.7'
    $Repo = 'https://github.com/emirhann-akca/studio-pulse'
    $Python = if ($env:PULSE_PYTHON) { $env:PULSE_PYTHON } else { '3.13' }
    $Extras = if ($env:PULSE_EXTRAS) { $env:PULSE_EXTRAS } else { 'gcp,ua' }
    $ReleaseBase = if ($env:PULSE_RELEASE_BASE) { $env:PULSE_RELEASE_BASE } else { "$Repo/releases/download" }
    $Archive = if ($env:PULSE_ARCHIVE_URL) { $env:PULSE_ARCHIVE_URL } else { "$Repo/archive/refs/heads/main.zip" }

    function Invoke-Native([string]$Exe, [string[]]$ArgList) {
        & $Exe @ArgList
        if ($LASTEXITCODE -ne 0) { throw "$Exe $($ArgList -join ' ') failed with exit code $LASTEXITCODE" }
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest's progress bar slows 5.1 to a crawl
    $wheelDir = $null
    if ($env:PULSE_SOURCE) {
        $Source = $env:PULSE_SOURCE
        Write-Host "pulse-install: source: PULSE_SOURCE ($Source)"
    } else {
        $WheelName = "studio_pulse-$Version-py3-none-any.whl"
        $Wheel = "$ReleaseBase/v$Version/$WheelName"
        $wheelDir = Join-Path ([IO.Path]::GetTempPath()) ('pulse-wheel-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $wheelDir | Out-Null
        $wheelFile = Join-Path $wheelDir $WheelName
        # Windows PowerShell 5.1 sometimes sees GitHub close the connection on a 404 instead of
        # reading the response (status 0): no keep-alive and up to three tries, then curl.exe
        # (Windows 10 1803+) reads the status code where .NET could not. The same logic as
        # creative-forge's installer.
        $status = 0
        $lastError = ''
        for ($attempt = 1; $attempt -le 3 -and $status -eq 0; $attempt++) {
            try {
                Invoke-WebRequest -Uri $Wheel -OutFile $wheelFile -UseBasicParsing -DisableKeepAlive
                $status = 200
            } catch {
                $lastError = $_.Exception.Message
                $response = $_.Exception.Response
                if ($response) { $status = [int]$response.StatusCode }
                if ($status -eq 0) { Start-Sleep -Seconds (2 * $attempt) }
            }
        }
        $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($status -eq 0 -and $curl) {
            $code = & $curl.Source -s -L --retry 2 -o $wheelFile -w '%{http_code}' $Wheel
            if ("$code" -match '^\d{3}$') { $status = [int]"$code" }
        }
        if ($status -eq 200) {
            $Source = $wheelFile
            Write-Host "pulse-install: source: release wheel v$Version ($Wheel)"
        } elseif ($status -eq 404) {
            $Source = $Archive
            Write-Host "pulse-install: no release wheel for v$Version (HTTP 404); source: branch archive ($Archive)"
        } else {
            throw "cannot download $Wheel (HTTP $status; $lastError); check the connection or set PULSE_SOURCE"
        }
    }
    if (Test-Path -LiteralPath $Source) {
        $Source = ([System.Uri](Resolve-Path -LiteralPath $Source).Path).AbsoluteUri
    }

    $uvDir = if ($env:UV_INSTALL_DIR) { $env:UV_INSTALL_DIR } else { Join-Path $env:USERPROFILE '.local\bin' }
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        $candidate = Join-Path $uvDir 'uv.exe'
        if (-not (Test-Path -LiteralPath $candidate)) {
            Write-Host "pulse-install: installing uv $UvVersion into $uvDir"
            # A child process with a process-scoped policy: uv's installer refuses Restricted/AllSigned.
            Invoke-Native 'powershell' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                "irm https://astral.sh/uv/$UvVersion/install.ps1 | iex")
        }
        $uv = $candidate
        if (-not (Test-Path -LiteralPath $uv)) { throw "uv not found at $uv after installation" }
    }
    Write-Host "pulse-install: using $(& $uv --version)"

    $spec = if ($Extras -and $Extras -ne 'none') { "studio-pulse[$Extras]" } else { 'studio-pulse' }
    Write-Host "pulse-install: installing $spec with Python $Python (uv downloads Python if it is missing)"
    # --refresh-package: a branch archive keeps its URL while its contents change
    try {
        Invoke-Native $uv @('tool', 'install', '--force', '--refresh-package', 'studio-pulse',
            '--python', $Python, "$spec @ $Source")
    } finally {
        if ($wheelDir) { Remove-Item -LiteralPath $wheelDir -Recurse -Force -ErrorAction SilentlyContinue }
    }

    $bin = (& $uv tool dir --bin).Trim()
    if (-not (($env:Path -split ';') -contains $bin)) { $env:Path = "$bin;$env:Path" }
    if (-not $env:UV_NO_MODIFY_PATH) {
        # Windows PowerShell 5.1 turns a redirected native stderr line into a terminating
        # error under 'Stop'; this call is best effort, so relax it here only.
        $ErrorActionPreference = 'Continue'
        & $uv tool update-shell 2>&1 | Out-Null
        $ErrorActionPreference = 'Stop'
    }
    Invoke-Native (Join-Path $bin 'pulse.exe') @('doctor')

    $launcher = $false
    if ($env:PULSE_NO_SHORTCUT -ne '1') {
        try {
            Invoke-Native (Join-Path $bin 'pulse.exe') @('app', '--install-shortcut')
            $launcher = $true
        } catch {
            Write-Host "pulse-install: could not add the Desktop / Start menu shortcut ($_); 'pulse app' opens the same window"
        }
    }

    if ($env:PULSE_DEMO -eq '1') {
        $demo = if ($env:PULSE_DEMO_DIR) { $env:PULSE_DEMO_DIR } else { Join-Path $env:USERPROFILE 'studio-pulse-demo' }
        New-Item -ItemType Directory -Force -Path $demo | Out-Null
        Push-Location -LiteralPath $demo
        try { Invoke-Native (Join-Path $bin 'pulse.exe') @('demo', '--scale', '0.3') } finally { Pop-Location }
        Write-Host "pulse-install: demo dashboard: $demo\demo-site\index.html"
    }
    if ($launcher) {
        Write-Host "pulse-install: done. Open 'Studio Pulse' from the Start menu or the Desktop (or, in a NEW terminal: pulse app)"
    } else {
        Write-Host 'pulse-install: done. Open a NEW terminal, then run: pulse app'
    }
}
