$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
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
& $csc /nologo /target:winexe /optimize+ $out $icon $refs /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $PSScriptRoot 'CapsWriterDesktop.cs') (Join-Path $PSScriptRoot 'Visuals.cs')
if ($LASTEXITCODE -ne 0) { throw 'C# compilation failed.' }
