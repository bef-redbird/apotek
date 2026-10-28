param(
    [Parameter(Mandatory=$true)][string]$ConfigFile,
    [Parameter(Mandatory=$true)][string]$Account,
    [Parameter(Mandatory=$true)][string]$OutputFile,
    [ValidateSet('DownloadOnly','FullMaintenance')][string]$Mode = 'DownloadOnly',
    [ValidateSet('InteractiveToken','Password')][string]$LogonType = 'InteractiveToken',
    [datetime]$StartAt = [datetime]::MinValue,
    [switch]$NoCatchUp,
    [switch]$RequireAcPower
)
$ErrorActionPreference = 'Stop'
# Produce a disabled definition only. Never registers, enables, or starts a task.
$config = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not [IO.Path]::IsPathRooted([string]$config.workspace) -or
    -not [IO.Path]::IsPathRooted([string]$config.operation_root) -or
    -not [IO.Path]::IsPathRooted($ConfigFile) -or -not [IO.Path]::IsPathRooted($OutputFile)) { throw 'Absolute paths are required' }
$output = [IO.Path]::GetFullPath($OutputFile)
$allowed = [IO.Path]::GetFullPath([string]$config.operation_root) + '\'
if (-not $output.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) { throw 'Definition must be under the external operation root' }
$cursor = [IO.Path]::GetDirectoryName($output)
while ($cursor) {
    if (Test-Path -LiteralPath (Join-Path $cursor '.git')) { throw 'Definition must be outside Git' }
    $parent = [IO.Path]::GetDirectoryName($cursor)
    if ($parent -eq $cursor) { break }
    $cursor = $parent
}
if (Test-Path -LiteralPath $output) { throw 'Definition already exists; never overwrite' }
if ($Account -match '[\x00-\x1f"]' -or -not $Account.Trim()) { throw 'Explicit Windows account required' }
$now = Get-Date
if ($StartAt -eq [datetime]::MinValue) {
    $StartAt = $now.Date.AddHours(18).AddMinutes(30)
    if ($StartAt -le $now) { $StartAt = $StartAt.AddDays(1) }
}
if ($StartAt -le $now) { throw 'A future local start time is required' }
if ($StartAt.Kind -eq [DateTimeKind]::Utc) { throw 'Use a local wall-clock start time' }
$workspace = [string]$config.workspace
if (-not (Test-Path -LiteralPath (Join-Path $workspace 'src\bef_redbird') -PathType Container)) {
    $workspace = Join-Path $workspace 'bef-redbird'
}
if (-not (Test-Path -LiteralPath (Join-Path $workspace 'src\bef_redbird') -PathType Container)) { throw 'Missing application workspace' }
$runner = Join-Path $workspace 'scripts\Invoke-F5Maintenance.ps1'
if ($runner -match '[\x00-\x1f"]' -or $ConfigFile -match '[\x00-\x1f"]') { throw 'Invalid task command path' }
function Escape-Xml([string]$Value) { return [Security.SecurityElement]::Escape($Value) }
$command = Escape-Xml (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
$runnerArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $runner + '" -ConfigFile "' + $ConfigFile + '"'
if ($Mode -eq 'DownloadOnly') { $runnerArguments += ' -AcquireOnly' }
$arguments = Escape-Xml $runnerArguments
$principal = Escape-Xml $Account
$working = Escape-Xml $workspace
$boundary = $StartAt.ToString('yyyy-MM-ddTHH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
$catchUp = (-not $NoCatchUp).ToString().ToLowerInvariant()
$acPower = ($RequireAcPower -or $Mode -eq 'FullMaintenance').ToString().ToLowerInvariant()
# Full-history verification can run overnight; keep a bounded 48-hour limit.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>F5 $Mode using native FTP. Disabled pending explicit activation and the selected Windows account's DPAPI connection and permissions.</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>$boundary</StartBoundary><Enabled>false</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Collector"><UserId>$principal</UserId><LogonType>$LogonType</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>$acPower</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>$acPower</StopIfGoingOnBatteries><StartWhenAvailable>$catchUp</StartWhenAvailable><Enabled>false</Enabled><ExecutionTimeLimit>PT48H</ExecutionTimeLimit><Hidden>true</Hidden></Settings>
  <Actions Context="Collector"><Exec><Command>$command</Command><Arguments>$arguments</Arguments><WorkingDirectory>$working</WorkingDirectory></Exec></Actions>
</Task>
"@
New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($output)) -Force | Out-Null
[IO.File]::WriteAllText($output, $xml, [Text.Encoding]::Unicode)
Write-Output 'Disabled task definition prepared; no task installed. Selected-account native FTP and permission checks remain required before activation.'
