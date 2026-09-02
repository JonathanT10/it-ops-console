# Checks an IT Ops Console install and says what it finds, in plain words.
# READ-ONLY: looks at folders, shortcuts and prerequisites, changes nothing.
# Right-click > Run with PowerShell. Everything is also written to
# check-setup.log beside this file, so it can be sent to whoever is helping you.
$ErrorActionPreference = 'Continue'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
try { Start-Transcript -Path (Join-Path $here 'check-setup.log') -Force | Out-Null } catch { }

$problems = 0
function Say  { param($m) Write-Host "  OK      $m" }
function Gap  { param($m) Write-Host "  PROBLEM $m"; $script:problems++ }
function Note { param($m) Write-Host "  note    $m" }

Write-Host ''
Write-Host '=== IT Ops Console health check ========================================'
Write-Host "PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition))"
Write-Host ''

# ---- find the install through the desktop shortcuts ---- #
$desktop = [Environment]::GetFolderPath('Desktop')
$root = $null
$shell = New-Object -ComObject WScript.Shell
$console = Join-Path $desktop 'IT Ops Console.lnk'
$refresh = Join-Path $desktop 'Refresh IT Ops Data.lnk'
if (Test-Path $console) {
    $target = $shell.CreateShortcut($console).TargetPath
    Say "desktop shortcut 'IT Ops Console' -> $target"
    if ($target) { $root = Split-Path (Split-Path $target -Parent) -Parent }
} else { Gap "desktop shortcut 'IT Ops Console' is missing - re-run setup to recreate it" }
if (Test-Path $refresh) { Say "desktop shortcut 'Refresh IT Ops Data' present" }
else { Gap "desktop shortcut 'Refresh IT Ops Data' is missing - re-run setup to recreate it" }
if (-not $root) { $root = 'C:\IT-Ops'; Note "assuming the install folder is $root" }

# ---- layout ---- #
$tools = Join-Path $root 'tools'; $output = Join-Path $root 'output'; $site = Join-Path $root 'console-site'
foreach ($p in @($root, $tools, $output, $site)) {
    if (Test-Path $p) { Say $p } else { Gap "$p is missing - re-run setup" }
}
foreach ($r in 'entra-tenant-docs','entra-security-snapshot','m365-license-waste-report','print-fleet-dashboard','it-ops-console') {
    if (Test-Path (Join-Path $tools $r)) { Say "tool: $r" } else { Gap "tool missing: $r - re-run setup to download it" }
}
if (Test-Path (Join-Path (Join-Path $tools 'it-ops-console') 'sources.ini')) { Say 'sources.ini (the wiring file)' }
else { Gap 'sources.ini is missing - re-run setup to rewrite it' }

# ---- can other local users read your collected data? ---- #
# The collected data (admin names, stale accounts, app inventory) lives under
# the install folder. Setup locks it to you + Administrators; flag it here if
# that lock is missing so a re-run (or a manual fix) can restore it.
if (Test-Path $output) {
    try {
        $acl = Get-Acl $output
        $usersOpen = @($acl.Access | Where-Object {
            $_.AccessControlType -eq 'Allow' -and
            $_.IdentityReference.Value -in @('BUILTIN\Users', 'NT AUTHORITY\Authenticated Users', 'Everyone') -and
            ($_.FileSystemRights.ToString() -match 'Read|FullControl|Modify')
        })
        if ($usersOpen.Count) {
            Gap ("collected data in $output is readable by other local users ({0}) - re-run setup to lock it down" -f (($usersOpen.IdentityReference.Value | Sort-Object -Unique) -join ', '))
        } else {
            Say 'collected data is restricted to you + Administrators'
        }
    } catch {
        Note "could not read the permissions on $output ($($_.Exception.Message))"
    }
}

# ---- has anything been collected / built? ---- #
Write-Host ''
$idx = Join-Path $site 'index.html'
if (Test-Path $idx) {
    Say ("console last built {0}" -f (Get-Item $idx).LastWriteTime)
} else { Note 'console not built yet - double-click "Refresh IT Ops Data" to run the first collection' }
if (Test-Path (Join-Path $output 'tenant-docs')) { Say 'tenant data has been collected at least once' }
else { Note 'no tenant data yet - "Refresh IT Ops Data" collects it (you will sign in)' }

# ---- prerequisites ---- #
Write-Host ''
foreach ($m in 'Microsoft.Graph.Authentication','Microsoft.Graph.Users','Microsoft.Graph.Identity.DirectoryManagement') {
    if (Get-Module -ListAvailable -Name $m) { Say "module: $m" } else { Gap "module missing: $m - re-run setup, or: Install-Module $m -Scope CurrentUser" }
}
$py = $null
foreach ($c in 'python','python3','py') {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { continue }
    # cmd.exe merges the Store stub's stderr "Python was not found..." into
    # plain text, so a health check never prints a red error for a fake python.
    $v = try { (& cmd.exe /d /c "$c --version 2>&1" | Out-String) } catch { '' }
    if ("$v" -match 'Python 3') { $py = $c; break }
}
if ($py) { Say "Python 3 ($py)" } else { Gap 'Python 3 not found - the console pages cannot rebuild without it. Install from python.org (tick "Add python.exe to PATH")' }

