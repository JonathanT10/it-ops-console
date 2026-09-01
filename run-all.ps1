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
    print-fleet-dashboard config.ini. Only needed if you want this wrapper to
    poll the printers too; most sites let the collector run on its own timer
    and leave this unset.

.PARAMETER FleetDb
    The fleet database the collector appends to. Must match sources.ini.

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
    [switch]$SkipTenantDocs,
    [switch]$SkipSecurity,
    [switch]$SkipLicensing,
    [switch]$SkipFleet,
    [switch]$NoConnect
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

function Invoke-Step {
    <# Runs one collector, times it, and turns a failure into a recorded result
       rather than an aborted run. #>
    param(
        [string]$Name,
        [string]$ScriptPath,
        [hashtable]$Arguments,
        [switch]$Skip
    )
    if ($Skip) { Add-Result $Name 'skipped'; return }
    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        Add-Result $Name 'missing' "not found: $ScriptPath"
        Write-Warning "$Name - script not found at $ScriptPath"
        return
    }
    Write-Host ''
    Write-Host "--- $Name ".PadRight(72, '-')
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        & $ScriptPath @Arguments | Out-Null
        $sw.Stop()
        Add-Result $Name 'ok' '' $sw.Elapsed.TotalSeconds
    } catch {
        $sw.Stop()
        Add-Result $Name 'FAILED' $_.Exception.Message $sw.Elapsed.TotalSeconds
        Write-Warning "$Name failed: $($_.Exception.Message)"
    }
}

function Invoke-Native {
    param([string]$Name, [string]$Exe, [string[]]$NativeArgs, [string]$WorkDir, [switch]$Skip)
    if ($Skip) { Add-Result $Name 'skipped'; return }
    Write-Host ''
    Write-Host "--- $Name ".PadRight(72, '-')
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $prev = Get-Location
    try {
        if ($WorkDir) { Set-Location -LiteralPath $WorkDir }
        & $Exe @NativeArgs
        $code = $LASTEXITCODE
        $sw.Stop()
        if ($code -ne 0) {
            Add-Result $Name 'FAILED' "exit code $code" $sw.Elapsed.TotalSeconds
            Write-Warning "$Name exited with code $code"
        } else {
            Add-Result $Name 'ok' '' $sw.Elapsed.TotalSeconds
        }
    } catch {
        $sw.Stop()
        Add-Result $Name 'FAILED' $_.Exception.Message $sw.Elapsed.TotalSeconds
        Write-Warning "$Name failed: $($_.Exception.Message)"
    } finally {
        Set-Location $prev
    }
}

# --------------------------------------------------------------------------- #
# Sign in once for all three Entra tools
# --------------------------------------------------------------------------- #

$needGraph = -not ($SkipTenantDocs -and $SkipSecurity -and $SkipLicensing)

if ($needGraph -and -not $NoConnect) {
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
        Connect-MgGraph -Scopes $scopes -NoWelcome
    } else {
        Write-Host "Already connected as $($ctx.Account) - reusing that session."
        $missing = @($scopes | Where-Object { $_ -notin $ctx.Scopes })
        if ($missing.Count) {
            Write-Warning ("Current session is missing: {0}" -f ($missing -join ', '))
            Write-Warning 'Some sections may come back empty. Disconnect-MgGraph and re-run to get all scopes.'
        }
    }
}

$null = New-Item -ItemType Directory -Path $OutputRoot -Force

# --------------------------------------------------------------------------- #
# Collectors
# --------------------------------------------------------------------------- #

Invoke-Step -Name 'entra-tenant-docs' -Skip:$SkipTenantDocs `
    -ScriptPath (Join-Path $ToolRoot 'entra-tenant-docs\Export-EntraTenantDocs.ps1') `
    -Arguments @{ OutputPath = (Join-Path $OutputRoot 'tenant-docs') }

Invoke-Step -Name 'entra-security-snapshot' -Skip:$SkipSecurity `
    -ScriptPath (Join-Path $ToolRoot 'entra-security-snapshot\Get-EntraSecuritySnapshot.ps1') `
    -Arguments @{
        JsonPath  = (Join-Path $OutputRoot 'security-snapshot.json')
        StaleDays = $StaleDays
    }

Invoke-Step -Name 'm365-license-waste-report' -Skip:$SkipLicensing `
    -ScriptPath (Join-Path $ToolRoot 'm365-license-waste-report\Get-LicenseWasteReport.ps1') `
    -Arguments @{
        JsonPath  = (Join-Path $OutputRoot 'licensing.json')
        StaleDays = $StaleDays
    }

# The printer collector usually runs on its own short timer; only poll here if
# the caller actually pointed us at a config.
$fleetRepo = Join-Path $ToolRoot 'print-fleet-dashboard'
$doFleet = -not $SkipFleet -and $FleetConfig -and $FleetDb
if ($doFleet) {
    Invoke-Native -Name 'print-fleet-collector' -Exe $Python -WorkDir $fleetRepo `
        -NativeArgs @('collector.py', '--config', $FleetConfig, '--db', $FleetDb)
} elseif ($SkipFleet) {
    Add-Result 'print-fleet-collector' 'skipped'
} else {
    Add-Result 'print-fleet-collector' 'skipped' 'no -FleetConfig/-FleetDb given'
}

# --------------------------------------------------------------------------- #
# Build the console
# --------------------------------------------------------------------------- #

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Console config not found: $ConfigPath (copy sources.example.ini to sources.ini and edit the paths)"
}

Invoke-Native -Name 'console build' -Exe $Python -WorkDir $PSScriptRoot `
    -NativeArgs @('build.py', '--config', $ConfigPath, '--out', $SitePath)

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

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
