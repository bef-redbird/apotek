param([Parameter(Mandatory=$true)][string]$ConfigFile)
$ErrorActionPreference = 'Stop'
# Interactive local setup only. Never run from Task Scheduler. No network is used.
function Assert-PrivatePath([string]$Value) {
    if (-not [IO.Path]::IsPathRooted($Value)) { throw 'Absolute private path required' }
    $full = [IO.Path]::GetFullPath($Value)
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath (Join-Path $cursor '.git')) { throw 'Configuration must remain outside Git' }
        if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked setup paths are forbidden' }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $full
}
function Protect-LocalPath([string]$Path, [bool]$Directory) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = if ($Directory) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($identity)
    $inheritance = if ($Directory) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    foreach ($sid in @($identity, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', $inheritance, 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    if ($Directory) { [IO.Directory]::SetAccessControl($Path, $acl) } else { [IO.File]::SetAccessControl($Path, $acl) }
}
$form = $null
try {
    $configPath = Assert-PrivatePath $ConfigFile
    $configBytes = [IO.File]::ReadAllBytes($configPath)
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.version -ne 1) { throw 'Unsupported operating configuration' }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Security
    $form = New-Object Windows.Forms.Form
    $form.Text = 'Apotek: local FTP connection'
    $form.Size = [Drawing.Size]::new(530, 420)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true
    $fields = @{}
    $labels = @('Host', 'Port', 'Connection', 'Username', 'Password')
    for ($index = 0; $index -lt $labels.Count; $index++) {
        $label = New-Object Windows.Forms.Label
        $label.Text = $labels[$index]
        $label.Location = [Drawing.Point]::new(20, (24 + 43 * $index))
        $label.AutoSize = $true
        $form.Controls.Add($label)
        if ($labels[$index] -eq 'Connection') {
            $field = New-Object Windows.Forms.ComboBox
            $field.DropDownStyle = 'DropDownList'
            [void]$field.Items.Add('Explicit FTPS (verified TLS)')
            [void]$field.Items.Add('Plain FTP (unencrypted)')
            $field.SelectedIndex = 0
        } else {
            $field = New-Object Windows.Forms.TextBox
            if ($labels[$index] -eq 'Password') { $field.UseSystemPasswordChar = $true }
        }
        $field.Location = [Drawing.Point]::new(120, (20 + 43 * $index))
        $field.Size = [Drawing.Size]::new(365, 25)
        $fields[$labels[$index]] = $field
        $form.Controls.Add($field)
    }
    $fields['Port'].Text = '21'
    $encoding = 'utf-8'
    # Prefill only a previously configured native endpoint, never another client's store.
    if ($config.ftp_transport -and $config.ftp_transport.backend -eq 'python-ftplib') {
        $fields['Host'].Text = [string]$config.ftp_transport.host
        $fields['Port'].Text = [string]$config.ftp_transport.port
        if ($config.ftp_transport.encoding -cin @('utf-8', 'latin-1', 'cp1252')) { $encoding = [string]$config.ftp_transport.encoding }
        if ($config.ftp_transport.security -eq 'plain') { $fields['Connection'].SelectedIndex = 1 }
    }
    $notice = New-Object Windows.Forms.Label
    $notice.Location = [Drawing.Point]::new(20, 244)
    $notice.Size = [Drawing.Size]::new(465, 62)
    $updateNotice = {
        if ($fields['Connection'].SelectedIndex -eq 1) {
            $notice.Text = 'Plain FTP sends your username, password and files without network encryption. Choose it only if it is the source service required by your account. No automatic TLS fallback is used.'
            $notice.ForeColor = [Drawing.Color]::DarkRed
        } else {
            $notice.Text = 'TLS certificate verification and encrypted data transfers are required. Unsupported TLS or MLSD will fail without switching protocol. Credentials are protected for this Windows account only.'
            $notice.ForeColor = [Drawing.Color]::Black
        }
    }
    $fields['Connection'].add_SelectedIndexChanged($updateNotice)
    & $updateNotice
    $form.Controls.Add($notice)
    $save = New-Object Windows.Forms.Button
    $save.Text = 'Save locally'
    $save.Location = [Drawing.Point]::new(260, 325)
    $save.Size = [Drawing.Size]::new(110, 30)
    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = 'Cancel'
    $cancel.Location = [Drawing.Point]::new(380, 325)
    $cancel.Size = [Drawing.Size]::new(105, 30)
    $cancel.DialogResult = [Windows.Forms.DialogResult]::Cancel
    $form.CancelButton = $cancel
    $form.AcceptButton = $save
    $form.Controls.AddRange(@($save, $cancel))
    $save.add_Click({
        $portValue = 0
        if (-not $fields['Host'].Text.Trim() -or $fields['Host'].Text -match '[\s/@\\\x00-\x1f]' -or
            -not [int]::TryParse($fields['Port'].Text, [ref]$portValue) -or $portValue -lt 1 -or $portValue -gt 65535 -or
            -not $fields['Username'].Text -or -not $fields['Password'].Text -or
            ($fields['Username'].Text + $fields['Password'].Text) -match '[\x00\r\n]') {
            [void][Windows.Forms.MessageBox]::Show($form, 'Enter a host, port from 1 to 65535, username and password. Do not enter a URL.', 'Connection details')
            return
        }
        $form.DialogResult = [Windows.Forms.DialogResult]::OK
        $form.Close()
    })
    if ($form.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { exit 2 }
    $folder = Assert-PrivatePath (Join-Path ([IO.Path]::GetDirectoryName($configPath)) 'credentials')
    if (-not (Test-Path -LiteralPath $folder)) { [void][IO.Directory]::CreateDirectory($folder) }
    Protect-LocalPath $folder $true
    $credentialPath = Join-Path $folder ('ftp-current-user-' + [guid]::NewGuid().ToString('N') + '.bin')
    $payload = @{version=1; username=$fields['Username'].Text; password=$fields['Password'].Text} | ConvertTo-Json -Compress
    $plain = [Text.Encoding]::UTF8.GetBytes($payload)
    try {
        $encrypted = [Security.Cryptography.ProtectedData]::Protect($plain, [Text.Encoding]::UTF8.GetBytes('apotek-ftp-credentials-v1'), [Security.Cryptography.DataProtectionScope]::CurrentUser)
        $header = [Text.Encoding]::ASCII.GetBytes("APOTEK-FTP-DPAPI-V1`n")
        $stream = [IO.File]::Open($credentialPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($header, 0, $header.Length); $stream.Write($encrypted, 0, $encrypted.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        Protect-LocalPath $credentialPath $false
    } finally {
        [Array]::Clear($plain, 0, $plain.Length)
        $payload = $null
        $fields['Password'].Clear()
    }
    $transport = [ordered]@{version=1; backend='python-ftplib'; host=$fields['Host'].Text.Trim(); port=[int]$fields['Port'].Text; security=$(if ($fields['Connection'].SelectedIndex -eq 0) {'explicit_tls'} else {'plain'}); credential_file=$credentialPath; encoding=$encoding; timeout_seconds=30; batch_timeout_seconds=300}
    $config | Add-Member -NotePropertyName ftp_transport -NotePropertyValue $transport -Force
    # A config edited while the dialog was open must not be silently overwritten.
    if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -ne [Convert]::ToBase64String($configBytes)) { throw 'Configuration changed during setup' }
    $temporary = $configPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $backup = $configPath + '.before-native-' + [guid]::NewGuid().ToString('N') + '.json'
    [IO.File]::WriteAllText($temporary, ($config | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    [IO.File]::Replace($temporary, $configPath, $backup)
    Write-Output 'Local native FTP configuration saved. No connection or download was attempted.'
} catch {
    # Do not expose entered fields, exception messages or credential material.
    Write-Error 'Local FTP setup did not complete. No connection was attempted.' -ErrorAction Continue
    exit 1
} finally {
    if ($null -ne $form) { $form.Dispose() }
}