# ---- automatic refresh ---- #
# Says in words how this machine keeps itself fresh (setup's last question),
# whether the Task Scheduler job behind that answer is really there, how its
# last run went, and how long the app certificate has left.
Write-Host ''
$ini = @{}
$iniPath = Join-Path (Join-Path $tools 'it-ops-console') 'automatic-refresh.ini'
if (Test-Path $iniPath) {
    $section = ''
    foreach ($raw in Get-Content $iniPath) {
        $line = $raw.Trim()
        if (-not $line -or $line[0] -in ';', '#') { continue }
        if ($line -match '^\[(.+)\]$') { $section = $matches[1].Trim().ToLower(); continue }
        $eq = $line.IndexOf('='); if ($eq -lt 1) { continue }
        $ini["$section.$($line.Substring(0, $eq).Trim().ToLower())"] = $line.Substring($eq + 1).Trim()
    }
}
$mode = if ($ini['schedule.mode']) { $ini['schedule.mode'].ToLower() } else { 'off' }
$time = $ini['schedule.time']; $runAs = $ini['schedule.run_as']
$taskName = if ($ini['schedule.task']) { $ini['schedule.task'] } else { 'IT Ops Console - automatic refresh' }
switch ($mode) {
    'while-signed-in' {
        Say "automatic refresh: every day at $time as $runAs, while signed in"
        if ($ini['signin.keep_signed_in'] -match '^(yes|true|1)$') { Note 'this computer stays signed in to Microsoft Graph (read-only) between refreshes - re-run setup and pick 1 to stop and sign out' }
    }
    'unattended' {
        Say "automatic refresh: every day at $time as SYSTEM, signing in as the registered app (read-only)"
        $exp = $ini['signin.certificate_expires']
        if ($exp) {
            try {
                $left = [int][math]::Floor(([datetime]::ParseExact($exp, 'yyyy-MM-dd', $null) - (Get-Date)).TotalDays)
                if ($left -lt 0) { Gap "the automatic-refresh certificate expired on $exp - re-run setup to make a new one and upload it" }
                elseif ($left -le 30) { Note "the automatic-refresh certificate expires in $left day(s) ($exp) - re-run setup to renew it" }
                else { Say "app certificate valid until $exp" }
            } catch { Note "could not read the certificate expiry date '$exp'" }
        }
        $thumb = $ini['signin.certificate_thumbprint']
        if ($thumb) {
            $c = $null; try { $c = Get-Item "Cert:\LocalMachine\My\$thumb" -ErrorAction Stop } catch { }
            if ($c) { Say 'app certificate is in this computer''s certificate store' }
            else { Gap 'the app certificate is not in this computer''s certificate store - re-run setup and pick 3 to make a new one' }
        }
    }
    default { Note 'automatic refresh is off - "Refresh IT Ops Data" on the desktop is the routine (re-run setup to schedule it)' }
}
if ($mode -ne 'off') {
    $task = $null
    try { $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop } catch { }
    if (-not $task) {
        Gap "the daily refresh task '$taskName' is missing from Task Scheduler - re-run setup and pick the same answer to recreate it"
    } else {
        Say "Task Scheduler job present ($($task.State))"
        try {
            $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
            $code = [int]$info.LastTaskResult
            $verdict = switch ($code) {
                0        { 'finished fine' }
                1        { 'finished with problems - the console overview says what' }
                267009   { 'running right now' }
                267011   { 'has not run yet' }
                default  { "ended with code $code" }
            }
            if ($code -eq 267011) { Note "last automatic run: none yet (next: $($info.NextRunTime))" }
            else { Say "last automatic run: $($info.LastRunTime) - $verdict (next: $($info.NextRunTime))" }
        } catch { Note "could not read the task's run history ($($_.Exception.Message))" }
    }
}
$rsPath = Join-Path $output 'refresh-status.json'
if (Test-Path $rsPath) {
    try {
        $rs = Get-Content $rsPath -Raw | ConvertFrom-Json
        $kind = if ($rs.Scheduled) { 'automatic refresh' } else { 'refresh' }
        if ($rs.SignIn -and $rs.SignIn.Ok -eq $false) {
            Gap "the last $kind ($($rs.GeneratedUtc)) could not sign in: $($rs.SignIn.Detail)"
        } else {
            if ($rs.SignIn -and @($rs.SignIn.Dropped).Count) { Note "the last $kind had to fall back: $(@($rs.SignIn.Dropped)[0])" }
            Say "last $kind ($($rs.GeneratedUtc)): $(if ($rs.Ok) { 'everything ran' } else { 'some steps had problems - see the console overview' })"
        }
    } catch { Note "could not read $rsPath ($($_.Exception.Message))" }
}

Write-Host ''
if ($problems -eq 0) { Write-Host 'Everything looks right. If a refresh still fails, the red text in its window says which step - send check-setup.log and that text to whoever is helping you.' }
else { Write-Host "$problems problem(s) found - the lines marked PROBLEM above say what to do for each." }
try { Stop-Transcript | Out-Null } catch { }
Write-Host ''
if (-not $env:ITOPS_CMD) { $null = Read-Host 'Done - press Enter to close this window' }
