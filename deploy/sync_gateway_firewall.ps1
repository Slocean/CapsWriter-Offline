param(
    [Parameter(Mandatory=$true)][string]$AppDir,
    [int]$AdminPort = 6017,
    [switch]$Once,
    [switch]$FunctionsOnly
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$AppDir = [IO.Path]::GetFullPath($AppDir).TrimEnd('\')
$serverExe = Join-Path $AppDir 'start_server.exe'
$adminExe = Join-Path $AppDir 'start_admin.exe'
$routeDir = Join-Path $AppDir 'gateway-endpoint'
$asrRule = 'CapsWriter-gateway-ASR-local'
$adminRule = 'CapsWriter-gateway-admin-local'
$blockName = 'CapsWriter-Offline admin 6017 untrusted-src block'

function IpNumber([string]$value) {
    $ip = [Net.IPAddress]::Parse($value)
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'IPv4 required' }
    $bytes = $ip.GetAddressBytes()
    [array]::Reverse($bytes)
    return [uint64][BitConverter]::ToUInt32($bytes, 0)
}

function NumberIp([uint64]$value) {
    $bytes = [BitConverter]::GetBytes([uint32]$value)
    [array]::Reverse($bytes)
    return ([Net.IPAddress]::new($bytes)).ToString()
}

function BlockComplement([string[]]$addresses) {
    $numbers = @($addresses | ForEach-Object { IpNumber $_ } | Sort-Object -Unique)
    $result = [Collections.Generic.List[string]]::new()
    [uint64]$start = 0
    foreach ($n in $numbers) {
        if ($n -gt $start) { $result.Add("$(NumberIp $start)-$(NumberIp ($n - 1))") }
        $start = $n + 1
    }
    if ($start -le [uint32]::MaxValue) { $result.Add("$(NumberIp $start)-255.255.255.255") }
    return @($result)
}

function SameValues($a, $b) {
    return ((@($a) | Sort-Object) -join ',') -eq ((@($b) | Sort-Object) -join ',')
}

function AssertRuleScope($rule, [string]$exe, [string]$action) {
    $app = $rule | Get-NetFirewallApplicationFilter
    $service = $rule | Get-NetFirewallServiceFilter
    $iface = $rule | Get-NetFirewallInterfaceFilter
    $iftype = $rule | Get-NetFirewallInterfaceTypeFilter
    $security = $rule | Get-NetFirewallSecurityFilter
    $ports = $rule | Get-NetFirewallPortFilter
    if ($rule.Action -ne $action -or $rule.Direction -ne 'Inbound' -or $rule.Profile -ne 'Any' -or
        $rule.Owner -or $rule.PolicyAppId -or @($rule.Platforms).Count -ne 0 -or
        @($rule.RemoteDynamicKeywordAddresses).Count -ne 0 -or $rule.PolicyStoreSourceType -ne 'Local' -or
        $rule.LocalOnlyMapping -ne $false -or $rule.LooseSourceMapping -ne $false -or
        $app.Program -ne $exe -or ($app.Package -and $app.Package -ne 'Any') -or
        $service.Service -ne 'Any' -or $iface.InterfaceAlias -ne 'Any' -or $iftype.InterfaceType -ne 'Any' -or
        $ports.Protocol -ne 'TCP' -or $ports.RemotePort -ne 'Any' -or
        $ports.IcmpType -ne 'Any' -or $ports.DynamicTarget -ne 'Any' -or
        $security.Authentication -ne 'NotRequired' -or $security.Encryption -ne 'NotRequired' -or
        $security.OverrideBlockRules -ne $false -or $security.LocalUser -ne 'Any' -or
        $security.RemoteUser -ne 'Any' -or $security.RemoteMachine -ne 'Any') {
        throw "Firewall rule has unexpected restrictions: $($rule.Name)"
    }
}

function ReadRoutes([string]$name, [string]$exe) {
    $path = Join-Path $routeDir $name
    if (!(Test-Path -LiteralPath $path)) { return $null }
    $file = Get-Item -LiteralPath $path
    if (((Get-Date).ToUniversalTime() - $file.LastWriteTimeUtc).TotalSeconds -gt 12) { return $null }
    $local = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred | Select-Object -ExpandProperty IPAddress)
    $addresses = [Collections.Generic.List[string]]::new()
    $port = 0
    foreach ($line in @(Get-Content -LiteralPath $path)) {
        if ($line -notmatch '^([0-9.]+):([0-9]{1,5})$') { throw 'Invalid endpoint file' }
        $ip = $Matches[1]
        $p = [int]$Matches[2]
        if ($p -lt 1 -or $p -gt 65535 -or $local -notcontains $ip) { throw 'Invalid endpoint' }
        if ($port -and $port -ne $p) { throw 'Inconsistent endpoint ports' }
        $null = IpNumber $ip
        $port = $p
        $addresses.Add($ip)
    }
    if (!$port -or !$addresses.Count) { return $null }
    $owners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    $found = $false
    foreach ($pidValue in $owners) {
        $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        if ($proc -and $proc.Path -and [string]::Equals($proc.Path, $exe, 'OrdinalIgnoreCase')) { $found = $true }
    }
    if (!$found) { return $null }
    return @{ Addresses = @($addresses | Sort-Object -Unique); Port = $port }
}

