param([Parameter(Mandatory=$true)][string]$RequestFile)
$ErrorActionPreference = 'Stop'
# Historical helper retired. Never reads saved-client sessions or connects.
$request = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Error 'The old transfer helper is retired. Configure native FTP locally and use Invoke-F5Maintenance.ps1.'
exit 1
