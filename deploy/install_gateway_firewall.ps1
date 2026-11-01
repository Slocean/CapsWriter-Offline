param([Parameter(Mandatory=$true)][string]$AppDir, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator privileges are required' }
$AppDir = [IO.Path]::GetFullPath($AppDir).TrimEnd('\')
if ($AppDir -match '["\r\n]') { throw 'Invalid application directory' }
foreach ($name in 'start_server.exe', 'start_admin.exe') {
    if (!(Test-Path -LiteralPath (Join-Path $AppDir $name) -PathType Leaf)) { throw "Missing $name" }
}
$taskName = 'CapsWriterGatewayFirewallSync'
$targetDir = Join-Path $env:ProgramData 'CapsWriterGateway'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Firewall sync task already exists' }
if (Test-Path -LiteralPath $targetDir) { throw 'Firewall sync directory already exists' }
$source = Join-Path $PSScriptRoot 'sync_gateway_firewall.ps1'
if (!(Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Missing firewall sync script' }
$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$parseErrors) | Out-Null
if ($parseErrors.Count) { throw 'Invalid firewall sync script syntax' }
. $source -AppDir $AppDir -FunctionsOnly
$block = @(Get-NetFirewallRule -Name $blockName -ErrorAction SilentlyContinue)
if ($block.Count -gt 1) { throw 'Admin block rule is not unique' }
if ($block.Count) {
    AssertRuleScope $block[0] (Join-Path $AppDir 'start_admin.exe') 'Block'
    $address = $block[0] | Get-NetFirewallAddressFilter
    $port = $block[0] | Get-NetFirewallPortFilter
    if ($block[0].Enabled -ne 'True' -or $port.LocalPort -ne '6017' -or $address.LocalAddress -ne 'Any') {
        throw 'Admin block rule does not match expected filters'
    }
}
if ($CheckOnly) { Write-Output 'FIREWALL_SYNC_PREFLIGHT_OK'; return }

New-Item -ItemType Directory -Path $targetDir | Out-Null
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in 'S-1-5-18', 'S-1-5-32-544') {
    $identity = [Security.Principal.SecurityIdentifier]::new($sid)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl',
        'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $targetDir -AclObject $acl
$target = Join-Path $targetDir 'sync_gateway_firewall.ps1'
Copy-Item -LiteralPath $source -Destination $target
if ((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash) {
    throw 'Firewall sync script hash mismatch'
}
$backup = @{block_existed = [bool]$block.Count; remote = if ($block.Count) { @($address.RemoteAddress) } else { @() }}
$backup | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $targetDir 'original-block.json') -Encoding UTF8
$exe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File `"$target`" -AppDir `"$AppDir`""
$action = New-ScheduledTaskAction -Execute $exe -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'FIREWALL_SYNC_TASK_INSTALLED'