function EnsureAllow([string]$name, [string]$exe, $route) {
    $existing = @(Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 1) { throw "Duplicate firewall rule: $name" }
    if (!$route) {
        if ($existing.Count) { Disable-NetFirewallRule -Name $name | Out-Null }
        return
    }
    if (!$existing.Count) {
        New-NetFirewallRule -Name $name -DisplayName $name -Direction Inbound -Action Allow `
            -Program $exe -Protocol TCP -LocalPort $route.Port -LocalAddress $route.Addresses `
            -RemoteAddress $route.Addresses -Profile Any | Out-Null
    } else {
        AssertRuleScope $existing[0] $exe 'Allow'
        $currentAddr = $existing[0] | Get-NetFirewallAddressFilter
        $currentPort = $existing[0] | Get-NetFirewallPortFilter
        $changed = (!(SameValues $currentAddr.LocalAddress $route.Addresses) -or
            !(SameValues $currentAddr.RemoteAddress $route.Addresses) -or
            [string]$currentPort.LocalPort -ne [string]$route.Port)
        if ($changed) {
            Disable-NetFirewallRule -Name $name | Out-Null
            $currentAddr | Set-NetFirewallAddressFilter -LocalAddress $route.Addresses -RemoteAddress $route.Addresses
            $currentPort | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $route.Port
        }
        if ($changed -or $existing[0].Enabled -ne 'True') { Enable-NetFirewallRule -Name $name | Out-Null }
    }
}

function UpdateFirewall {
    $block = @(Get-NetFirewallRule -Name $blockName -ErrorAction SilentlyContinue)
    if (!$block.Count) {
        New-NetFirewallRule -Name $blockName -DisplayName $blockName -Direction Inbound -Action Block `
            -Program $adminExe -Protocol TCP -LocalPort $AdminPort -LocalAddress Any `
            -RemoteAddress Any -Profile Any | Out-Null
        $block = @(Get-NetFirewallRule -Name $blockName -ErrorAction Stop)
    }
    if ($block.Count -ne 1 -or $block[0].Action -ne 'Block' -or $block[0].Direction -ne 'Inbound' -or
        $block[0].Enabled -ne 'True' -or $block[0].Profile -ne 'Any') { throw 'Admin block rule is not verified' }
    AssertRuleScope $block[0] $adminExe 'Block'
    $ports = $block[0] | Get-NetFirewallPortFilter
    $addr = $block[0] | Get-NetFirewallAddressFilter
    if ([string]$ports.LocalPort -ne [string]$AdminPort -or
        $addr.LocalAddress -ne 'Any') { throw 'Admin block filters are not verified' }
    try { $admin = ReadRoutes 'admin-routes.txt' $adminExe } catch { $admin = $null }
    try { $asr = ReadRoutes 'routes.txt' $serverExe } catch { $asr = $null }
    if ($admin -and $admin.Port -ne $AdminPort) { $admin = $null }
    $remote = if ($admin) { @(BlockComplement $admin.Addresses) } else { @('Any') }
    if (!(SameValues $addr.RemoteAddress $remote)) { $addr | Set-NetFirewallAddressFilter -RemoteAddress $remote }
    EnsureAllow $adminRule $adminExe $admin
    EnsureAllow $asrRule $serverExe $asr
}

if ($FunctionsOnly) { return }

do {
    try { UpdateFirewall } catch {
        if ($Once) { throw }
        Write-Error $_ -ErrorAction Continue
    }
    if (!$Once) { Start-Sleep -Seconds 5 }
} while (!$Once)
