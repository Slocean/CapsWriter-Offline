# CapsWriter installed-flavor update transaction (ASCII only: PS5.1 reads
# BOM-less scripts in the system ANSI codepage).
# Called by the GUI (desktop/Update.cs) and by the update transaction tests:
#   powershell -File update-setup-wrapper.ps1 -SetupPath <new setup exe> `
#     -GuiPid <gui pid> -AppDir <install dir> -ResultPath <result txt>
#     [-WaitTimeoutSeconds 60] [-ProbePath <diag stand-in exe>]
# Unified transaction with ONE recovery boundary: backup -> install ->
# verify -> start. EVERY failure path (installer exit code, missing target,
# Start-Process throwing, new app dying immediately, any unexpected
# exception) goes through Restore-OldVersion + Restart-App:
#   - the whole program directory is backed up first (robocopy; Inno does
#     NOT back up replaced files by itself - official FAQ "Making Backups
#     Before Replacing Files");
#   - restore robocopy failure (>=8) or restart failure keeps the backup and
#     exits nonzero - success is never claimed while recovery is incomplete;
#   - the backup is deleted only after a fully successful update.
# -ProbePath replaces the restart target for UI-less transaction tests;
# production never passes it.
param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [Parameter(Mandatory = $true)][int]$GuiPid,
    [Parameter(Mandatory = $true)][string]$AppDir,
    [Parameter(Mandatory = $true)][string]$ResultPath,
    [int]$WaitTimeoutSeconds = 60,
    [string]$ProbePath = ''
)
$ErrorActionPreference = 'Stop'
$log = New-Object System.Collections.Generic.List[string]
function Save {
    Set-Content -LiteralPath $ResultPath -Value ($log -join "`r`n") -Encoding utf8
}
function Log([string]$m) {
    $script:log.Add((Get-Date -Format s) + ' ' + $m)
    Save
}
function Get-AliveProcess([int]$ProcessId) {
    return (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}
try { $null = [System.IO.File]::OpenWrite($ResultPath).Close(); } catch { $ResultPath = Join-Path $env:TEMP 'capswriter-update-result.txt' }

Log ("wrapper start; gui_pid=" + $GuiPid + " wait_timeout=" + $WaitTimeoutSeconds)
Log ("setup=" + $SetupPath)
Log ("appdir=" + $AppDir)

if (-not (Get-AliveProcess $GuiPid)) {
    Log 'gui already exited before wrapper start -> proceed'
} else {
    try {
        Wait-Process -Id $GuiPid -Timeout $WaitTimeoutSeconds -ErrorAction Stop
        Log 'gui exited during wait'
    } catch {
        if (-not (Get-AliveProcess $GuiPid)) {
            Log 'gui exited (wait raised but process is gone) -> proceed'
        } else {
            Log 'gui still running past wait timeout -> abort update, old install untouched'
            exit 99
        }
    }
}

if (-not (Test-Path -LiteralPath $SetupPath)) {
    Log 'downloaded setup missing -> abort'
    exit 98
}

$leaf = Split-Path -Leaf $AppDir
$backup = Join-Path (Split-Path -Parent $AppDir) ($leaf + '.update-backup')
$exe = Join-Path $AppDir 'CapsWriterDesktop.exe'
$workdir = $AppDir
if ($ProbePath -ne '') { $restartTarget = $ProbePath; $workdir = Split-Path -Parent $ProbePath }
else { $restartTarget = $exe }

function Restore-OldVersion([string]$reason) {
    Log ("recovery: " + $reason)
    if (-not (Test-Path -LiteralPath $backup)) {
        Log 'backup missing -> cannot restore from backup; keeping state for manual recovery'
        return $false
    }
    robocopy $backup $AppDir /MIR /NFL /NDL /NJH /NJS /XD logs diary __pycache__ | Out-Null
    Log ("restore robocopy code " + $LASTEXITCODE)
    if ($LASTEXITCODE -ge 8) {
        Log 'restore robocopy failed (>=8) -> KEEPING backup, success not claimed'
        return $false
    }
    if (-not (Test-Path -LiteralPath $exe)) {
        Log 'old exe still missing after restore -> KEEPING backup, success not claimed'
        return $false
    }
    return $true
}

function Restart-App([string]$phase) {
    try {
        Start-Process -FilePath $restartTarget -WorkingDirectory $workdir
        Log ($phase + " restarted")
        return $true
    } catch {
        Log ($phase + " restart failed: " + $_.Exception.Message + " -> KEEPING backup")
        return $false
    }
}

try {
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
    Log ("backup -> " + $backup)
    robocopy $AppDir $backup /E /NFL /NDL /NJH /NJS /XD logs diary __pycache__ | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Log ("backup robocopy failed with code " + $LASTEXITCODE + " -> abort, old install untouched")
        exit 97
    }
    Log 'backup done'

    $installCode = 91
    try {
        # 传递给安装进程的环境变量：生产安装器忽略；事务小样的伪安装器据此
        # 定位应用目录，制造真实"部分写入后失败"用例
        $env:CAPSWRITER_UPDATE_APPDIR = $AppDir
        $installer = Start-Process -FilePath $SetupPath -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
        $installCode = $installer.ExitCode
        Log ("installer exit code " + $installCode)
    } catch {
        Log ("installer could not be started: " + $_.Exception.Message)
    }

    if ($installCode -ne 0 -or -not (Test-Path -LiteralPath $exe)) {
        if (Restore-OldVersion ("install failed (code " + $installCode + ") or restart target missing")) {
            Restart-App 'old version' | Out-Null
        }
        if ($installCode -ne 0) { exit $installCode }
        exit 96
    }

    try {
        $proc = Start-Process -FilePath $restartTarget -WorkingDirectory $workdir -PassThru -ErrorAction Stop
        Start-Sleep -Seconds 3
        if ($null -eq $proc -or $proc.HasExited) {
            throw (New-Object System.InvalidOperationException 'new app exited immediately after start')
        }
    } catch {
        if (Restore-OldVersion ("new start failed: " + $_.Exception.Message)) {
            Restart-App 'old version' | Out-Null
        }
        exit 95
    }

    Log 'app restarted from install directory and running'
    try { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue; Log 'backup cleaned' } catch { Log 'backup cleanup deferred (kept)' }
    exit 0
} catch {
    Log ("unexpected wrapper exception: " + $_.Exception.Message + " -> unified recovery")
    if (Restore-OldVersion 'unexpected exception') {
        Restart-App 'old version' | Out-Null
    }
    exit 92
}
