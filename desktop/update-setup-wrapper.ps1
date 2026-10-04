# CapsWriter installed-flavor update transaction (ASCII only: PS5.1 reads
# BOM-less scripts in the system ANSI codepage).
# Called by the GUI (desktop/Update.cs) and by the update transaction tests:
#   powershell -File update-setup-wrapper.ps1 -SetupPath <new setup exe> `
#     [-GuiPid <gui pid>] [-AppDir <install dir>] [-ResultPath <result txt>]
#     [-WaitTimeoutSeconds 60] [-ProbePath <diag stand-in exe>] [-TxId <id>]
#
# Legacy argument repair: GUI versions <=2.6.3 build the -AppDir argument
# from AppDomain.CurrentDomain.BaseDirectory, which ALWAYS ends with a
# backslash. Inside a quoted command-line token that trailing backslash
# escapes the closing quote, so CRT parsing folds "-ResultPath <value>"
# into ONE -AppDir token and -ResultPath never binds. With the previously
# mandatory parameters that killed the script before line one (the
# 2.6.2/2.6.3 "UI closed, then silence" hot-update failure). All parameters
# are now optional with $PSScriptRoot defaults, the script reads $args raw
# (so space-containing install dirs cannot smuggle stray fragments into
# other parameters), and Repair-LegacyArguments reassembles the full
# corrupted value and recovers the real AppDir/ResultPath from the INTACT
# string, so updates from already-installed legacy versions succeed without
# touching legacy GUI code.
#
# Protocol: the result file mixes timestamped human lines with raw protocol
# lines that carry NO timestamp prefix so the GUI can match them exactly:
#   TX <txid>                 first line, transaction identity
#   READY [<txid>]            backup done, GUI may exit (GUI honors only
#                             the line carrying its own transaction id)
#   RESULT: OK | RESULT: FAILED: <reason>   always the LAST line
# Every failure path lands in Fail() - ONE recovery boundary:
#   - before the installer ran: old install untouched; partial backup from
#     an aborted/failed backup stage is removed; if the GUI is already gone
#     (legacy GUIs exit unconditionally) the old version is restarted, so a
#     precheck failure never leaves the user with a closed app;
#   - after the installer ran: Restore-OldVersion from the backup, then
#     restart; restore robocopy failure (>=8) or restart failure keeps the
#     backup and exits nonzero - success is never claimed while recovery is
#     incomplete;
#   - any unexpected exception goes through the same boundary (exit 92).
#   - RESULT is written by Finish() AFTER recovery, and both robocopy
#     operations exclude update-setup-result.txt (/XF), so the result file
#     always ends up complete and visible to the restarted GUI.
#   - the whole program directory is backed up first (robocopy; Inno does
#     NOT back up replaced files by itself - official FAQ "Making Backups
#     Before Replacing Files");
#   - the installer runs with an explicit /DIR so non-default install
#     directories are always honored.
# -ProbePath replaces the restart target for UI-less transaction tests;
# production never passes it.
# Raw argument access instead of a param() block, ON PURPOSE: legacy GUIs
# (<=2.6.3) send a corrupted -AppDir token whose stray fragments PowerShell
# would otherwise bind into other parameters (e.g. an [int] conversion
# failure before line one - the original "UI closed, then silence" bug for
# install dirs containing spaces). Reading $args directly lets the repair
# below reassemble the full corrupted value from ALL its fragments.
$KnownParams = @('SetupPath', 'GuiPid', 'AppDir', 'ResultPath', 'WaitTimeoutSeconds', 'ProbePath', 'TxId')
$raw = @{}
$i = 0
while ($i -lt $args.Count) {
    $tok = [string]$args[$i]
    if ($tok -match '^-[A-Za-z]+$' -and $KnownParams -contains $tok.Substring(1) -and ($i + 1) -lt $args.Count) {
        $name = $tok.Substring(1)
        $value = [string]$args[$i + 1]
        $i += 2
        # Corrupted -AppDir: fragments of a space-containing ResultPath value
        # follow as separate non-option tokens - rejoin them with single
        # spaces (the GUI emits exactly single spaces).
        if ($name -eq 'AppDir') {
            while ($i -lt $args.Count -and ([string]$args[$i] -notmatch '^-')) {
                $value = $value + ' ' + [string]$args[$i]
                $i += 1
            }
        }
        $raw[$name] = $value
    } else {
        $i += 1
    }
}
[string]$SetupPath = ''
[int]$GuiPid = 0
[string]$AppDir = ''
[string]$ResultPath = ''
[int]$WaitTimeoutSeconds = 60
[string]$ProbePath = ''
[string]$TxId = ''
if ($raw.ContainsKey('SetupPath')) { $SetupPath = $raw['SetupPath'] }
if ($raw.ContainsKey('GuiPid')) { [void][int]::TryParse($raw['GuiPid'], [ref]$GuiPid) }
if ($raw.ContainsKey('AppDir')) { $AppDir = $raw['AppDir'] }
if ($raw.ContainsKey('ResultPath')) { $ResultPath = $raw['ResultPath'] }
if ($raw.ContainsKey('WaitTimeoutSeconds')) { [void][int]::TryParse($raw['WaitTimeoutSeconds'], [ref]$WaitTimeoutSeconds) }
if ($raw.ContainsKey('ProbePath')) { $ProbePath = $raw['ProbePath'] }
if ($raw.ContainsKey('TxId')) { $TxId = $raw['TxId'] }
$ErrorActionPreference = 'Stop'
$log = New-Object System.Collections.Generic.List[string]

