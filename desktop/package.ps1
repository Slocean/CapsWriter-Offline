param(
    [Parameter(Mandatory = $true)][string]$ClientDirectory,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$source = (Resolve-Path -LiteralPath $ClientDirectory).Path
if (-not (Test-Path -LiteralPath (Join-Path $source 'start_client.exe'))) {
    throw 'ClientDirectory must contain start_client.exe.'
}
if (-not (Test-Path -LiteralPath (Join-Path $source 'core\client\output'))) {
    throw 'ClientDirectory must contain the external core source tree.'
}
$dest = [System.IO.Path]::GetFullPath($OutputDirectory)
if ($dest.TrimEnd('\') -eq $source.TrimEnd('\')) {
    throw 'OutputDirectory must differ from ClientDirectory.'
}
New-Item -ItemType Directory -Path $dest -Force | Out-Null
& robocopy $source $dest /E /XD logs /XF CapsWriter-Float.exe CapsWriter-Float.cs config_client.before-float.py | Out-Null
if ($LASTEXITCODE -gt 7) { throw "robocopy failed with code $LASTEXITCODE" }
& (Join-Path $PSScriptRoot 'build.ps1')
Copy-Item -LiteralPath (Join-Path $repo 'CapsWriterDesktop.exe') -Destination $dest -Force
New-Item -ItemType Directory -Path (Join-Path $dest 'assets') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'assets\icon.ico') -Destination (Join-Path $dest 'assets\icon.ico') -Force
Copy-Item -LiteralPath (Join-Path $repo 'assets\icon.png') -Destination (Join-Path $dest 'assets\icon.png') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\output\live_output.py') -Destination (Join-Path $dest 'core\client\output\live_output.py') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\output\result_processor.py') -Destination (Join-Path $dest 'core\client\output\result_processor.py') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\audio\recorder.py') -Destination (Join-Path $dest 'core\client\audio\recorder.py') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\audio\pause_segmenter.py') -Destination (Join-Path $dest 'core\client\audio\pause_segmenter.py') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\shortcut\shortcut_manager.py') -Destination (Join-Path $dest 'core\client\shortcut\shortcut_manager.py') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\shortcut\event_handler.py') -Destination (Join-Path $dest 'core\client\shortcut\event_handler.py') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\shortcut\key_mapper.py') -Destination (Join-Path $dest 'core\client\shortcut\key_mapper.py') -Force
Copy-Item -LiteralPath (Join-Path $repo 'core\client\udp\udp_control.py') -Destination (Join-Path $dest 'core\client\udp\udp_control.py') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $dest 'Desktop-README.md') -Force
$cfg = Join-Path $dest 'config_client.py'
$content = [System.IO.File]::ReadAllText($cfg, [System.Text.Encoding]::UTF8)
if ($content -notmatch '(?m)^\s*live_output\s*=') {
    $content = $content.Replace('class ClientConfig:', "class ClientConfig:`r`n    live_output = False")
    [System.IO.File]::WriteAllText($cfg, $content, (New-Object System.Text.UTF8Encoding($false)))
}
Write-Host "Desktop package ready: $dest"
