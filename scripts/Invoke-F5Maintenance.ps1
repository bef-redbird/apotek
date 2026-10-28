param(
    [Parameter(Mandatory=$true)][string]$ConfigFile,
    [ValidateSet('update','resume','status','dry-run')][string]$Command = 'update',
    [switch]$AcquireOnly,
    [switch]$Recheck
)
$ErrorActionPreference = 'Stop'
$code = 1
$lock = $null
try {
    if ($AcquireOnly -and $Command -notin @('update', 'resume')) { throw 'AcquireOnly requires update or resume' }
    $resolvedConfig = [IO.Path]::GetFullPath($ConfigFile)
    $config = Get-Content -LiteralPath $resolvedConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($field in @('workspace', 'python_path', 'operation_root')) {
        $value = [string]$config.$field
        if (-not [IO.Path]::IsPathRooted($value) -or $value -match '[\x00-\x1f"]') { throw 'Invalid maintenance configuration' }
    }
    $root = [IO.Path]::GetFullPath([string]$config.operation_root)
    $cursor = $root
    while ($cursor) {
        if (Test-Path -LiteralPath (Join-Path $cursor '.git')) { throw 'Operations must remain outside Git' }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    $workspace = [string]$config.workspace
    if (-not (Test-Path -LiteralPath (Join-Path $workspace 'src\bef_redbird') -PathType Container)) {
        $workspace = Join-Path $workspace 'bef-redbird'
    }
    if (-not (Test-Path -LiteralPath ([string]$config.python_path) -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $workspace 'src\bef_redbird') -PathType Container)) { throw 'Missing runner prerequisites' }
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    # Read-only journal inspection remains available while a writer is running.
    # Exclusive handles serialize scheduled/manual updates and release after a crash.
    if ($Command -in @('update', 'resume')) {
        $lock = [IO.File]::Open((Join-Path $root 'maintenance.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } elseif ($Recheck) { throw 'Recheck requires an update or resume command' }
    Push-Location -LiteralPath $workspace
    try {
        $arguments = @('-B', '-m', 'bef_redbird.foundation.ftp_maintenance', $Command, '--config', $resolvedConfig)
        if ($AcquireOnly) { $arguments += '--acquire-only' }
        if ($Recheck) { $arguments += '--recheck' }
        & ([string]$config.python_path) @arguments
        $code = $LASTEXITCODE
    } finally { Pop-Location }
} catch {
    Write-Error -Message 'Maintenance failed or another maintenance runner is active; inspect private operation receipts' -ErrorAction Continue
    $code = 1
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
exit $code
