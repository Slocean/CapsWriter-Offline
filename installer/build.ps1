param(
    [string]$Version = '',
    [string]$ISCC = '',
    [string]$OutputDirectory = '',
    [string]$LanAddr = '192.168.0.104',
    [switch]$SkipPyInstaller,
    [switch]$SkipGui,
    [switch]$SkipAcceptanceStamp
)
# 一条命令完成客户端双形态构建：
#   1. 桌面 GUI（C# / WPF，版本注入）
#   2. PyInstaller 客户端 COLLECT（干净源码，无 junction / 无本机 dist 依赖）
#   3. installer/stage_payload.py 组装 payload（禁服务端、禁密钥、整树校验）
#   4. Inno Setup 安装器 + 便携版单文件 EXE
# GitHub Actions 与本地共用本脚本；版本唯一来源是 installer/version.txt。
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Version) { $Version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'version.txt') -Raw).Trim() }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "版本号必须是 X.Y.Z：$Version" }
$versionFile = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'version.txt') -Raw).Trim()
if ($Version -ne $versionFile) { throw "版本 $Version 与 installer/version.txt 的 $versionFile 不一致（版本唯一来源）" }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repo 'dist' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$python = 'python'
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { throw '当前 python 缺少 PyInstaller；请先 pip install -r requirements-client.txt' }

# 1. 桌面 GUI
if (-not $SkipGui) {
    & (Join-Path $repo 'desktop\build.ps1') -Version $Version
    if ($LASTEXITCODE -ne 0) { throw 'GUI 构建失败' }
}
$guiExe = Join-Path $repo 'CapsWriterDesktop.exe'
if (-not (Test-Path -LiteralPath $guiExe)) { throw "缺少 GUI 可执行文件: $guiExe" }

# 2. PyInstaller 客户端
if (-not $SkipPyInstaller) {
    Push-Location $repo
    try { & $python -m PyInstaller 'build-client.spec' --noconfirm --distpath dist --workpath build }
    finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
}
$collect = Join-Path $repo 'dist\CapsWriter-Client-Raw'
if (-not (Test-Path -LiteralPath (Join-Path $collect 'start_client.exe'))) { throw "COLLECT 产物缺失: $collect" }

# 3. payload 组装（含服务端/密钥/README 排除整树校验）
$payload = Join-Path $repo ('dist\client-payload-' + $Version)
& $python (Join-Path $PSScriptRoot 'stage_payload.py') `
    --repo $repo --collect $collect --gui-exe $guiExe `
    --out $payload --lan-addr $LanAddr --force
if ($LASTEXITCODE -ne 0) { throw 'payload 组装失败' }

# 4a. Inno Setup 安装器
if (-not $ISCC) {
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        (Join-Path $repo 'work\installer-tools\inno6\ISCC.exe'))) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $ISCC = $candidate; break }
    }
}
if (-not $ISCC -or -not (Test-Path -LiteralPath $ISCC)) { throw "找不到 ISCC.exe；请用 -ISCC 指定 Inno Setup 编译器路径" }
& $ISCC "/DPayloadDir=$payload" "/DAppVersion=$Version" "/DInstallerOutputDir=$OutputDirectory" (Join-Path $PSScriptRoot 'capswriter-client.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败: $LASTEXITCODE" }

# 4b. 便携版单文件 EXE
$setup = Join-Path $OutputDirectory ("CapsWriter-Setup-$Version.exe")
$portable = Join-Path $OutputDirectory ("CapsWriter-Portable-$Version.exe")
& $python (Join-Path $PSScriptRoot 'build_portable.py') `
    --repo $repo --payload $payload --out $portable --version $Version `
    --record (Join-Path $OutputDirectory 'portable-build-record.json')
if ($LASTEXITCODE -ne 0) { throw '便携版构建失败' }

$summary = [ordered]@{
    version      = $Version
    setup        = $setup
    portable     = $portable
    setup_sha256 = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
    portable_sha256 = (Get-FileHash -LiteralPath $portable -Algorithm SHA256).Hash.ToLowerInvariant()
    payload      = $payload
}
$summary | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'build-summary.json') -Encoding utf8
$summary | ConvertTo-Json
