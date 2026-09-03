<#
.SYNOPSIS
    Run every collector, then build the console from what they wrote.

.DESCRIPTION
    The console is a renderer - it never talks to Graph or to a printer. This
    wrapper is the other half: it runs the collectors in order, then calls
    build.py. Point it at the folder holding the tool repos and the folder they
    should write to, and it handles the rest.

    One sign-in for all three Entra tools. The wrapper connects once with the
    union of their read-only scopes; each script sees an existing Graph context
    and reuses it instead of prompting again.

    A collector that fails does not stop the run. The wrapper records the
    failure, carries on, and still builds the console - which will show that
    feed as stale rather than quietly serving last week's numbers. The exit
    code is non-zero if anything failed, so a scheduled task shows red.

    After the build, alerts: build.py has already worked out what is firing
    (output\alerts.json, from the rules in alerts.ini beside this script);
    notify.py then sends a Teams and/or email message ONLY when something is
    new, worse or cleared since the last one (or a weekly summary). With no
    channel in alerts.ini the step is skipped, in words, and nothing else changes.

.PARAMETER ToolRoot
    Folder containing the tool repos (entra-tenant-docs, entra-security-snapshot,
    m365-license-waste-report, print-fleet-dashboard). Defaults to this repo's
    parent folder, which is right if you cloned them all side by side.

.PARAMETER OutputRoot
    Where the collectors write. Must match the paths in sources.ini.

.PARAMETER SitePath
    Where the built console goes. Point a web server or a file share at it.

.PARAMETER ConfigPath
    The console's sources.ini. Defaults to sources.ini beside this script.

.PARAMETER StaleDays
    "Not signed in for this many days" threshold, passed to the security
    snapshot and the license report.

.PARAMETER Python
    Python 3 executable. 'python' on most Windows installs, 'python3' elsewhere.

.PARAMETER FleetConfig
    print-fleet-dashboard config.ini. Usually unnecessary: when the config.ini
    beside the collector differs from the shipped example, the wrapper polls
    the printers automatically - editing that file IS turning the feature on.

.PARAMETER FleetDb
    The fleet database the collector appends to. Defaults to fleet.db in
    -OutputRoot, which is where setup's sources.ini already points.

.PARAMETER LicensePrices
    An INI of per-seat monthly prices, so the license report shows the waste in
    dollars. Usually unnecessary: a prices.ini beside the license tool is used
    automatically, and the first run writes a starter listing your own SKUs to
    fill in. Pass this to point at a prices file kept somewhere else.

.PARAMETER NoStatusPage
    Do not OPEN a browser window for the live progress page. The page and the
    progress it reads are written either way - the console's own "Refresh now"
    button already has a window, and a scheduled run that goes wrong should
    still have left a record of where it got to.

.PARAMETER NoConnect
    Skip the up-front Connect-MgGraph - use when you are already connected, or
    when running app-only with a certificate you connect with yourself.

.PARAMETER Scheduled
    This run was started by the automatic-refresh task, not by a person. The
    sign-in then happens from a short-lived child process with a time limit, so
    a sign-in window nobody is there to finish cannot hang the run: when the
    limit passes the run carries on without Microsoft 365 data, records why,
    and the console shows one plain sentence about it.

.PARAMETER RefreshConfig
    The automatic-refresh.ini setup writes (schedule mode, whether to stay
    signed in between runs, and the registered app + certificate for
    unattended runs). Defaults to the file beside this script; a missing file
    means "no automatic refresh", which is how a desktop click behaves.

.PARAMETER SignInTimeoutSeconds
    How long a scheduled run waits for a sign-in window to be finished before
    giving up on Microsoft 365 for this run. Default 300 (5 minutes).

.PARAMETER AlertsConfig
    The alerts.ini to use. Defaults to the file beside this script.

.PARAMETER SkipAlerts
    Build everything but send no alert message this run.

.EXAMPLE
    .\run-all.ps1 -OutputRoot C:\it-ops\output -SitePath C:\inetpub\console

.EXAMPLE
    .\run-all.ps1 -SkipFleet -Verbose
    Entra tools only, with per-step detail.

.NOTES
    Everything here is read-only against your tenant. Nothing is changed,
    created, or deleted in Entra, Intune, or on the printers.
#>
[CmdletBinding()]
param(
    [string]$ToolRoot,
    [string]$OutputRoot = 'C:\it-ops\output',
    [string]$SitePath   = 'C:\it-ops\console-site',
    [string]$ConfigPath,
    [ValidateRange(1, 3650)][int]$StaleDays = 90,
    [string]$Python = 'python',
    [string]$FleetConfig,
    [string]$FleetDb,
    [string]$LicensePrices,
    [switch]$SkipTenantDocs,
    [switch]$SkipSecurity,
    [switch]$SkipLicensing,
    [switch]$SkipFleet,
    [switch]$NoConnect,
    [switch]$NoStatusPage,
    [switch]$Scheduled,
    # How long the security snapshot may spend talking to Microsoft 365 before
    # it reports what it has. Its sign-in-log sweep is the slowest and most
    # throttled thing this whole refresh does, and an unbounded one is
    # indistinguishable from a hang. 0 removes the limit.
    [ValidateRange(0, 240)][int]$SecurityBudgetMinutes = 20,
    # A collector that runs past this is STOPPED, and the refresh carries on
    # without it. Nothing else can guarantee that: a step blocks on a sign-in
    # window nobody can see, and a process cannot interrupt itself. 0 removes
    # the limit, which is the old behaviour and can hang for ever.
    [ValidateRange(0, 240)][int]$StepTimeoutMinutes = 30,
    [string]$RefreshConfig,
    [ValidateRange(30, 3600)][int]$SignInTimeoutSeconds = 300,
    [string]$AlertsConfig,
    [switch]$SkipAlerts
)

$ErrorActionPreference = 'Stop'
# Deliberately NO Set-StrictMode here. Strict mode inherits into every script
# this wrapper invokes with '&', and it changes their semantics: the collectors
# read optional Graph response properties (a missing '@odata.nextLink' is how
# paging ENDS), which strict mode turns from "null" into a terminating error.
# A wrapper must not alter the behaviour of the things it wraps.

if (-not $ToolRoot)      { $ToolRoot      = Split-Path -Parent $PSScriptRoot }
if (-not $ConfigPath)    { $ConfigPath    = Join-Path $PSScriptRoot 'sources.ini' }
if (-not $RefreshConfig) { $RefreshConfig = Join-Path $PSScriptRoot 'automatic-refresh.ini' }
if (-not $AlertsConfig)  { $AlertsConfig  = Join-Path $PSScriptRoot 'alerts.ini' }

$results = [System.Collections.Generic.List[object]]::new()

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail = '', [double]$Seconds = 0)
    $results.Add([pscustomobject]@{
        Step = $Name; Status = $Status; Seconds = [math]::Round($Seconds, 1); Detail = $Detail
    })
}

function Get-PlainWords {
    # Turn the most common failure texts into a sentence a non-technical
    # person can act on. The raw detail stays in the table above - this is a
    # translation, not a replacement.
    param([string]$Step, [string]$Detail)
    if ($Step -eq 'console build') {
        return 'The console pages could not be rebuilt, so the site still shows the previous data. If this keeps happening, check that Python 3 is installed.'
    }
    # The sign-in step composes its own plain sentence as it goes (which route
    # it tried, why each was passed over) - pass that through untouched.
    if ($Step -eq 'sign-in') { return $Detail }
    if ($Step -eq 'alerts') {
        return 'The alert message could not be sent - check the Teams Workflows URL or the mail relay in alerts.ini. The console itself was rebuilt.'
    }
    switch -Wildcard ($Detail) {
        '*AADSTS*'                       { return 'The sign-in did not complete. Run this again and finish the sign-in window.' }
        '*Authentication needed*'        { return 'You were not signed in. Run this again and finish the sign-in window.' }
        '*InteractiveBrowserCredential*' { return 'The sign-in window was closed before finishing. Run this again.' }
        '*User canceled*'                { return 'The sign-in was cancelled. Run this again when ready.' }
        '*Insufficient privileges*'      { return 'Your account was not allowed to read this data. An administrator needs to approve the read-only permissions once.' }
        '*Authorization_RequestDenied*'  { return 'Your account was not allowed to read this data. An administrator needs to approve the read-only permissions once.' }
        '*not found: *'                  { return 'A tool folder is missing. Re-run setup and it will download it again.' }
        '*TooManyRequests*'              { return 'Microsoft asked us to slow down. Wait a few minutes and run this again.' }
        '*429*'                          { return 'Microsoft asked us to slow down. Wait a few minutes and run this again.' }
    }
    return $null
}

