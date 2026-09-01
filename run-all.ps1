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
    Do not open the live progress page. Use for scheduled/headless runs; the
    console itself is built either way.

.PARAMETER NoConnect
    Skip the up-front Connect-MgGraph - use when you are already connected, or
    when running app-only with a certificate you connect with yourself.

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
    [switch]$NoStatusPage
)

$ErrorActionPreference = 'Stop'
# Deliberately NO Set-StrictMode here. Strict mode inherits into every script
# this wrapper invokes with '&', and it changes their semantics: the collectors
# read optional Graph response properties (a missing '@odata.nextLink' is how
# paging ENDS), which strict mode turns from "null" into a terminating error.
# A wrapper must not alter the behaviour of the things it wraps.

if (-not $ToolRoot)   { $ToolRoot   = Split-Path -Parent $PSScriptRoot }
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot 'sources.ini' }

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

function Invoke-Step {
    <# Runs one collector, times it, and turns a failure into a recorded result
       rather than an aborted run. #>
    param(
        [string]$Name,
        [string]$ScriptPath,
        [hashtable]$Arguments,
        [switch]$Skip,
        [string]$StepKey
    )
    if ($Skip) { Add-Result $Name 'skipped'; Set-StepState $StepKey 'skipped'; return }
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
    try {
        # *>&1 turns the collector's Write-Host stage lines into a stream this
        # pipeline can see, so the progress page shows them AS THEY HAPPEN.
        # Data objects (a report's return value) are dropped, not displayed.
        & $ScriptPath @Arguments *>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.InformationRecord] -or
                $_ -is [System.Management.Automation.WarningRecord] -or
                $_ -is [string]) {
                $line = "$_".TrimEnd()
                if ($line) { Write-Host $line; Add-StatusLine $StepKey $line }
            }
        }
        $sw.Stop()
        Add-Result $Name 'ok' '' $sw.Elapsed.TotalSeconds
        Set-StepState $StepKey 'ok' -Seconds $sw.Elapsed.TotalSeconds
    } catch {
        $sw.Stop()
        Add-Result $Name 'FAILED' $_.Exception.Message $sw.Elapsed.TotalSeconds
        Add-StatusLine $StepKey "ERROR: $($_.Exception.Message)"
        Set-StepState $StepKey 'failed' -Seconds $sw.Elapsed.TotalSeconds -Plain (Get-PlainWords $Name $_.Exception.Message)
        Write-Warning "$Name failed: $($_.Exception.Message)"
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

# --------------------------------------------------------------------------- #
# Sign in once for all three Entra tools
# --------------------------------------------------------------------------- #

# Start the progress page first, so the person has something to watch while
# the sign-in window is up - and so a failure anywhere still reports somewhere.
if (-not $NoStatusPage) {
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
        if ($script:StatusEnabled) {
            try { Start-Process (Join-Path $SitePath 'status.html') | Out-Null }
            catch { Write-Host "Progress page: $(Join-Path $SitePath 'status.html') (open it in a browser to watch)" }
        }
    }
}

$needGraph = -not ($SkipTenantDocs -and $SkipSecurity -and $SkipLicensing)

if (-not ($needGraph -and -not $NoConnect)) {
    Set-StepState 'signin' 'skipped' -Detail 'Already signed in, or nothing to collect'
}
$script:GraphConnectedByUs = $false
if ($needGraph -and -not $NoConnect) {
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
        throw "Microsoft.Graph.Authentication is not installed. Run: Install-Module Microsoft.Graph -Scope CurrentUser"
    }
    Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
    $ctx = Get-MgContext
    if (-not $ctx) {
        Write-Host 'Connecting to Microsoft Graph (read-only scopes)...'
        Add-StatusLine 'signin' 'A Microsoft sign-in window is open - finish signing in there.'
        Connect-MgGraph -Scopes $scopes -NoWelcome
        $script:GraphConnectedByUs = $true
    } else {
        Write-Host "Already connected as $($ctx.Account) - reusing that session."
        $missing = @($scopes | Where-Object { $_ -notin $ctx.Scopes })
        if ($missing.Count) {
            Write-Warning ("Current session is missing: {0}" -f ($missing -join ', '))
            Write-Warning 'Some sections may come back empty. Disconnect-MgGraph and re-run to get all scopes.'
        }
    }
    Set-StepState 'signin' 'ok' -Detail 'Signed in with read-only access'
}

$null = New-Item -ItemType Directory -Path $OutputRoot -Force

# --------------------------------------------------------------------------- #
# Collectors
# --------------------------------------------------------------------------- #

Invoke-Step -Name 'entra-tenant-docs' -Skip:$SkipTenantDocs -StepKey 'tenant' `
    -ScriptPath (Join-Path $ToolRoot 'entra-tenant-docs\Export-EntraTenantDocs.ps1') `
    -Arguments @{ OutputPath = (Join-Path $OutputRoot 'tenant-docs') }
Update-StatsFromOutputs

Invoke-Step -Name 'entra-security-snapshot' -Skip:$SkipSecurity -StepKey 'security' `
    -ScriptPath (Join-Path $ToolRoot 'entra-security-snapshot\Get-EntraSecuritySnapshot.ps1') `
    -Arguments @{
        JsonPath  = (Join-Path $OutputRoot 'security-snapshot.json')
        StaleDays = $StaleDays
    }
Update-StatsFromOutputs

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

Invoke-Step -Name 'm365-license-waste-report' -Skip:$SkipLicensing -StepKey 'licensing' `
    -ScriptPath (Join-Path $licenseRepo 'Get-LicenseWasteReport.ps1') `
    -Arguments $licenseArgs
Update-StatsFromOutputs

# First-run convenience: if the license report ran and there is still no
# prices.ini, list the tenant's real SKUs (blank values) so pricing is a
# fill-in exercise. Never overwrite a file the user has touched.
if (-not $SkipLicensing -and -not (Test-Path $pricesPath)) {
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

Invoke-Native -Name 'console build' -StepKey 'build' -Exe $Python -WorkDir $PSScriptRoot `
    -NativeArgs @('build.py', '--config', $ConfigPath, '--out', $SitePath)

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

# Sign out of the Graph session we opened, so a shared machine doesn't keep a
# live token cached after the refresh. Only if WE connected - a session you
# started yourself (e.g. app-only with a certificate) is left for you to manage.
if ($script:GraphConnectedByUs) {
    try { Disconnect-MgGraph -ErrorAction Stop | Out-Null; Write-Host 'Signed out of Microsoft Graph.' }
    catch { Write-Warning "Could not sign out of Graph cleanly ($($_.Exception.Message))." }
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
