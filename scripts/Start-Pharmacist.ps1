param(
    [Parameter(Mandatory=$true)][string]$ConfigFile,
    [int]$Port = 8530,
    [switch]$Stop,
    [switch]$Restart
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$configPath = (Resolve-Path -LiteralPath $ConfigFile).Path
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$statePath = Join-Path $settings.root 'viewer-process.json'
if (Test-Path -LiteralPath $statePath) {
    $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $($saved.pid)"
    if ($existing -and $existing.CommandLine -and $existing.CommandLine.Contains('pharmacist_app.py') -and
        $existing.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$saved.created).ToUniversalTime().Ticks) {
        if ($Stop -or $Restart) {
            # Windows virtual-environment Python may have a forwarding parent.
            Get-CimInstance Win32_Process -Filter "ParentProcessId = $($saved.pid)" |
                Where-Object { $_.CommandLine -and $_.CommandLine.Contains('pharmacist_app.py') -and $_.CommandLine.Contains('streamlit') } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
            Stop-Process -Id $saved.pid -ErrorAction SilentlyContinue
        }
        else { Start-Process "http://127.0.0.1:$($saved.port)"; exit 0 }
    }
}
if ($Stop) { exit 0 }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port er allerede i brug. Stop den kendte app eller vælg en anden port."
}
Push-Location $repo
$priorEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    # Sync the same locked inference environment; no dev tooling is required.
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $settings.root 'runtime'
    & uv sync --locked --no-dev --group inference
    if ($LASTEXITCODE -ne 0) { throw 'Det låste Python-miljø kunne ikke installeres.' }
    $env:REDBIRD_ASSESSMENT_CONFIG = $configPath
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $python = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts/python.exe'
    $out = Join-Path $settings.root 'viewer.stdout.log'
    $err = Join-Path $settings.root 'viewer.stderr.log'
    $process = Start-Process -FilePath $python -ArgumentList @('-B','-m','streamlit','run','pharmacist_app.py','--server.address','127.0.0.1','--server.port',"$Port",'--server.headless','true','--server.fileWatcherType','none','--browser.gatherUsageStats','false') -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $native = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    @{pid=$process.Id; created=$native.CreationDate.ToUniversalTime().ToString('o'); port=$Port} | ConvertTo-Json | Set-Content -LiteralPath $statePath
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try { $response = Invoke-WebRequest "http://127.0.0.1:$Port/_stcore/health" -UseBasicParsing; if ($response.StatusCode -eq 200) { $ready = $true; break } } catch {}
        if ($process.HasExited) { break }
    }
    if (-not $ready) { throw "Appen kunne ikke starte. Se private logfiler i operatørens datamappe." }
    Start-Process "http://127.0.0.1:$Port"
    Write-Output "Apotek kører på http://127.0.0.1:$Port. Stop med samme kommando og -Stop; genstart med -Restart."
} finally { $env:UV_PROJECT_ENVIRONMENT = $priorEnvironment; Pop-Location }