# --------------------------------------------------------------------------- #
# Live progress page. run-all streams every collector's output into
# $SitePath\progress.js; refresh-status.html (copied next to it) polls that
# file once a second and renders the run - steps, live activity, stats, and a
# plain-English finish. The task stays simple; the progress looks like work.
# --------------------------------------------------------------------------- #

$script:StatusEnabled = $false
$script:StatusDone = $false
$script:StatusOk = $true
$script:StatusSummary = @()
$script:StatusStats = @()
$script:StatusLog = New-Object System.Collections.Generic.List[string]
$script:StatusLastWrite = Get-Date '2000-01-01'
$script:StatusJsPath = $null
$script:StatusSteps = @(
    [pscustomobject]@{ key='signin';    label='Signing you in';                      detail='A Microsoft sign-in window opens - read-only access'; state='pending'; seconds=$null; now=$null }
    [pscustomobject]@{ key='tenant';    label='Documenting your Microsoft 365 setup'; detail='Users, sign-in policies, groups, apps, devices';       state='pending'; seconds=$null; now=$null }
    [pscustomobject]@{ key='security';  label='Checking security posture';            detail='MFA coverage, admin accounts, stale accounts, legacy sign-ins'; state='pending'; seconds=$null; now=$null }
    [pscustomobject]@{ key='licensing'; label='Reviewing licenses';                   detail='Paid seats nobody is using';                            state='pending'; seconds=$null; now=$null }
    [pscustomobject]@{ key='fleet';     label='Polling printers';                     detail='Optional - only when printers are configured';          state='pending'; seconds=$null; now=$null }
    [pscustomobject]@{ key='build';     label='Building your console';                detail='Turning everything into readable pages';                state='pending'; seconds=$null; now=$null }
    [pscustomobject]@{ key='alerts';    label='Sending alerts';                       detail='Only if something is new, worse or cleared';           state='pending'; seconds=$null; now=$null }
)

function Update-RefreshStatus {
    param([switch]$Force, [switch]$Done, [bool]$Ok = $true, [string[]]$Summary = @())
    if (-not $script:StatusEnabled) { return }
    $nowT = Get-Date
    if (-not $Force -and -not $Done -and ($nowT - $script:StatusLastWrite).TotalMilliseconds -lt 600) { return }
    $script:StatusLastWrite = $nowT
    if ($Done) { $script:StatusDone = $true; $script:StatusOk = $Ok; $script:StatusSummary = @($Summary) }
    $payload = [ordered]@{
        done    = $script:StatusDone
        ok      = $script:StatusOk
        summary = @($script:StatusSummary)
        steps   = @($script:StatusSteps | ForEach-Object {
            [ordered]@{ key=$_.key; label=$_.label; detail=$_.detail; state=$_.state; seconds=$_.seconds; now=$_.now } })
        stats   = @($script:StatusStats)
        log     = @(@($script:StatusLog.ToArray()) | Select-Object -Last 30)
    }
    try {
        ('window.PROGRESS = ' + ($payload | ConvertTo-Json -Depth 6 -Compress) + ';') |
            Set-Content -Path $script:StatusJsPath -Encoding UTF8
    } catch { }
}

function Set-StepState {
    param([string]$Key, [string]$State, [string]$Detail, $Seconds, [string]$Plain)
    $s = $script:StatusSteps | Where-Object { $_.key -eq $Key }
    if (-not $s) { return }
    $s.state = $State
    if ($Detail) { $s.detail = $Detail }
    if ($null -ne $Seconds) { $s.seconds = [math]::Round([double]$Seconds, 1) }
    if ($Plain) { $s.detail = $Plain }
    Update-RefreshStatus -Force
}

function Add-StatusLine {
    param([string]$Key, [string]$Line)
    if (-not $script:StatusEnabled) { return }
    $script:StatusLog.Add($Line)
    if ($script:StatusLog.Count -gt 200) { $script:StatusLog.RemoveRange(0, $script:StatusLog.Count - 200) }
    $s = $script:StatusSteps | Where-Object { $_.key -eq $Key }
    if ($s) { $s.now = $Line }
    Update-RefreshStatus
}

