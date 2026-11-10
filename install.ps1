# install.ps1 - studio-pulse for Windows 10/11 (x64). No admin rights and no system Python needed:
# uv installs Python and the tool for this user.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/emirhann-akca/studio-pulse/v0.2.0/install.ps1 | iex"
#
# Options (environment variables), as in install.sh:
#   PULSE_SOURCE   wheel path or URL to install (default: the v0.2.0 release wheel)
#   PULSE_EXTRAS   extras to install, default "gcp"; "none" for none
#   PULSE_PYTHON   Python version for the tool, default 3.13
#   PULSE_DEMO=1   build the demo dashboard afterwards (in PULSE_DEMO_DIR, default %USERPROFILE%\studio-pulse-demo)
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
    $Extras = if ($env:PULSE_EXTRAS) { $env:PULSE_EXTRAS } else { 'gcp' }
    $Source = if ($env:PULSE_SOURCE) { $env:PULSE_SOURCE } else {
        "$Repo/releases/download/v$Version/studio_pulse-$Version-py3-none-any.whl" }
    if (Test-Path -LiteralPath $Source) {
        $Source = ([System.Uri](Resolve-Path -LiteralPath $Source).Path).AbsoluteUri
    }

    function Invoke-Native([string]$Exe, [string[]]$ArgList) {
        & $Exe @ArgList
        if ($LASTEXITCODE -ne 0) { throw "$Exe $($ArgList -join ' ') failed with exit code $LASTEXITCODE" }
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

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
    Write-Host "pulse-install: installing $spec $Version with Python $Python (uv downloads Python if it is missing)"
    Invoke-Native $uv @('tool', 'install', '--force', '--python', $Python, "$spec @ $Source")

    $bin = (& $uv tool dir --bin).Trim()
    if (-not (($env:Path -split ';') -contains $bin)) { $env:Path = "$bin;$env:Path" }
    if (-not $env:UV_NO_MODIFY_PATH) { & $uv tool update-shell *> $null }
    Invoke-Native (Join-Path $bin 'pulse.exe') @('doctor')

    if ($env:PULSE_DEMO -eq '1') {
        $demo = if ($env:PULSE_DEMO_DIR) { $env:PULSE_DEMO_DIR } else { Join-Path $env:USERPROFILE 'studio-pulse-demo' }
        New-Item -ItemType Directory -Force -Path $demo | Out-Null
        Push-Location -LiteralPath $demo
        try { Invoke-Native (Join-Path $bin 'pulse.exe') @('demo', '--scale', '0.3') } finally { Pop-Location }
        Write-Host "pulse-install: demo dashboard: $demo\demo-site\index.html"
    }
    Write-Host 'pulse-install: done. Open a NEW terminal, then run: pulse demo --open'
}
