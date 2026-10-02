param([string]$Version = '')
# 编译桌面 GUI（CapsWriterDesktop.exe）。版本唯一来源是 installer/version.txt，
# 写入 desktop/AppVersion.generated.cs 后一并编译（该生成文件不入库）。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Version) { $Version = (Get-Content -LiteralPath (Join-Path $root 'installer\version.txt') -Raw).Trim() }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "版本号必须是 X.Y.Z：$Version" }
$versionFile = (Get-Content -LiteralPath (Join-Path $root 'installer\version.txt') -Raw).Trim()
if ($Version -ne $versionFile) { throw "版本 $Version 与 installer/version.txt 的 $versionFile 不一致" }
$generated = Join-Path $PSScriptRoot 'AppVersion.generated.cs'
$source = "internal static partial class Desktop {`r`n    // 由 desktop/build.ps1 生成，版本唯一来源 installer/version.txt`r`n    internal const string AppVersion = `"$Version`";`r`n}`r`n"
[System.IO.File]::WriteAllText($generated, $source, (New-Object System.Text.UTF8Encoding($true)))
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$fx = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$out = '/out:' + (Join-Path $root 'CapsWriterDesktop.exe')
$icon = '/win32icon:' + (Join-Path $root 'assets\icon.ico')
$refs = @(
  'WPF\PresentationFramework.dll',
  'WPF\PresentationCore.dll',
  'WPF\WindowsBase.dll',
  'System.Xaml.dll'
) | ForEach-Object { '/reference:' + (Join-Path $fx $_) }
& $csc /nologo /target:winexe /optimize+ $out $icon $refs `
  '/reference:System.Drawing.dll' '/reference:System.Security.dll' `
  '/reference:System.Windows.Forms.dll' '/reference:System.Web.Extensions.dll' `
  (Join-Path $PSScriptRoot 'CapsWriterDesktop.cs') (Join-Path $PSScriptRoot 'Visuals.cs') `
  (Join-Path $PSScriptRoot 'Shortcuts.cs') (Join-Path $PSScriptRoot 'Update.cs') `
  $generated
if ($LASTEXITCODE -ne 0) { throw 'C# compilation failed.' }
Write-Host "CapsWriterDesktop.exe 构建完成（版本 $Version）"