function Update-StatsFromOutputs {
    # Re-read whatever output files exist and rebuild the stat chips. Called
    # after each step; cheap, idempotent, and always consistent with disk.
    if (-not $script:StatusEnabled) { return }
    $stats = New-Object System.Collections.Generic.List[object]
    try {
        $rs = Get-Content (Join-Path (Join-Path $OutputRoot 'tenant-docs') 'run-summary.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        $k = $rs.Kpis
        if ($null -ne $k.Members)          { $stats.Add(@{ v = $k.Members; k = 'people' }) }
        if ($null -ne $k.CaTotal)          { $stats.Add(@{ v = "$($k.CaEnabled) of $($k.CaTotal)"; k = 'sign-in policies enforced' }) }
        if ($null -ne $k.AppRegistrations) { $stats.Add(@{ v = $k.AppRegistrations; k = 'registered apps' }) }
        if ($k.EnrolledDevices)            { $stats.Add(@{ v = $k.EnrolledDevices; k = 'managed devices' }) }
        if ($k.CredsExpired)               { $stats.Add(@{ v = $k.CredsExpired; k = 'expired app credentials' }) }
    } catch { }
    try {
        $ss = Get-Content (Join-Path $OutputRoot 'security-snapshot.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -ne $ss.MfaCoverage.CoveragePercent) { $stats.Add(@{ v = "$($ss.MfaCoverage.CoveragePercent)%"; k = 'MFA coverage' }) }
        $na = @($ss.NeedsAttention).Count
        if ($na) { $stats.Add(@{ v = $na; k = 'security items to review' }) }
    } catch { }
    try {
        $lj = Get-Content (Join-Path $OutputRoot 'licensing.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -ne $lj.LicensedUsers) { $stats.Add(@{ v = $lj.LicensedUsers; k = 'licensed people' }) }
        $un = 0; foreach ($r in @($lj.SkuSummary)) { $un += [int]$r.Unassigned }
        if ($un) { $stats.Add(@{ v = $un; k = 'unused paid seats' }) }
        $rc = @($lj.ReclaimCandidates).Count
        if ($rc) { $stats.Add(@{ v = $rc; k = 'licenses to review' }) }
    } catch { }
    $script:StatusStats = @($stats.ToArray())
    Update-RefreshStatus -Force
}

function ConvertTo-PsLiteral {
    <# One argument value as PowerShell source, for the wrapper script below. #>
    param($Value)
    if ($null -eq $Value) { return '$null' }
    if ($Value -is [bool])   { return $(if ($Value) { '$true' } else { '$false' }) }
    if ($Value -is [switch]) { return $(if ($Value.IsPresent) { '$true' } else { '$false' }) }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [double]) { return "$Value" }
    return "'" + ($Value -replace "'", "''") + "'"
}

function Read-NewText {
    <# The bytes added to a file since $Offset. The child is still writing to
       it, so it is opened shared - a plain Get-Content would fight for it. #>
    param([string]$Path, [ref]$Offset)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    } catch { return '' }
    try {
        if ($fs.Length -le $Offset.Value) { return '' }
        $null = $fs.Seek($Offset.Value, [IO.SeekOrigin]::Begin)
        $buf = New-Object byte[] ($fs.Length - $Offset.Value)
        $read = $fs.Read($buf, 0, $buf.Length)
        $Offset.Value += $read
        return [Text.Encoding]::UTF8.GetString($buf, 0, $read)
    } finally { $fs.Dispose() }
}

function Stop-ProcessTree {
    param($Process)
    if (-not $Process -or $Process.HasExited) { return }
    if ($env:OS -eq 'Windows_NT') {
        # /T takes the children with it - a collector may have started its own.
        try { & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null } catch { }
    }
    try { if (-not $Process.HasExited) { $Process.Kill() } } catch { }
}

function Invoke-Step {
    <# Runs one collector, times it, and turns a failure into a recorded result
       rather than an aborted run.

       The collector runs in a CHILD PowerShell, not in this one. That is the
       whole point: a step that blocks - and the way these block is a Microsoft
       sign-in window opening behind the window you are watching, which nobody
       ever sees or finishes - cannot be interrupted from inside the process it
       is blocking. A child can be killed. So it gets a deadline, and if it
       runs past it the process tree is stopped and the step is reported as
       "took longer than N minutes" instead of the refresh sitting there for
       ever. The child is started -NonInteractive too, so a prompt fails rather
       than waits; the deadline is what makes that a guarantee.

       Output is streamed out of the child's redirect files as it appears, so
       the progress page still shows each stage AS IT HAPPENS. #>
    param(
        [string]$Name,
        [string]$ScriptPath,
        [hashtable]$Arguments,
        [switch]$Skip,
        [string]$SkipReason,
        [string]$StepKey,
        [int]$TimeoutMinutes = 0
    )
    if ($Skip) {
        Add-Result $Name 'skipped' $SkipReason
        if ($SkipReason) { Set-StepState $StepKey 'skipped' -Detail "Skipped - $SkipReason" }
        else { Set-StepState $StepKey 'skipped' }
        return
    }
    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        Add-Result $Name 'missing' "not found: $ScriptPath"
        Set-StepState $StepKey 'missing' -Plain (Get-PlainWords $Name "not found: $ScriptPath")
        Write-Warning "$Name - script not found at $ScriptPath"
        return
    }
    Write-Host ''
    Write-Host "--- $Name ".PadRight(72, '-')
    Set-StepState $StepKey 'running'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    $tag = [guid]::NewGuid().ToString('n').Substring(0, 8)
    $tmp = [IO.Path]::GetTempPath()
    $wrapper = Join-Path $tmp "itops-step-$tag.ps1"
    $outFile = Join-Path $tmp "itops-step-$tag.out"
    $errFile = Join-Path $tmp "itops-step-$tag.err"
    $doneFile = Join-Path $tmp "itops-step-$tag.done"

    $body = New-Object System.Collections.Generic.List[string]
    $body.Add('$ErrorActionPreference = ' + "'Stop'")
    $body.Add('$ProgressPreference = ' + "'SilentlyContinue'")
    if ($script:ChildConnect) {
        # Only the app route needs saying: it is app-only, and a collector left
        # to itself would ask for delegated scopes instead. On the person route
        # each collector signs in from this account's saved sign-in, which is
        # already on disk - no window, and nothing here to get wrong.
        $body.Add('Import-Module Microsoft.Graph.Authentication -ErrorAction Stop')
        $body.Add($script:ChildConnect)
    }
    $body.Add('$stepArgs = @{')
    foreach ($k in $Arguments.Keys) { $body.Add("    '$k' = " + (ConvertTo-PsLiteral $Arguments[$k])) }
    $body.Add('}')
    # The wrapper records its OWN result. Reading it back from the process is
    # not dependable: a process from Start-Process -PassThru does not populate
    # ExitCode on Windows PowerShell until WaitForExit has cached its handle,
    # and a null exit code is not 0 - which reported every collector as failed
    # while they were all working perfectly. WaitForExit is called below as
    # well; this file is what actually decides.
    $body.Add('$rc = 0')
    $body.Add('try {')
    $body.Add('  & ' + (ConvertTo-PsLiteral $ScriptPath) + ' @stepArgs *>&1 | ForEach-Object { "$_" }')
    $body.Add('} catch { $rc = 1; "ERROR: " + $_.Exception.Message }')
    $body.Add('Set-Content -LiteralPath ' + (ConvertTo-PsLiteral $doneFile) + ' -Value $rc -Encoding ASCII')
    $body.Add('exit $rc')
    Set-Content -Path $wrapper -Value ($body -join [Environment]::NewLine) -Encoding UTF8

    $engine = $null
    try { $engine = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName } catch { }
    if (-not $engine) { $engine = if ($env:OS -eq 'Windows_NT') { 'powershell.exe' } else { 'pwsh' } }
    $psArgs = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $wrapper)

    $script:StepOutAt = 0
    $script:StepErrAt = 0
    $carry = ''
    $proc = $null
    $timedOut = $false
    $deadline = if ($TimeoutMinutes -gt 0) { (Get-Date).AddMinutes($TimeoutMinutes) } else { $null }

    try {
        # -WindowStyle exists only on Windows PowerShell; asking for it
        # anywhere else is an error, not a no-op.
        $start = @{ FilePath = $engine; ArgumentList = $psArgs; PassThru = $true
                    RedirectStandardOutput = $outFile; RedirectStandardError = $errFile }
        if ($env:OS -eq 'Windows_NT') { $start['WindowStyle'] = 'Hidden' }
        $proc = Start-Process @start
        while ($true) {
            $done = $proc.HasExited
            $text = (Read-NewText $outFile ([ref]$script:StepOutAt)) + (Read-NewText $errFile ([ref]$script:StepErrAt))
            if ($text) {
                $text = $carry + $text
                $parts = $text -split "`r?`n"
                $carry = $parts[-1]
                for ($i = 0; $i -lt $parts.Count - 1; $i++) {
                    $line = "$($parts[$i])".TrimEnd()
                    if ($line) { Write-Host $line; Add-StatusLine $StepKey $line }
                }
            }
            if ($done) { break }
            if ($deadline -and (Get-Date) -gt $deadline) {
                $timedOut = $true
                Stop-ProcessTree $proc
                Start-Sleep -Milliseconds 500
                break
            }
            Start-Sleep -Milliseconds 300
        }
        if ($carry.Trim()) { Write-Host $carry.Trim(); Add-StatusLine $StepKey $carry.Trim() }
        if (-not $timedOut) { try { $proc.WaitForExit() } catch { } }
        $sw.Stop()

        if ($timedOut) {
            $plain = "$Name took longer than $TimeoutMinutes minutes and was stopped, so this refresh has no fresh data from it. The usual cause is a Microsoft sign-in window waiting behind another window. Double-click ""Refresh IT Ops Data"" to try again."
            Add-Result $Name 'FAILED' "stopped after $TimeoutMinutes minutes" $sw.Elapsed.TotalSeconds
            Add-StatusLine $StepKey "Stopped after $TimeoutMinutes minutes."
            Set-StepState $StepKey 'failed' -Seconds $sw.Elapsed.TotalSeconds -Plain $plain
            Write-Warning $plain
            return
        }
        # What the wrapper wrote is the answer; the process's own exit code is
        # only consulted when the wrapper never got to write one at all.
        $rc = $null
        if (Test-Path -LiteralPath $doneFile) {
            try { $rc = [int]((Get-Content -LiteralPath $doneFile -Raw).Trim()) } catch { $rc = $null }
        }
        if ($null -eq $rc) {
            try { $rc = $proc.ExitCode } catch { $rc = $null }
            if ($null -eq $rc) { $rc = 1 }   # it did not finish cleanly enough to say
        }
        if ($rc -ne 0) {
            $why = ''
            try { $why = (Get-Content -LiteralPath $errFile -Raw -ErrorAction Stop) } catch { }
            $why = @("$why" -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1) -join ''
            if (-not $why) { $why = "it stopped without saying why (result $rc)" }
            Add-Result $Name 'FAILED' $why $sw.Elapsed.TotalSeconds
            Add-StatusLine $StepKey "ERROR: $why"
            Set-StepState $StepKey 'failed' -Seconds $sw.Elapsed.TotalSeconds -Plain (Get-PlainWords $Name $why)
            Write-Warning "$Name failed: $why"
            return
        }
        Add-Result $Name 'ok' '' $sw.Elapsed.TotalSeconds
        Set-StepState $StepKey 'ok' -Seconds $sw.Elapsed.TotalSeconds
    } catch {
        $sw.Stop()
        Stop-ProcessTree $proc
        Add-Result $Name 'FAILED' $_.Exception.Message $sw.Elapsed.TotalSeconds
        Add-StatusLine $StepKey "ERROR: $($_.Exception.Message)"
        Set-StepState $StepKey 'failed' -Seconds $sw.Elapsed.TotalSeconds -Plain (Get-PlainWords $Name $_.Exception.Message)
        Write-Warning "$Name failed: $($_.Exception.Message)"
    } finally {
        Remove-Item $wrapper, $outFile, $errFile, $doneFile -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Native {
    param([string]$Name, [string]$Exe, [string[]]$NativeArgs, [string]$WorkDir, [switch]$Skip, [string]$StepKey)
    if ($Skip) { Add-Result $Name 'skipped'; Set-StepState $StepKey 'skipped'; return }
    Write-Host ''
    Write-Host "--- $Name ".PadRight(72, '-')
    Set-StepState $StepKey 'running'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $prev = Get-Location
    try {
        if ($WorkDir) { Set-Location -LiteralPath $WorkDir }
        & $Exe @NativeArgs 2>&1 | ForEach-Object {
            $line = "$_".TrimEnd()
            if ($line) { Write-Host $line; Add-StatusLine $StepKey $line }
        }
        $code = $LASTEXITCODE
        $sw.Stop()
        if ($code -ne 0) {
            Add-Result $Name 'FAILED' "exit code $code" $sw.Elapsed.TotalSeconds
            Set-StepState $StepKey 'failed' -Seconds $sw.Elapsed.TotalSeconds -Plain (Get-PlainWords $Name "exit code $code")
            Write-Warning "$Name exited with code $code"
        } else {
            Add-Result $Name 'ok' '' $sw.Elapsed.TotalSeconds
            Set-StepState $StepKey 'ok' -Seconds $sw.Elapsed.TotalSeconds
        }
    } catch {
        $sw.Stop()
        Add-Result $Name 'FAILED' $_.Exception.Message $sw.Elapsed.TotalSeconds
        Set-StepState $StepKey 'failed' -Seconds $sw.Elapsed.TotalSeconds -Plain (Get-PlainWords $Name $_.Exception.Message)
        Write-Warning "$Name failed: $($_.Exception.Message)"
    } finally {
        Set-Location $prev
    }
}

function Save-HistorySnapshot {
    <# Archive a collector's JSON so the console can draw posture-over-time
       trends - only when that step JUST succeeded, so a failed or stale file
       never becomes a data point. Same idea entra-tenant-docs uses for itself;
       done here, in the orchestrator, so the collectors stay simple. #>
    param([string]$Step, [string]$Source, [string]$HistoryDir, [string]$Prefix)
    $ok = @($results.ToArray() | Where-Object { $_.Step -eq $Step -and $_.Status -eq 'ok' }).Count -gt 0
    if (-not $ok -or -not (Test-Path -LiteralPath $Source)) { return }
    try {
        $null = New-Item -ItemType Directory -Path $HistoryDir -Force
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
        Copy-Item -LiteralPath $Source -Destination (Join-Path $HistoryDir "$Prefix-$stamp.json") -Force
    } catch {
        Write-Warning "Could not archive the $Prefix snapshot for trends ($($_.Exception.Message))."
    }
}
$historyRoot = Join-Path $OutputRoot 'history'

# --------------------------------------------------------------------------- #
# Sign in once for all three Entra tools
# --------------------------------------------------------------------------- #

# Write the progress page first, so the person has something to watch while
# the sign-in window is up - and so a failure anywhere still reports somewhere.
# It is written on EVERY run, -NoStatusPage or not. That switch means "do not
# open a browser window here", which is a separate thing: the console's own
# "Refresh now" button sends you to this page itself, and a run that writes no
# progress leaves that page sitting on "waiting for the first step" for ever,
# or - worse - showing the last run's "All done".
$tpl = Join-Path $PSScriptRoot 'refresh-status.html'
if (Test-Path $tpl) {
    try {
        $null = New-Item -ItemType Directory -Path $SitePath -Force
        Copy-Item $tpl (Join-Path $SitePath 'status.html') -Force
        $script:StatusJsPath = Join-Path $SitePath 'progress.js'
        $script:StatusEnabled = $true
        Update-RefreshStatus -Force
    } catch { $script:StatusEnabled = $false }
    # Opening the browser is best-effort and must never disable the page
    # itself - a headless or non-Windows host still gets progress.js.
    if ($script:StatusEnabled -and -not $NoStatusPage) {
        try { Start-Process (Join-Path $SitePath 'status.html') | Out-Null }
        catch { Write-Host "Progress page: $(Join-Path $SitePath 'status.html') (open it in a browser to watch)" }
    }
}

$needGraph = -not ($SkipTenantDocs -and $SkipSecurity -and $SkipLicensing)

# ---- what setup decided for this machine (automatic-refresh.ini) ---------- #
function Read-IniFile {
    <# Tiny INI reader: sections -> keys -> trimmed values. Comments start with
       ; or #. A missing file is an empty config, never an error. #>
    param([string]$Path)
    $ini = @{}
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $ini }
    $section = ''
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $raw.Trim()
        if (-not $line -or $line[0] -in ';', '#') { continue }
        if ($line -match '^\[(.+)\]$') { $section = $matches[1].Trim().ToLowerInvariant(); if (-not $ini.ContainsKey($section)) { $ini[$section] = @{} }; continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $line.Substring(0, $eq).Trim().ToLowerInvariant()
        $val = $line.Substring($eq + 1)
        $c = $val.IndexOf(' ;'); if ($c -ge 0) { $val = $val.Substring(0, $c) }
        $c = $val.IndexOf(' #'); if ($c -ge 0) { $val = $val.Substring(0, $c) }
        if (-not $ini.ContainsKey($section)) { $ini[$section] = @{} }
        $ini[$section][$key] = $val.Trim()
    }
    return $ini
}
function Get-IniValue {
    param($Ini, [string]$Section, [string]$Key, [string]$Default = '')
    if ($Ini.ContainsKey($Section) -and $Ini[$Section].ContainsKey($Key) -and $Ini[$Section][$Key]) { return $Ini[$Section][$Key] }
    return $Default
}

