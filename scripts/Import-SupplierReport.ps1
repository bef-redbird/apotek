param(
    [Parameter(Mandatory=$true)][string]$Path,
    [string]$ConfigFile = '..\data\foundation-operations\config.json'
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$code = 1
$lock = $null
$previousPythonPath = $env:PYTHONPATH
try {
    $repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'supplier_input_not_found' }
    $sourceFile = (Resolve-Path -LiteralPath $Path).Path
    if (-not [IO.Path]::IsPathRooted($ConfigFile)) {
        $ConfigFile = Join-Path $repoRoot $ConfigFile
    }
    if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) { throw 'supplier_configuration_not_found' }
    $resolvedConfig = (Resolve-Path -LiteralPath $ConfigFile).Path
    $config = Get-Content -LiteralPath $resolvedConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    $python = [string]$config.python_path
    if (-not $python) { $python = Join-Path $repoRoot '.venv\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'supplier_runner_not_found' }
    $operationRoot = [IO.Path]::GetFullPath([string]$config.operation_root)
    New-Item -ItemType Directory -Path $operationRoot -Force | Out-Null
    # Coordinate with the ordinary manual FTP wrapper for the complete intake/publication.
    $lock = [IO.File]::Open((Join-Path $operationRoot 'maintenance.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $env:PYTHONPATH = Join-Path $repoRoot 'src'
    Push-Location -LiteralPath $repoRoot
    try {
        & $python -m bef_redbird.foundation.supplier_intake --config $resolvedConfig --publish $sourceFile
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    if ($code -eq 2) {
        Write-Warning 'Verified report revision needs review; current supplier facts were preserved.'
    } elseif ($code -ne 0) {
        Write-Error -Message 'Supplier intake or publication failed; inspect private supplier status and publication receipts.' -ErrorAction Continue
    }
} catch {
    $messages = @{
        supplier_input_not_found = 'supplier_input_not_found: Select an existing downloaded report file and retry -Path.'
        supplier_configuration_not_found = 'supplier_configuration_not_found: Select an existing operating configuration with -ConfigFile.'
        supplier_runner_not_found = 'supplier_runner_not_found: Check python_path in the operating configuration.'
    }
    $message = $messages[[string]$_.Exception.Message]
    if (-not $message) { $message = 'supplier_operation_unavailable: Check the operating config/storage and wait for any active maintenance runner before retrying.' }
    Write-Error -Message $message -ErrorAction Continue
    $code = 1
} finally {
    $env:PYTHONPATH = $previousPythonPath
    if ($null -ne $lock) { $lock.Dispose() }
}
exit $code