function Save {
    Set-Content -LiteralPath $ResultPath -Value ($log -join "`r`n") -Encoding utf8
}
# Raw protocol line (no timestamp): TX / READY / RESULT.
function Mark([string]$m) {
    $script:log.Add($m)
    Save
}
function Log([string]$m) {
    $script:log.Add((Get-Date -Format s) + ' ' + $m)
    Save
}
function Finish([string]$result) {
    Mark ("RESULT: " + $result)
}
function Get-AliveProcess([int]$ProcessId) {
    if ($ProcessId -le 0) { return $null }
    return (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

# Legacy GUI (<=2.6.3) quoting bug: the trailing backslash of BaseDirectory
# escapes the closing quote of -AppDir; CRT parsing folds everything up to
# the NEXT quote into one -AppDir value shaped like
#   <appdir>" -ResultPath <resultpath>
# Keep the corrupted string intact, locate both markers in it, split once,
# then trim any surrounding quotes off the recovered values.
function Repair-LegacyArguments {
    $corrupted = $AppDir
    $marker = ' -ResultPath '
    if (-not $corrupted -or -not $corrupted.Contains($marker)) { return }
    $quote = $corrupted.IndexOf('"')
    $idx = $corrupted.IndexOf($marker)
    if ($quote -le 0 -or $idx -le $quote) { return }
    $script:AppDir = $corrupted.Substring(0, $quote)
    $rest = $corrupted.Substring($idx + $marker.Length).Trim()
    $rest = $rest.Trim('"')
    if ($rest) { $script:ResultPath = $rest }
    $script:legacyRepaired = $true
}
$script:legacyRepaired = $false
Repair-LegacyArguments
if (-not $AppDir) { $AppDir = $PSScriptRoot }
if (-not $ResultPath) { $ResultPath = Join-Path $AppDir 'update-setup-result.txt' }
$AppDir = $AppDir.TrimEnd('\')
if ($AppDir -match '^[A-Za-z]:$') { $AppDir = $AppDir + '\' }

try { $null = [System.IO.File]::OpenWrite($ResultPath).Close(); } catch { $ResultPath = Join-Path $env:TEMP 'capswriter-update-result.txt' }

# Transaction identity comes first so the file always names its transaction.
Mark ('TX ' + $TxId)
Log ("wrapper start; gui_pid=" + $GuiPid + " wait_timeout=" + $WaitTimeoutSeconds)
Log ("setup=" + $SetupPath)
Log ("appdir=" + $AppDir)
if ($script:legacyRepaired) { Log 'legacy argument corruption repaired (trailing-backslash quoting bug of GUI <=2.6.3)' }

$script:phase = 'precheck'
$script:installStarted = $false
$restartTarget = $null
$workdir = $null
$backup = $null
$exe = $null

# The GUI shows cancel/failure diagnostics itself. Before restarting, verify
# NO CapsWriterDesktop instance is alive (by pid AND by name): avoids a second
# instance, and covers a recycled GuiPid (identity mismatch) as well.
# Returns a STATUS STRING so callers can only ever claim "restarted" when
# this function actually started a process (independent review counterexample:
# a leftover unrelated instance made a skipped restart look like a restart).
function Restart-AppWhenGone([string]$phase) {
    $byPid = Get-AliveProcess $GuiPid
    if ($null -ne $byPid -and $byPid.ProcessName -like 'CapsWriterDesktop*') {
        Log ($phase + " restart skipped: gui still running (pid " + $GuiPid + ")")
        return 'skipped-gui-running'
    }
    $any = Get-Process -Name 'CapsWriterDesktop' -ErrorAction SilentlyContinue
    if ($null -ne $any) {
        Log ($phase + " restart skipped: a CapsWriterDesktop instance is already running (pid " + (@($any)[0].Id) + ", not started by this transaction)")
        return 'skipped-instance-running'
    }
    if (-not $restartTarget) {
        Log ($phase + " restart skipped: no restart target (app dir missing?)")
        return 'skipped-no-target'
    }
    try {
        Start-Process -FilePath $restartTarget -WorkingDirectory $workdir
        Log ($phase + " restarted")
        return 'restarted'
    } catch {
        Log ($phase + " restart failed: " + $_.Exception.Message + " -> KEEPING backup")
        return 'failed'
    }
}

function Restore-OldVersion([string]$reason) {
    Log ("recovery: " + $reason)
    if (-not (Test-Path -LiteralPath $backup)) {
        Log 'backup missing -> cannot restore from backup; keeping state for manual recovery'
        return $false
    }
    # /XF keeps the result file (and the running wrapper's log) alive across
    # the mirror restore; logs/diary are user data and excluded as well.
    robocopy $backup $AppDir /MIR /NFL /NDL /NJH /NJS /XD logs diary __pycache__ /XF update-setup-result.txt | Out-Null
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

# ONE recovery boundary for EVERY failure path (called via throw+catch and
# directly). Nothing is claimed successful while recovery is incomplete, and
# the RESULT line carries the REAL recovery outcome ("outcome: ...") - only
# "restarted" when a process was actually started by this transaction.
function Fail([int]$code, [string]$reason) {
    $outcome = ''
    try {
        if ($script:installStarted) {
            # installer may have replaced files: restore, then restart old
            if (Restore-OldVersion $reason) {
                $restart = Restart-AppWhenGone 'old version'
                if ($restart -eq 'restarted') { $outcome = 'old version restored and restarted' }
                elseif ($restart -eq 'skipped-gui-running') { $outcome = 'old version restored; gui still running (restart skipped)' }
                elseif ($restart -eq 'skipped-instance-running') { $outcome = 'old version restored; restart skipped (a CapsWriterDesktop instance was already running, not started by this transaction)' }
                else { $outcome = 'recovery incomplete; backup kept for manual recovery' }
            } else {
                $outcome = 'recovery incomplete; backup kept for manual recovery'
            }
        } else {
            # nothing was replaced yet: clean abort
            if (($script:phase -eq 'backup' -or $script:phase -eq 'waiting') -and $backup -and (Test-Path -LiteralPath $backup)) {
                try {
                    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction Stop
                    Log 'backup removed (abort before install, old install untouched)'
                } catch {
                    Log ("backup cleanup deferred (kept): " + $_.Exception.Message)
                }
            }
            # legacy GUIs exit unconditionally after launching the wrapper;
            # if the GUI is already gone, bring the old version back up so a
            # precheck failure never ends with a closed app and no restart.
            $restart = Restart-AppWhenGone 'precheck'
            if ($restart -eq 'restarted') { $outcome = 'old install untouched; app restarted' }
            elseif ($restart -eq 'skipped-gui-running') { $outcome = 'old install untouched; gui still running' }
            elseif ($restart -eq 'skipped-instance-running') { $outcome = 'old install untouched; a CapsWriterDesktop instance was already running' }
            else { $outcome = 'old install untouched; app restart failed' }
        }
    } catch {
        Log ("recovery handler exception: " + $_.Exception.Message + " -> KEEPING backup, success not claimed")
        if ($script:installStarted) { $outcome = 'recovery incomplete; backup kept for manual recovery' }
    }
    if ($outcome) { Finish ("FAILED: " + $reason + "; outcome: " + $outcome) }
    else { Finish ("FAILED: " + $reason) }
    exit $code
}

try {
    if (-not (Test-Path -LiteralPath $AppDir)) {
        Fail 94 'app dir missing -> abort, nothing touched'
    }
    if (-not (Split-Path -Parent $AppDir)) {
        Fail 94 'app dir is a drive root; refusing transaction (backup/restore would mirror the whole drive)'
    }
    $leaf = Split-Path -Leaf $AppDir
    if (-not $leaf) { $leaf = 'CapsWriter' }
    $backup = Join-Path (Split-Path -Parent $AppDir) ($leaf + '.update-backup')
    $exe = Join-Path $AppDir 'CapsWriterDesktop.exe'
    $workdir = $AppDir
    if ($ProbePath -ne '') { $restartTarget = $ProbePath; $workdir = Split-Path -Parent $ProbePath }
    else { $restartTarget = $exe }

    # ---- phase 1: precheck + backup (GUI still open; UI exits only after READY) ----
    if (-not $SetupPath) {
        Fail 98 'setup path missing (download/verify stage) -> abort, old install untouched'
    }
    if (-not (Test-Path -LiteralPath $SetupPath)) {
        Fail 98 'downloaded setup missing -> abort, old install untouched'
    }

    $guiProc = Get-AliveProcess $GuiPid
    if ($null -ne $guiProc -and $guiProc.ProcessName -notlike 'CapsWriterDesktop*') {
        Fail 93 ("gui pid " + $GuiPid + " is '" + $guiProc.ProcessName + "' (identity mismatch, recycled pid?)")
    }

    $script:phase = 'backup'
    try {
        if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
        Log ("backup -> " + $backup)
        robocopy $AppDir $backup /E /NFL /NDL /NJH /NJS /XD logs diary __pycache__ /XF update-setup-result.txt | Out-Null
        if ($LASTEXITCODE -ge 8) {
            Fail 97 ("backup robocopy failed with code " + $LASTEXITCODE + " -> abort, old install untouched")
        }
        Log 'backup done'
    } catch {
        Fail 97 ("backup stage exception: " + $_.Exception.Message + " -> abort, old install untouched")
    }

    # READY is the GUI's exit gate: the UI closes only after seeing this line
    # (and only when it carries the transaction id the GUI issued).
    Mark ('READY' + $(if ($TxId) { ' ' + $TxId }))
    Log 'READY written; waiting for gui exit'

    # ---- phase 2: wait for GUI exit ----
    $script:phase = 'waiting'
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
                Fail 99 'gui did not exit (update cancelled by GUI); old install untouched'
            }
        }
    }

    # ---- phase 3: install (explicit /DIR, non-default directories honored) ----
    $script:phase = 'installing'
    $installCode = 91
    try {
        # environment passed to the install process: the production installer
        # ignores it; the transaction test's pseudo-installer uses it to find
        # the app dir and produce real "partial write, then fail" cases
        $env:CAPSWRITER_UPDATE_APPDIR = $AppDir
        $installArgs = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="' + $AppDir + '"'
        Log ("installer args=" + $installArgs)
        $script:installStarted = $true
        $installer = Start-Process -FilePath $SetupPath -ArgumentList $installArgs -Wait -PassThru
        $installCode = $installer.ExitCode
        Log ("installer exit code " + $installCode)
    } catch {
        Log ("installer could not be started: " + $_.Exception.Message)
    }

    if ($installCode -ne 0 -or -not (Test-Path -LiteralPath $exe)) {
        Fail $(if ($installCode -ne 0) { $installCode } else { 96 }) ("install failed (code " + $installCode + ") or restart target missing")
    }

    # ---- phase 4: start the new version from the install dir, confirm alive ----
    $script:phase = 'starting'
    try {
        $proc = Start-Process -FilePath $restartTarget -WorkingDirectory $workdir -PassThru -ErrorAction Stop
        Start-Sleep -Seconds 3
        if ($null -eq $proc -or $proc.HasExited) {
            throw (New-Object System.InvalidOperationException 'new app exited immediately after start')
        }
    } catch {
        Fail 95 ("new start failed: " + $_.Exception.Message)
    }

    Log 'app restarted from install directory and running'
    try { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue; Log 'backup cleaned' } catch { Log 'backup cleanup deferred (kept)' }
    Finish 'OK'
    exit 0
} catch {
    # unexpected exception anywhere above: same recovery boundary, honest exit
    Fail 92 $_.Exception.Message
}