function Get-RefreshCertificateInfo {
    <# The registered-app route needs three things from the ini (tenant, app,
       certificate thumbprint) and the certificate itself in this computer's
       store. Report what is there and how long the certificate has left, so
       the run - and the console - can say "expires in 12 days" before it
       silently stops working. #>
    param($Ini)
    $info = @{ Configured = $false; TenantId = ''; ClientId = ''; Thumbprint = ''; Present = $false; Expires = $null; DaysLeft = $null; Expired = $false; KeyUsable = $false; KeyWhy = '' }
    $info.Thumbprint = (Get-IniValue $Ini 'signin' 'certificate_thumbprint') -replace '\s', ''
    $info.TenantId   = Get-IniValue $Ini 'signin' 'tenant_id'
    $info.ClientId   = Get-IniValue $Ini 'signin' 'client_id'
    if (-not ($info.Thumbprint -and $info.TenantId -and $info.ClientId)) { return $info }
    $info.Configured = $true
    $cert = $null
    foreach ($store in 'Cert:\LocalMachine\My', 'Cert:\CurrentUser\My') {
        try { $cert = Get-Item "$store\$($info.Thumbprint)" -ErrorAction Stop; if ($cert) { break } } catch { $cert = $null }
    }
    if ($cert) {
        $info.Present = $true
        $info.Expires = [datetime]$cert.NotAfter
        # SEEING the certificate is not the same as being allowed to USE it.
        # The private key of a LocalMachine certificate is readable by SYSTEM
        # and Administrators, so an ordinary sign-in can list this certificate
        # and still fail at "Keyset does not exist" the moment Graph needs it.
        # Ask now, while there is somewhere sensible to say so.
        # Usable unless we can PROVE otherwise. A check that cannot run (an
        # older certificate object, a non-Windows host) must not condemn a
        # certificate that would have worked - the sign-in itself is the real
        # test, and its error now comes back in plain words either way.
        $info.KeyUsable = $true
        $hasKeyProp = $cert.PSObject.Properties['HasPrivateKey']
        if ($hasKeyProp -and -not $cert.HasPrivateKey) {
            $info.KeyUsable = $false
            $info.KeyWhy = 'this copy of it has no private key'
        } elseif ($hasKeyProp) {
            try {
                $key = [Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert)
                if ($key) { try { $key.Dispose() } catch { } }
                else {
                    $info.KeyUsable = $false
                    $info.KeyWhy = 'this account is not allowed to use its private key'
                }
            } catch {
                # Could not ask. Leave it usable and let the sign-in decide.
            }
        }
    } else {
        # Not in the store (or no store on this OS): fall back to the expiry
        # setup recorded, so the words about it are still right.
        $recorded = Get-IniValue $Ini 'signin' 'certificate_expires'
        if ($recorded) { try { $info.Expires = [datetime]::ParseExact($recorded, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) } catch { } }
    }
    if ($info.Expires) {
        $info.DaysLeft = [int][math]::Floor(($info.Expires - (Get-Date)).TotalDays)
        $info.Expired = $info.DaysLeft -lt 0
    }
    return $info
}

function Get-CertSignInWords {
    <# The certificate sign-in's own error text, with a sentence in front of it
       that says what to do. The original is KEPT - an error code is what you
       search for when the plain words are not enough - but "Keyset does not
       exist" on its own tells nobody anything, and it is the first thing the
       person who clicked Refresh has to read. #>
    param([string]$Message)
    $plain = switch -Wildcard ($Message) {
        '*Keyset does not exist*'       { "this account is not allowed to use the certificate's private key. Re-run setup as an administrator to set it up again." }
        '*key is not accessible*'       { "this account is not allowed to use the certificate's private key. Re-run setup as an administrator to set it up again." }
        '*Cannot find the certificate*' { 'the certificate is no longer in this computer. Re-run setup to make a new one and upload it.' }
        '*AADSTS700027*'                { 'Microsoft 365 does not recognise this certificate any more. Re-run setup to make a new one and upload it.' }
        '*AADSTS7000215*'               { 'Microsoft 365 rejected the credential. Re-run setup to make a new one and upload it.' }
        '*Authorization_RequestDenied*' { 'the app signed in but is not allowed to read - a Global Administrator still has to grant admin consent.' }
        default                         { '' }
    }
    if (-not $plain) { return $Message }
    return "$plain ($($Message.Trim()))"
}

function Invoke-SignInProbe {
    <# A scheduled run signs in from a short-lived child PowerShell with a time
       limit. If the saved sign-in is still good the child finishes in seconds
       and leaves the token in the per-user cache, so this process's own
       Connect-MgGraph then completes without a window. If a window IS needed
       and nobody finishes it in time, the child is killed and the run carries
       on without Microsoft 365 - a plain sentence about it instead of a hang. #>
    param([string[]]$Scopes, [int]$TimeoutSeconds)
    $onWin = ($env:OS -eq 'Windows_NT')
    $engine = if ($onWin) { 'powershell.exe' } else { 'pwsh' }
    $scopeList = ($Scopes | ForEach-Object { "'$_'" }) -join ','
    $code = "Import-Module Microsoft.Graph.Authentication -ErrorAction Stop; " +
            "Connect-MgGraph -Scopes @($scopeList) -NoWelcome -ErrorAction Stop; " +
            "if (Get-MgContext) { exit 0 } else { exit 3 }"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
    $psArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $enc)
    if ($onWin) { $psArgs = @('-WindowStyle', 'Hidden') + $psArgs }
    $minutes = [math]::Max(1, [int][math]::Round($TimeoutSeconds / 60))
    try {
        $p = Start-Process -FilePath $engine -ArgumentList $psArgs -PassThru
        if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
            try { $p.Kill() } catch { }
            return @{ Ok = $false; Detail = "Nobody finished the Microsoft sign-in window within $minutes minute$(if ($minutes -ne 1) { 's' })." }
        }
        if ($p.ExitCode -eq 0) { return @{ Ok = $true; Detail = '' } }
        return @{ Ok = $false; Detail = "The Microsoft sign-in did not complete (exit code $($p.ExitCode))." }
    } catch {
        return @{ Ok = $false; Detail = "Could not start the Microsoft sign-in ($($_.Exception.Message))." }
    }
}

$refreshIni   = Read-IniFile $RefreshConfig
$schedMode    = (Get-IniValue $refreshIni 'schedule' 'mode' 'off').ToLowerInvariant()
$schedTime    = Get-IniValue $refreshIni 'schedule' 'time'
$schedRunAs   = Get-IniValue $refreshIni 'schedule' 'run_as'
# Staying signed in between runs is a choice a person made in setup, and only
# for the "while I'm signed in" schedule. Anything else signs out at the end,
# exactly as before.
# Two ways to end a run still signed in:
#   - the "refresh while I'm signed in" schedule, which a person chose in setup;
#   - a refresh a person started THEMSELVES. Signing them out at the end only
#     means the next click asks them to pick their account again, which is the
#     whole cost and none of the benefit: they are sitting right here.
# A scheduled run and an app sign-in always close, as before.
$keepSignedIn = (-not $Scheduled) -or
                (($schedMode -eq 'while-signed-in') -and
                 ((Get-IniValue $refreshIni 'signin' 'keep_signed_in' 'no') -match '^(yes|true|1)$'))
$certInfo     = Get-RefreshCertificateInfo $refreshIni

# ---- the sign-in ladder --------------------------------------------------- #
# 1. the registered app + this computer's certificate (unattended schedule)
# 2. the saved sign-in, silently (a person set up "while I'm signed in")
# 3. a sign-in window - with a time limit when nobody may be there
# 4. stop cleanly: no Microsoft 365 data this run, one plain sentence why
# A rung that is passed over is REPORTED, not hidden - an expired certificate
# must not be papered over by a sign-in that happened to still work.
$script:GraphConnectedByUs = $false
$script:SignIn = [ordered]@{ Mode = 'none'; Ok = $true; Detail = ''; Dropped = @(); Missing = @() }
function Drop-SignInRung {
    param([string]$Why)
    $script:SignIn.Dropped = @($script:SignIn.Dropped) + $Why
    Write-Warning $Why
    Add-StatusLine 'signin' $Why
}

if (-not $needGraph) {
    Set-StepState 'signin' 'skipped' -Detail 'Nothing to collect from Microsoft 365 this run'
    $script:SignIn.Mode = 'not-needed'
} elseif ($NoConnect) {
    Set-StepState 'signin' 'skipped' -Detail 'Using the session you already opened'
    $script:SignIn.Mode = 'existing'
} else {
    Set-StepState 'signin' 'running'
    $scopes = @(
        'Directory.Read.All'
        'Policy.Read.All'
        'RoleManagement.Read.Directory'
        'Application.Read.All'
        'Organization.Read.All'
        'User.Read.All'
        'AuditLog.Read.All'
        'DeviceManagementConfiguration.Read.All'
        'DeviceManagementManagedDevices.Read.All'
        'DeviceManagementApps.Read.All'
    )
    if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Authentication)) {
        $script:SignIn.Detail = 'The Microsoft Graph PowerShell module is not installed for this account. Re-run setup, or: Install-Module Microsoft.Graph.Authentication -Scope CurrentUser'
    } else {
        Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
        $ctx = Get-MgContext
        if ($ctx) {
            $who = if ($ctx.Account) { $ctx.Account } elseif ($ctx.ClientId) { "app $($ctx.ClientId)" } else { 'an existing session' }
            Write-Host "Already connected as $who - reusing that session."
            $missing = @($scopes | Where-Object { $_ -notin @($ctx.Scopes) })
            if ($missing.Count) {
                Write-Warning ("Current session is missing: {0}" -f ($missing -join ', '))
                Write-Warning 'Some sections may come back empty. Disconnect-MgGraph and re-run to get all scopes.'
            }
            $script:SignIn.Mode = 'existing'
        }

        # Rung 1: the registered app. This is the SCHEDULED run's route: the
        # certificate lives in the computer's store, where its private key is
        # readable by SYSTEM and Administrators - not by you at your desk. So
        # a refresh you started yourself does not try it. It used to, and the
        # failure ("Keyset does not exist") dropped a browser sign-in window
        # in front of whoever clicked Refresh, every single time.
        if ($script:SignIn.Mode -eq 'none' -and $schedMode -eq 'unattended' -and -not $Scheduled) {
            $note = 'The registered app and its certificate are for the scheduled refresh, which runs as this computer. This one is yours, so it signs in as you.'
            Write-Host $note
            Add-StatusLine 'signin' $note
        }
        if ($script:SignIn.Mode -eq 'none' -and $schedMode -eq 'unattended' -and $Scheduled) {
            if (-not $certInfo.Configured) {
                Drop-SignInRung 'Automatic refresh is set to run unattended, but the registered app or its certificate is missing from automatic-refresh.ini - re-run setup to finish that step.'
            } elseif ($certInfo.Expired) {
                Drop-SignInRung ("The automatic-refresh certificate expired on {0} - re-run setup to make a new one and upload it." -f $certInfo.Expires.ToString('yyyy-MM-dd'))
            } elseif (-not $certInfo.Present) {
                Drop-SignInRung "The automatic-refresh certificate ($($certInfo.Thumbprint)) is not in this computer's certificate store."
            } elseif (-not $certInfo.KeyUsable) {
                Drop-SignInRung ("The automatic-refresh certificate is in this computer's store, but {0}. Re-run setup as an administrator to set it up again." -f $certInfo.KeyWhy)
            } else {
                Write-Host 'Connecting to Microsoft Graph as the registered app (read-only)...'
                Add-StatusLine 'signin' 'Signing in as the registered app - no window needed.'
                try {
                    Connect-MgGraph -ClientId $certInfo.ClientId -TenantId $certInfo.TenantId `
                        -CertificateThumbprint $certInfo.Thumbprint -NoWelcome -ErrorAction Stop
                    $script:GraphConnectedByUs = $true
                    $script:SignIn.Mode = 'app'
                } catch {
                    Drop-SignInRung ("Signing in as the registered app failed: " + (Get-CertSignInWords $_.Exception.Message))
                }
            }
        }

        # Rungs 2 and 3: as a person - silently from the saved sign-in when there
        # is one, otherwise a window. A scheduled run puts a time limit on it.
        if ($script:SignIn.Mode -eq 'none') {
            if ($Scheduled) {
                Write-Host 'Signing in to Microsoft Graph from the saved sign-in (read-only scopes)...'
                Add-StatusLine 'signin' 'Signing in from the saved sign-in - a window opens only if that has expired.'
                $probe = Invoke-SignInProbe -Scopes $scopes -TimeoutSeconds $SignInTimeoutSeconds
                if ($probe.Ok) {
                    try {
                        Connect-MgGraph -Scopes $scopes -NoWelcome -ErrorAction Stop
                        $script:GraphConnectedByUs = $true
                        $script:SignIn.Mode = 'user'
                    } catch { Drop-SignInRung "The sign-in did not complete: $($_.Exception.Message)" }
                } else {
                    Drop-SignInRung $probe.Detail
                }
            } else {
                Write-Host 'Connecting to Microsoft Graph (read-only scopes)...'
                Add-StatusLine 'signin' 'A Microsoft sign-in window is open - finish signing in there.'
                try {
                    Connect-MgGraph -Scopes $scopes -NoWelcome -ErrorAction Stop
                    $script:GraphConnectedByUs = $true
                    $script:SignIn.Mode = 'user'
                } catch { Drop-SignInRung "The sign-in did not complete: $($_.Exception.Message)" }
            }
        }
    }

    if ($script:SignIn.Mode -eq 'none') {
        # Rung 4: stop cleanly. The Microsoft 365 steps are skipped below, the
        # printers and the console build still run, and the exit code says red.
        $script:SignIn.Ok = $false
        $whatNow = 'This refresh ran without Microsoft 365 data. Double-click "Refresh IT Ops Data" to sign in and collect it.'
        if (-not $script:SignIn.Detail) {
            $script:SignIn.Detail = ((@($script:SignIn.Dropped) -join ' ') + ' ' + $whatNow).Trim()
        } else {
            Write-Warning $script:SignIn.Detail     # a reason no rung reported (module missing)
            $script:SignIn.Detail = "$($script:SignIn.Detail) $whatNow"
        }
        Add-Result 'sign-in' 'FAILED' $script:SignIn.Detail
        Set-StepState 'signin' 'failed' -Plain $script:SignIn.Detail
        # The reasons were already warned about as each rung was passed over;
        # what is new here is what happens next.
        Write-Warning $whatNow
    } else {
        $how = switch ($script:SignIn.Mode) {
            'app'      { 'Signed in as the registered app (read-only)' }
            'existing' { 'Using the session already open (read-only)' }
            default    { 'Signed in with read-only access' }
        }
        $script:SignIn.Detail = $how
        Set-StepState 'signin' 'ok' -Detail $how
    }
}
$signedIn = $script:SignIn.Mode -ne 'none'

# What this sign-in can actually reach. A tenant can decline to consent to
# some of the scopes above (Intune is the usual one) and still sign in
# happily. The collectors check for themselves before touching an endpoint -
# calling one you were not granted makes the Graph SDK open a sign-in window
# mid-run, behind whatever you are looking at - but the person deserves to be
# told once, here, rather than wondering why a section is empty.
if ($signedIn -and $script:SignIn.Mode -ne 'existing') {
    try {
        $granted = @((Get-MgContext).Scopes)
        $missing = @($scopes | Where-Object { $_ -notin $granted })
        if ($granted.Count -and $missing.Count) {
            $friendly = @{
                'DeviceManagementConfiguration.Read.All' = 'Intune'
                'DeviceManagementManagedDevices.Read.All' = 'Intune'
                'DeviceManagementApps.Read.All'           = 'Intune'
                'AuditLog.Read.All'                       = 'sign-in history (MFA coverage, stale accounts, legacy auth)'
            }
            $areas = @($missing | ForEach-Object { if ($friendly[$_]) { $friendly[$_] } else { $_ } } | Sort-Object -Unique)
            $line = "This sign-in was not granted everything, so these are skipped: $($areas -join '; '). Everything else is collected as normal."
            Write-Warning $line
            Add-StatusLine 'signin' $line
            $script:SignIn.Missing = $missing
        }
    } catch { }
}

$notSignedIn = if ($signedIn) { $null } else { 'not signed in' }

# Each collector now runs in its own child process, and a Graph session does
# not cross a process boundary. On the person route that is fine: the child
# signs in from this account's saved sign-in, already on disk, without a
# window. The APP route has to be spelled out, because a collector left to
# itself would ask for delegated scopes instead of using the certificate.
$script:ChildConnect = ''
if ($script:SignIn.Mode -eq 'app' -and $certInfo.Configured) {
    $script:ChildConnect = ("Connect-MgGraph -ClientId '{0}' -TenantId '{1}' -CertificateThumbprint '{2}' -NoWelcome -ErrorAction Stop" -f
                            $certInfo.ClientId, $certInfo.TenantId, $certInfo.Thumbprint)
}

function Write-RefreshStatus {
    <# One small JSON the console (and check-setup) read: how the last refresh
       signed in, what was passed over, the schedule this machine is on, and
       how long the certificate has left. Written before the console build so
       the pages reflect THIS run, and again at the end with the outcome. #>
    param([bool]$Final, [bool]$Ok, [string[]]$Summary = @())
    $status = [ordered]@{
        GeneratedUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        Scheduled    = [bool]$Scheduled
        Final        = $Final
        Ok           = $Ok
        Message      = (@($Summary) -join ' ')
        SignIn       = [ordered]@{ Mode = $script:SignIn.Mode; Ok = $script:SignIn.Ok; Detail = $script:SignIn.Detail; Dropped = @($script:SignIn.Dropped); Missing = @($script:SignIn.Missing) }
        Steps        = @($results.ToArray() | ForEach-Object { [ordered]@{ Step = $_.Step; Status = $_.Status; Detail = $_.Detail } })
        Schedule     = [ordered]@{ Mode = $schedMode; Time = $schedTime; RunAs = $schedRunAs }
        KeepSignedIn = [bool]$keepSignedIn
        Certificate  = $null
    }
    if ($certInfo.Configured) {
        $status.Certificate = [ordered]@{
            Thumbprint = $certInfo.Thumbprint
            Present    = [bool]$certInfo.Present
            Expires    = if ($certInfo.Expires) { $certInfo.Expires.ToString('yyyy-MM-dd') } else { $null }
            DaysLeft   = $certInfo.DaysLeft
        }
    }
    try {
        $status | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $OutputRoot 'refresh-status.json') -Encoding UTF8
    } catch { Write-Warning "Could not write refresh-status.json ($($_.Exception.Message))." }
}

$null = New-Item -ItemType Directory -Path $OutputRoot -Force

# --------------------------------------------------------------------------- #
# Collectors
# --------------------------------------------------------------------------- #

Invoke-Step -Name 'entra-tenant-docs' -Skip:($SkipTenantDocs -or -not $signedIn) -StepKey 'tenant' `
    -SkipReason $(if ($SkipTenantDocs) { '' } else { $notSignedIn }) `
    -ScriptPath (Join-Path $ToolRoot 'entra-tenant-docs\Export-EntraTenantDocs.ps1') `
    -Arguments @{ OutputPath = (Join-Path $OutputRoot 'tenant-docs') } `
    -TimeoutMinutes $StepTimeoutMinutes
Update-StatsFromOutputs

Invoke-Step -Name 'entra-security-snapshot' -Skip:($SkipSecurity -or -not $signedIn) -StepKey 'security' `
    -SkipReason $(if ($SkipSecurity) { '' } else { $notSignedIn }) `
    -ScriptPath (Join-Path $ToolRoot 'entra-security-snapshot\Get-EntraSecuritySnapshot.ps1') `
    -Arguments @{
        JsonPath          = (Join-Path $OutputRoot 'security-snapshot.json')
        StaleDays         = $StaleDays
        TimeBudgetMinutes = $SecurityBudgetMinutes
    } `
    -TimeoutMinutes $StepTimeoutMinutes
Update-StatsFromOutputs
Save-HistorySnapshot -Step 'entra-security-snapshot' -Prefix 'security' `
    -Source (Join-Path $OutputRoot 'security-snapshot.json') -HistoryDir (Join-Path $historyRoot 'security')

# License prices auto-engage: a prices.ini sitting beside the license tool
# means "put a dollar figure on the waste". Explicit -LicensePrices overrides.
# After the run, if there is no prices.ini yet, we write a starter listing the
# tenant's own SKUs so the next edit is fill-in-the-blanks, not SKU research.
$licenseRepo = Join-Path $ToolRoot 'm365-license-waste-report'
$pricesPath = if ($LicensePrices) { $LicensePrices } else { Join-Path $licenseRepo 'prices.ini' }
$licenseArgs = @{
    JsonPath  = (Join-Path $OutputRoot 'licensing.json')
    StaleDays = $StaleDays
}
if (Test-Path $pricesPath) { $licenseArgs['PriceList'] = $pricesPath }

Invoke-Step -Name 'm365-license-waste-report' -Skip:($SkipLicensing -or -not $signedIn) -StepKey 'licensing' `
    -SkipReason $(if ($SkipLicensing) { '' } else { $notSignedIn }) `
    -ScriptPath (Join-Path $licenseRepo 'Get-LicenseWasteReport.ps1') `
    -Arguments $licenseArgs `
    -TimeoutMinutes $StepTimeoutMinutes
Update-StatsFromOutputs
Save-HistorySnapshot -Step 'm365-license-waste-report' -Prefix 'licensing' `
    -Source (Join-Path $OutputRoot 'licensing.json') -HistoryDir (Join-Path $historyRoot 'licensing')

# First-run convenience: if the license report ran and there is still no
# prices.ini, list the tenant's real SKUs (blank values) so pricing is a
# fill-in exercise. Never overwrite a file the user has touched.
if (-not $SkipLicensing -and $signedIn -and -not (Test-Path $pricesPath)) {
    $licJson = Join-Path $OutputRoot 'licensing.json'
    if (Test-Path $licJson) {
        try {
            $lic = Get-Content $licJson -Raw | ConvertFrom-Json
            $skuList = @($lic.SkuSummary | ForEach-Object { "$($_.Sku)" } | Sort-Object -Unique)
            if ($skuList.Count) {
                $lines = New-Object System.Collections.Generic.List[string]
                $lines.Add('# Per-seat MONTHLY price for each license SKU. Fill in the numbers')
                $lines.Add('# next to your SKUs, then run "Refresh IT Ops Data" again - the')
                $lines.Add('# console will then show the dollar waste. Skip any you do not care')
                $lines.Add('# about; they keep showing seat counts. Text after ; or # is a comment.')
                $lines.Add('')
                $lines.Add('[settings]')
                $lines.Add('currency = $')
                $lines.Add('')
                $lines.Add('[prices]')
                foreach ($s in $skuList) { $lines.Add(('{0,-28} = ' -f $s)) }
                Set-Content -Path $pricesPath -Value ($lines -join "`r`n") -Encoding UTF8
                Write-Host "Wrote a price-list starter with your SKUs: $pricesPath"
                Write-Host '  Add per-seat prices there and Refresh again to see dollar waste.'
            }
        } catch {
            Write-Warning "Could not write the price-list starter ($($_.Exception.Message))."
        }
    }
}

# Printers auto-engage: a config.ini that differs from the shipped example is
# a person saying "these are my printers", and the Refresh must just pick that
# up - nobody should need command-line parameters to turn a feature on.
# Explicit -FleetConfig/-FleetDb still override; -SkipFleet still wins.
$fleetRepo = Join-Path $ToolRoot 'print-fleet-dashboard'
if (-not $FleetConfig) {
    $autoCfg = Join-Path $fleetRepo 'config.ini'
    $exCfg   = Join-Path $fleetRepo 'config.example.ini'
    if (Test-Path $autoCfg) {
        $edited = $true
        if (Test-Path $exCfg) {
            $a = (Get-Content $autoCfg -Raw) -replace '\s', ''
            $b = (Get-Content $exCfg -Raw) -replace '\s', ''
            $edited = ($a -ne $b)
        }
        if ($edited) { $FleetConfig = $autoCfg }
    }
}
if (-not $FleetDb) { $FleetDb = Join-Path $OutputRoot 'fleet.db' }
$doFleet = -not $SkipFleet -and $FleetConfig
if ($doFleet) {
    Invoke-Native -Name 'print-fleet-collector' -StepKey 'fleet' -Exe $Python -WorkDir $fleetRepo `
        -NativeArgs @('collector.py', '--config', $FleetConfig, '--db', $FleetDb)
} elseif ($SkipFleet) {
    Add-Result 'print-fleet-collector' 'skipped'
    Set-StepState 'fleet' 'skipped'
} else {
    Add-Result 'print-fleet-collector' 'skipped' 'config.ini is still the unedited example'
    Set-StepState 'fleet' 'skipped' -Detail 'Optional - add printer IPs to config.ini and Refresh again'
}

# --------------------------------------------------------------------------- #
# Build the console
# --------------------------------------------------------------------------- #

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Console config not found: $ConfigPath (copy sources.example.ini to sources.ini and edit the paths)"
}

# The console reads refresh-status.json while it builds: write what this run
# knows so far (how the sign-in went, what was passed over, certificate days
# left) so the pages describe THIS refresh, not the previous one.
$soFar = @($results.ToArray() | Where-Object { $_.Status -in 'FAILED', 'missing' })
Write-RefreshStatus -Final $false -Ok ($soFar.Count -eq 0)

Invoke-Native -Name 'console build' -StepKey 'build' -Exe $Python -WorkDir $PSScriptRoot `
    -NativeArgs @('build.py', '--config', $ConfigPath, '--out', $SitePath)

# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
# notify.py is the one thing here that sends anything, and only to the Teams
# webhook / mail relay named in alerts.ini. It runs only when a channel is
# actually configured and the console build (which wrote alerts.json) worked.
$buildOk = @($results.ToArray() | Where-Object { $_.Step -eq 'console build' -and $_.Status -eq 'ok' }).Count -gt 0
$alertsIni = Read-IniFile $AlertsConfig
$teamsHook = Get-IniValue $alertsIni 'teams' 'webhook'
if (-not $teamsHook -and $env:ITOPS_TEAMS_WEBHOOK) { $teamsHook = $env:ITOPS_TEAMS_WEBHOOK }
$mailRelay = Get-IniValue $alertsIni 'email' 'smtp_server'
$haveChannel = [bool]($teamsHook -or $mailRelay)
if ($SkipAlerts) {
    Add-Result 'alerts' 'skipped'
    Set-StepState 'alerts' 'skipped'
} elseif (-not $haveChannel) {
    Add-Result 'alerts' 'skipped' 'no Teams or email channel in alerts.ini'
    Set-StepState 'alerts' 'skipped' -Detail 'Optional - paste a Teams Workflows URL into alerts.ini and Refresh again'
    Write-Host ''
    Write-Host "Alerts: not set up (no channel in $AlertsConfig). The console's Alerts page shows what would be sent."
} elseif (-not $buildOk) {
    Add-Result 'alerts' 'skipped' 'console build did not finish, so alerts.json is not current'
    Set-StepState 'alerts' 'skipped' -Detail 'Skipped - the console build did not finish'
} else {
    Invoke-Native -Name 'alerts' -StepKey 'alerts' -Exe $Python -WorkDir $PSScriptRoot `
        -NativeArgs @('notify.py', '--config', $AlertsConfig,
                      '--alerts', (Join-Path $OutputRoot 'alerts.json'),
                      '--state', (Join-Path $OutputRoot 'alerts-state.json'))
}

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

# Laid out by hand rather than with Format-Table: a non-interactive host (a
# scheduled task, a CI runner) reports no console width and Format-Table then
# silently prints nothing at all.
Write-Host ''
Write-Host ('=' * 72)
Write-Host ('{0,-26} {1,-8} {2,7}  {3}' -f 'Step', 'Status', 'Seconds', 'Detail')
Write-Host ('{0,-26} {1,-8} {2,7}  {3}' -f ('-' * 26), '--------', '-------', ('-' * 24))
foreach ($r in $results.ToArray()) {
    $detail = if ($r.Detail.Length -gt 60) { $r.Detail.Substring(0, 57) + '...' } else { $r.Detail }
    Write-Host ('{0,-26} {1,-8} {2,7}  {3}' -f $r.Step, $r.Status, $r.Seconds, $detail)
}
Write-Host ''

$failed  = @($results.ToArray() | Where-Object { $_.Status -eq 'FAILED' })
$missing = @($results.ToArray() | Where-Object { $_.Status -eq 'missing' })
$consoleFailed = @($failed | Where-Object { $_.Step -eq 'console build' }).Count -gt 0

$plain = @()
foreach ($r in $failed + $missing) {
    $words = Get-PlainWords $r.Step $r.Detail
    if ($words) { $plain += "  $($r.Step): $words" }
}
if ($plain.Count) {
    Write-Host 'In plain words:'
    foreach ($line in $plain) { Write-Host $line }
    Write-Host ''
}

$summaryLines = @()
if ($failed.Count -or $missing.Count) {
    $summaryLines += "$(($failed.Count + $missing.Count)) step(s) had problems:"
    foreach ($line in $plain) { $summaryLines += $line.Trim() }
    if ($consoleFailed) { $summaryLines += 'Because the build step failed, the console still shows its previous data.' }
    else { $summaryLines += 'Everything else ran, and the console shows how old each of its numbers is.' }
} else {
    $summaryLines += 'Everything ran and your console was rebuilt with fresh data.'
    $people = @($script:StatusStats | Where-Object { $_.k -eq 'people' })
    $seats  = @($script:StatusStats | Where-Object { $_.k -eq 'unused paid seats' })
    if ($people.Count -and $seats.Count) {
        $summaryLines += "It covers $($people[0].v) people, and flagged $($seats[0].v) unused paid seats worth a look."
    } elseif ($people.Count) {
        $summaryLines += "It covers $($people[0].v) people."
    }
}
Update-RefreshStatus -Done -Ok:(-not ($failed.Count -or $missing.Count)) -Summary $summaryLines -Force
Write-RefreshStatus -Final $true -Ok (-not ($failed.Count -or $missing.Count)) -Summary $summaryLines

# Sign out of the Graph session we opened, so a shared machine doesn't keep a
# live token cached after the refresh. Only if WE connected - a session you
# started yourself (e.g. app-only with a certificate) is left for you to manage.
# The one exception is a person's choice, made in setup: on a machine set to
# refresh "while I'm signed in", the sign-in is kept so the next automatic run
# does not have to ask again. An app sign-in is always closed - there is no
# saved sign-in to protect, and nothing to keep.
if ($script:GraphConnectedByUs) {
    if ($script:SignIn.Mode -eq 'user' -and $keepSignedIn) {
        if ($Scheduled) {
            Write-Host 'Staying signed in to Microsoft Graph for the next automatic refresh.'
            Write-Host '  (To sign out, re-run setup and choose "I will click Refresh myself".)'
        } else {
            Write-Host 'Staying signed in to Microsoft 365 (read-only), so the next Refresh does not ask again.'
            Write-Host '  (To sign out now, run: Disconnect-MgGraph)'
        }
    } else {
        try { Disconnect-MgGraph -ErrorAction Stop | Out-Null; Write-Host 'Signed out of Microsoft Graph.' }
        catch { Write-Warning "Could not sign out of Graph cleanly ($($_.Exception.Message))." }
    }
}

if ($failed.Count -or $missing.Count) {
    if ($consoleFailed) {
        Write-Warning ("{0} step(s) did not complete, including the console build itself - the console was NOT rebuilt this run; whatever site existed before is unchanged." -f ($failed.Count + $missing.Count))
    } else {
        Write-Warning ("{0} step(s) did not complete. The console was still built - the feeds those steps produce will show their real age." -f ($failed.Count + $missing.Count))
    }
    exit 1
}

Write-Host "Console built: $(Join-Path $SitePath 'index.html')"
exit 0
