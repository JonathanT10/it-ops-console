# Test: run-all.ps1's sign-in ladder.        pwsh tests/test_run_all_signin.ps1
#
# Drives run-all against stub collectors and a stub Microsoft.Graph.Authentication
# module, so every rung of the ladder can be made to succeed, fail, or hang:
#   1. the registered app + certificate (unattended schedule)
#   2. the saved sign-in, silently (from the scheduled run's child process)
#   3. a sign-in window - with the time limit a scheduled run puts on it
#   4. stop cleanly: skip the Microsoft 365 steps, still build the console,
#      say why in refresh-status.json and on the overview, exit 1
# It also checks the sign-out rule: a person's "stay signed in" choice is
# honoured only for the while-signed-in schedule; app sign-ins always close.
#
# Nothing here touches Microsoft 365: the stub module is what gets imported.
# Runs on Linux (pwsh) or Windows. Two cases wait out the 30-second time limit.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSCommandPath
$repo = Split-Path -Parent $here
$work = Join-Path ([IO.Path]::GetTempPath()) "itops-signin-$([guid]::NewGuid().ToString('n').Substring(0, 6))"
$tools = Join-Path $work 'tools'; $out = Join-Path $work 'out'; $site = Join-Path $work 'site'
$mods = Join-Path $work 'mods'; $log = Join-Path $work 'graph.log'
$cfg = Join-Path $work 'sources.ini'; $ini = Join-Path $work 'automatic-refresh.ini'
$null = New-Item -ItemType Directory -Path $tools, $out, $site, $mods -Force
$python = if (Get-Command python3 -ErrorAction SilentlyContinue) { 'python3' } else { 'python' }

$fails = [System.Collections.Generic.List[string]]::new()
function Check { param([string]$Label, [bool]$Cond)
    Write-Host ("{0} {1}" -f ($(if ($Cond) { 'PASS' } else { 'FAIL' })), $Label)
    if (-not $Cond) { $fails.Add($Label) }
}

# ---- stub collectors: copy the sample feeds into place ---------------------- #
$sample = Join-Path $repo 'sample/feeds'
foreach ($d in 'entra-tenant-docs', 'entra-security-snapshot', 'm365-license-waste-report', 'print-fleet-dashboard') {
    $null = New-Item -ItemType Directory -Path (Join-Path $tools $d) -Force
}
Set-Content (Join-Path $tools 'entra-tenant-docs/Export-EntraTenantDocs.ps1') @"
param([string]`$OutputPath)
`$null = New-Item -ItemType Directory -Path `$OutputPath -Force
Copy-Item '$sample/tenant.json' (Join-Path `$OutputPath 'tenant.json')
Copy-Item '$sample/run-summary.json' (Join-Path `$OutputPath 'run-summary.json')
Write-Host 'stub tenant-docs ok'
"@
Set-Content (Join-Path $tools 'entra-security-snapshot/Get-EntraSecuritySnapshot.ps1') @"
param([string]`$JsonPath, [int]`$StaleDays)
Copy-Item '$sample/security-snapshot.json' `$JsonPath
Write-Host 'stub security ok'
"@
Set-Content (Join-Path $tools 'm365-license-waste-report/Get-LicenseWasteReport.ps1') @"
param([string]`$JsonPath, [int]`$StaleDays, [string]`$PriceList)
Copy-Item '$sample/licensing.json' `$JsonPath
Write-Host 'stub license ok'
"@
Set-Content $cfg @"
[console]
base_path = $out
[sources]
tenant      = tenant-docs/tenant.json
run_summary = tenant-docs/run-summary.json
security    = security-snapshot.json
licensing   = licensing.json
refresh_status = refresh-status.json
history =
fleet =
"@

# ---- stub Microsoft.Graph.Authentication --------------------------------- #
# Behaviour comes from ITOPS_STUB_GRAPH (comma list): app-ok, user-fail, hang.
# An app connect without app-ok throws like a bad certificate does. Every call
# is appended to ITOPS_STUB_LOG so the test can see WHICH rungs ran.
$modDir = Join-Path $mods 'Microsoft.Graph.Authentication'
$null = New-Item -ItemType Directory -Path $modDir -Force
Set-Content (Join-Path $modDir 'Microsoft.Graph.Authentication.psm1') @'
$script:ctx = $null
function Connect-MgGraph {
    param([string[]]$Scopes, [string]$ClientId, [string]$TenantId, [string]$CertificateThumbprint, [switch]$NoWelcome)
    $modes = "$env:ITOPS_STUB_GRAPH" -split ','
    if ($ClientId) {
        Add-Content $env:ITOPS_STUB_LOG "connect app $ClientId $TenantId $CertificateThumbprint"
        if ($modes -contains 'app-ok') {
            $script:ctx = [pscustomobject]@{ Account = $null; ClientId = $ClientId; AuthType = 'AppOnly'; Scopes = @('Directory.Read.All') }
            return
        }
        throw 'AADSTS700027: Client assertion contains an invalid signature. [Reason - The key was not found.]'
    }
    Add-Content $env:ITOPS_STUB_LOG "connect user scopes=$(@($Scopes).Count)"
    if ($modes -contains 'hang') { Start-Sleep -Seconds 120 }
    if ($modes -contains 'user-fail') { throw 'InteractiveBrowserCredential authentication failed: User canceled authentication.' }
    $script:ctx = [pscustomobject]@{ Account = 'someone@example.com'; ClientId = $null; AuthType = 'Delegated'; Scopes = $Scopes }
}
function Get-MgContext { $script:ctx }
function Disconnect-MgGraph { Add-Content $env:ITOPS_STUB_LOG 'disconnect'; $script:ctx = $null }
Export-ModuleMember -Function Connect-MgGraph, Get-MgContext, Disconnect-MgGraph
'@
$env:PSModulePath = $mods + [IO.Path]::PathSeparator + $env:PSModulePath
$env:ITOPS_STUB_LOG = $log

# The certificate store does not exist off Windows, so the child session gets a
# Get-Item that answers Cert: paths from ITOPS_STUB_CERT_NOTAFTER (set = the
# certificate is present with that expiry; unset = not in the store).
$preamble = @'
function Get-Item {
    [CmdletBinding()] param([Parameter(Position=0)][string]$Path, [string]$LiteralPath, [switch]$Force)
    if ($Path -like 'Cert:*') {
        if ($env:ITOPS_STUB_CERT_NOTAFTER) {
            return [pscustomobject]@{
                NotAfter      = [datetime]$env:ITOPS_STUB_CERT_NOTAFTER
                Thumbprint    = ($Path -split '[\\/]')[-1]
                HasPrivateKey = ($env:ITOPS_STUB_CERT_NOKEY -ne '1')
            }
        }
        throw "Cannot find path '$Path' because it does not exist."
    }
    if ($LiteralPath) { return Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force }
    Microsoft.PowerShell.Management\Get-Item -Path $Path -Force:$Force
}
'@

function Run-Case {
    param([string]$Graph, [string]$CertNotAfter, [string]$IniText, [switch]$Desktop, [switch]$NoConnect, [switch]$NoKey, [int]$Timeout = 30, [int]$StepTimeout = 0)
    if (Test-Path $log) { Remove-Item $log }
    if (Test-Path $out) { Remove-Item $out -Recurse -Force }
    if (Test-Path $site) { Remove-Item $site -Recurse -Force }
    $null = New-Item -ItemType Directory -Path $out, $site -Force
    if ($IniText) { Set-Content $ini $IniText } elseif (Test-Path $ini) { Remove-Item $ini }
    # earlier cases leave the price-list starter behind; each case starts clean
    Remove-Item (Join-Path $tools 'm365-license-waste-report/prices.ini') -ErrorAction SilentlyContinue
    $env:ITOPS_STUB_GRAPH = $Graph
    $env:ITOPS_STUB_CERT_NOTAFTER = $CertNotAfter
    $env:ITOPS_STUB_CERT_NOKEY = if ($NoKey) { '1' } else { $null }
    $flags = @()
    if (-not $Desktop) { $flags += '-Scheduled' }
    if ($NoConnect) { $flags += '-NoConnect' }
    if ($StepTimeout) { $flags += @('-StepTimeoutMinutes', "$StepTimeout") }
    $cmd = $preamble + "`n& '$repo/run-all.ps1' -ToolRoot '$tools' -OutputRoot '$out' -SitePath '$site' -ConfigPath '$cfg' -RefreshConfig '$ini' -Python $python -NoStatusPage -SignInTimeoutSeconds $Timeout $($flags -join ' ')"
    $text = (& pwsh -NoProfile -Command $cmd 2>&1 | Out-String)
    $code = $LASTEXITCODE
    $status = $null
    $sp = Join-Path $out 'refresh-status.json'
    if (Test-Path $sp) { $status = Get-Content $sp -Raw | ConvertFrom-Json }
    $entries = @(); if (Test-Path $log) { $entries = @(Get-Content $log) }
    $index = ''; $ip = Join-Path $site 'index.html'
    if (Test-Path $ip) { $index = Get-Content $ip -Raw }
    return @{ Code = $code; Text = $text; Status = $status; Log = $entries; Index = $index }
}
function Count { param($Log, [string]$Like) @($Log | Where-Object { $_ -like $Like }).Count }

$iniKeep = @"
[schedule]
mode = while-signed-in
time = 07:00
run_as = X\sam
[signin]
keep_signed_in = yes
"@
$iniApp = @"
[schedule]
mode = unattended
time = 06:30
run_as = SYSTEM
[signin]
keep_signed_in = no
tenant_id = 11111111-1111-1111-1111-111111111111
client_id = 22222222-2222-2222-2222-222222222222
certificate_thumbprint = ABCDEF0123456789ABCDEF0123456789ABCDEF01
certificate_expires = 2028-08-01
"@
$plus700 = (Get-Date).AddDays(700).ToString('yyyy-MM-dd')
$minus13 = (Get-Date).AddDays(-13).ToString('yyyy-MM-dd')

Write-Host ''
Write-Host '-- 1. no schedule file, scheduled run, saved sign-in works: signs in, signs out'
$r = Run-Case -Graph 'user-ok'
Check 'exit 0' ($r.Code -eq 0)
Check 'mode user' ($r.Status.SignIn.Mode -eq 'user')
Check 'probe + parent = two user connects' ((Count $r.Log 'connect user*') -eq 2)
Check 'no app connect attempted' ((Count $r.Log 'connect app*') -eq 0)
Check 'signed out at the end (no schedule -> old behaviour)' ((Count $r.Log 'disconnect') -eq 1 -and $r.Text -like '*Signed out of Microsoft Graph*')
Check 'status: not keeping signed in' ($r.Status.KeepSignedIn -eq $false)
Check 'status: schedule off' ($r.Status.Schedule.Mode -eq 'off')
Check 'status: final, ok' ($r.Status.Final -eq $true -and $r.Status.Ok -eq $true)
Check 'collectors ran' ((Test-Path (Join-Path $out 'tenant-docs/tenant.json')) -and (Test-Path (Join-Path $out 'licensing.json')))
Check 'no banner on the overview' ($r.Index -notlike '*class="banner*')

Write-Host ''
Write-Host '-- 2. while-signed-in schedule with keep_signed_in: stays signed in'
$r = Run-Case -Graph 'user-ok' -IniText $iniKeep
Check 'exit 0' ($r.Code -eq 0)
Check 'no disconnect' ((Count $r.Log 'disconnect') -eq 0)
Check 'says so in words' ($r.Text -like '*Staying signed in to Microsoft Graph for the next automatic refresh*')
Check 'status: keeping signed in' ($r.Status.KeepSignedIn -eq $true)
Check 'status: schedule while-signed-in 07:00' ($r.Status.Schedule.Mode -eq 'while-signed-in' -and $r.Status.Schedule.Time -eq '07:00')
Check 'footer note on every page' ($r.Index -like '*refresh-note">Automatic refresh: every day at 07:00 while you&#x27;re signed in. This computer stays signed in*')

Write-Host ''
Write-Host '-- 3. while-signed-in, the sign-in window is never finished: time limit, run continues, exit 1'
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = Run-Case -Graph 'hang' -IniText $iniKeep -Timeout 30
$sw.Stop()
Check 'exit 1' ($r.Code -eq 1)
Check 'gave up after the limit, not much later' ($sw.Elapsed.TotalSeconds -ge 30 -and $sw.Elapsed.TotalSeconds -lt 90)
Check 'sign-in not ok' ($r.Status.SignIn.Ok -eq $false -and $r.Status.SignIn.Mode -eq 'none')
Check 'dropped rung says nobody finished the window' (@($r.Status.SignIn.Dropped)[0] -like 'Nobody finished the Microsoft sign-in window within 1 minute*')
Check 'detail tells the person what to do' ($r.Status.SignIn.Detail -like '*Double-click "Refresh IT Ops Data"*')
Check 'only the probe connected (parent never tried)' ((Count $r.Log 'connect user*') -eq 1)
Check 'no disconnect (nothing to close)' ((Count $r.Log 'disconnect') -eq 0)
$steps = @{}; foreach ($s in $r.Status.Steps) { $steps[$s.Step] = $s }
Check 'sign-in recorded FAILED' ($steps['sign-in'].Status -eq 'FAILED')
Check 'Microsoft 365 steps skipped, reason given' ($steps['entra-tenant-docs'].Status -eq 'skipped' -and $steps['entra-tenant-docs'].Detail -eq 'not signed in' -and $steps['m365-license-waste-report'].Status -eq 'skipped')
Check 'console still built' ($steps['console build'].Status -eq 'ok' -and $r.Index.Length -gt 0)
Check 'overview banner: could not sign in, what to do' ($r.Index -like '*class="banner warning"*' -and $r.Index -like '*couldn&#x27;t sign in*' -and $r.Index -like '*Double-click &quot;Refresh IT Ops Data&quot;*')
Check 'no collector output written' (-not (Test-Path (Join-Path $out 'tenant-docs')))
Check 'no price-list starter written without a license run' (-not (Test-Path (Join-Path $tools 'm365-license-waste-report/prices.ini')))

Write-Host ''
Write-Host '-- 4. unattended schedule, certificate present and valid, app sign-in works'
$r = Run-Case -Graph 'app-ok' -IniText $iniApp -CertNotAfter $plus700
Check 'exit 0' ($r.Code -eq 0)
Check 'mode app' ($r.Status.SignIn.Mode -eq 'app')
# Each collector runs in its own child process now, and a Graph session does
# not cross a process boundary - so the app sign-in happens in the parent AND
# once inside each collector. What must be true is that they are all the SAME
# app: no child quietly falling back to asking a person instead.
$appConnects = @($r.Log | Where-Object { $_ -like 'connect app*' })
Check 'every app sign-in uses that tenant, client and thumbprint' (
    $appConnects.Count -ge 1 -and
    @($appConnects | Where-Object { $_ -ne 'connect app 22222222-2222-2222-2222-222222222222 11111111-1111-1111-1111-111111111111 ABCDEF0123456789ABCDEF0123456789ABCDEF01' }).Count -eq 0)
Check 'each collector signed in as the app too, not as a person' (
    $appConnects.Count -eq 4 -and (Count $r.Log 'connect user*') -eq 0)
Check 'no user sign-in attempted' ((Count $r.Log 'connect user*') -eq 0)
Check 'app sign-in always closed' ((Count $r.Log 'disconnect') -eq 1)
Check 'nothing dropped' (@($r.Status.SignIn.Dropped).Count -eq 0)
Check 'certificate days left from the store' ($r.Status.Certificate.Present -eq $true -and $r.Status.Certificate.DaysLeft -ge 698 -and $r.Status.Certificate.DaysLeft -le 700)
Check 'footer note names the app route' ($r.Index -like '*as the registered app, whether or not anyone is signed in*')
Check 'no banner' ($r.Index -notlike '*class="banner*')

Write-Host ''
Write-Host '-- 5. unattended, certificate EXPIRED, saved sign-in still works: falls through AND says so'
$r = Run-Case -Graph 'user-ok' -IniText $iniApp -CertNotAfter $minus13
Check 'exit 0 (the run itself worked)' ($r.Code -eq 0)
Check 'mode user' ($r.Status.SignIn.Mode -eq 'user')
Check 'app never attempted with an expired certificate' ((Count $r.Log 'connect app*') -eq 0)
Check 'dropped rung reported' (@($r.Status.SignIn.Dropped)[0] -like "The automatic-refresh certificate expired on $minus13*")
Check 'warned on screen too' ($r.Text -like '*certificate expired on*')
Check 'certificate days left negative' ($r.Status.Certificate.DaysLeft -lt 0)
Check 'signed out (keep applies to while-signed-in only)' ((Count $r.Log 'disconnect') -eq 1)
Check 'overview: fall-back banner AND expired-certificate banner' ($r.Index -like '*couldn&#x27;t use its usual sign-in*' -and $r.Index -like '*class="banner serious"*' -and $r.Index -like "*expired on $minus13*")

Write-Host ''
Write-Host '-- 6. unattended, certificate valid, app sign-in rejected by Microsoft: reported, falls through'
$r = Run-Case -Graph 'user-ok' -IniText $iniApp -CertNotAfter $plus700
Check 'exit 0' ($r.Code -eq 0)
Check 'app attempted once' ((Count $r.Log 'connect app*') -eq 1)
Check 'plain words first, the code still there to search for' (@($r.Status.SignIn.Dropped)[0] -like 'Signing in as the registered app failed: Microsoft 365 does not recognise this certificate any more.*' -and @($r.Status.SignIn.Dropped)[0] -like '*AADSTS700027*')
Check 'mode user' ($r.Status.SignIn.Mode -eq 'user')
Check 'overview: fall-back banner' ($r.Index -like '*couldn&#x27;t use its usual sign-in: Signing in as the registered app failed: Microsoft 365 does not recognise this certificate*')

Write-Host ''
Write-Host '-- 7. unattended, certificate NOT in the store, nobody finishes the window: two drops, stop cleanly'
$r = Run-Case -Graph 'hang' -IniText $iniApp -Timeout 30
Check 'exit 1' ($r.Code -eq 1)
Check 'first drop: certificate missing' (@($r.Status.SignIn.Dropped)[0] -like "*not in this computer's certificate store*")
Check 'second drop: window not finished' (@($r.Status.SignIn.Dropped)[1] -like 'Nobody finished*')
Check 'mode none' ($r.Status.SignIn.Mode -eq 'none')
Check 'certificate expiry from the ini when the store has none' ($r.Status.Certificate.Present -eq $false -and $r.Status.Certificate.Expires -eq '2028-08-01')

Write-Host ''
Write-Host '-- 8. desktop click on a while-signed-in machine: in-process sign-in, stays signed in'
$r = Run-Case -Graph 'user-ok' -IniText $iniKeep -Desktop
Check 'exit 0' ($r.Code -eq 0)
Check 'one in-process connect, no probe' ((Count $r.Log 'connect user*') -eq 1)
Check 'stays signed in' ((Count $r.Log 'disconnect') -eq 0 -and $r.Text -like '*Staying signed in*')
Check 'status: not a scheduled run' ($r.Status.Scheduled -eq $false)

Write-Host ''
Write-Host '-- 9. desktop click, no schedule, sign-in cancelled: recorded, console built, no banner'
$r = Run-Case -Graph 'user-fail' -Desktop
Check 'exit 1' ($r.Code -eq 1)
Check 'reason recorded' (@($r.Status.SignIn.Dropped)[0] -like 'The sign-in did not complete: InteractiveBrowserCredential*')
Check 'console built' ($r.Index.Length -gt 0)
Check 'no banner for an unscheduled run' ($r.Index -notlike '*class="banner*')
Check 'plain words in the summary' ($r.Text -like '*In plain words:*' -and $r.Text -like '*sign-in: The sign-in did not complete*')

Write-Host ''
Write-Host '-- 9b. THE BUG: desktop click on an UNATTENDED machine'
# The certificate lives in the computer's store, where only SYSTEM and
# Administrators can open its private key. A person clicking Refresh at their
# desk could see it, tried to use it, failed on "Keyset does not exist", and
# got a browser sign-in window - every single time. A manual run must not
# reach for that certificate at all, and must not sign the person out at the
# end, or the next click asks them to pick their account again.
$r = Run-Case -Graph 'user-ok' -IniText $iniApp -CertNotAfter $plus700 -Desktop
Check '9b exit 0' ($r.Code -eq 0)
Check '9b the certificate is never touched by a manual run' ((Count $r.Log 'connect app*') -eq 0)
Check '9b nothing is reported as a failed rung' (@($r.Status.SignIn.Dropped).Count -eq 0)
Check '9b it says whose sign-in this is, in plain words' ($r.Text -like '*for the scheduled refresh, which runs as this computer*')
Check '9b signed in as the person' ($r.Status.SignIn.Mode -eq 'user' -and (Count $r.Log 'connect user*') -eq 1)
Check '9b STAYS signed in, so the next click does not ask again' ((Count $r.Log 'disconnect') -eq 0)
Check '9b and says so' ($r.Text -like '*Staying signed in to Microsoft 365*')
Check '9b no banner - nothing went wrong' ($r.Index -notlike '*class="banner*')

Write-Host ''
Write-Host '-- 9c. the same machine on its 07:00 schedule still uses the certificate'
$r = Run-Case -Graph 'app-ok' -IniText $iniApp -CertNotAfter $plus700
Check '9c mode app' ($r.Status.SignIn.Mode -eq 'app')
Check '9c the certificate IS used when nobody is there' ((Count $r.Log 'connect app*') -ge 1 -and (Count $r.Log 'connect user*') -eq 0)
Check '9c an app sign-in is always closed' ((Count $r.Log 'disconnect') -eq 1)

Write-Host ''
Write-Host '-- 9d. scheduled run, certificate present but its key cannot be opened'
$r = Run-Case -Graph 'user-ok' -IniText $iniApp -CertNotAfter $plus700 -NoKey
Check '9d the app rung is refused before it is attempted' ((Count $r.Log 'connect app*') -eq 0)
Check '9d and the reason is a sentence, not a crypto error' (@($r.Status.SignIn.Dropped)[0] -like '*no private key*' -and @($r.Status.SignIn.Dropped)[0] -like '*Re-run setup as an administrator*')

Write-Host ''
Write-Host '-- 9e. THE HANG: a collector that blocks is stopped, and the refresh finishes'
# This is the one the time budget inside the collector could never fix. These
# steps block on a Microsoft sign-in window opening behind the window you are
# watching; a process cannot interrupt itself, so the only thing that can end
# it is the parent killing it. Here the stub sleeps far longer than the run
# allows, which is indistinguishable from that.
$wedge = Join-Path $tools 'entra-security-snapshot/Get-EntraSecuritySnapshot.ps1'
$wedgeKeep = Get-Content $wedge -Raw
Set-Content $wedge @'
param([string]$JsonPath, [int]$StaleDays, [int]$TimeBudgetMinutes)
Write-Host 'stub security: about to block'
Start-Sleep -Seconds 600
Write-Host 'never reached'
'@
try {
    $t0 = Get-Date
    $r = Run-Case -Graph 'user-ok' -IniText $iniKeep -Desktop -StepTimeout 1
    $took = ((Get-Date) - $t0).TotalSeconds
    Check '9e it did not sit there for ever' ($took -lt 240)
    Check '9e the wedged step is recorded as failed' (@($r.Status.Steps | Where-Object { $_.Step -eq 'entra-security-snapshot' -and $_.Status -eq 'FAILED' }).Count -eq 1)
    Check '9e in words a person can act on' ($r.Text -like '*took longer than 1 minutes and was stopped*' -and $r.Text -like '*sign-in window waiting behind another window*')
    Check '9e what it managed to say before blocking is still shown' ($r.Text -like '*stub security: about to block*')
    Check '9e the OTHER collectors still ran' (
        @($r.Status.Steps | Where-Object { $_.Step -eq 'entra-tenant-docs' -and $_.Status -eq 'ok' }).Count -eq 1 -and
        @($r.Status.Steps | Where-Object { $_.Step -eq 'm365-license-waste-report' -and $_.Status -eq 'ok' }).Count -eq 1)
    Check '9e the console was still built' (
        @($r.Status.Steps | Where-Object { $_.Step -eq 'console build' -and $_.Status -eq 'ok' }).Count -eq 1 -and $r.Index.Length -gt 0)
    Check '9e and the run says it was not clean' ($r.Code -eq 1 -and $r.Status.Final -eq $true)
} finally {
    Set-Content $wedge $wedgeKeep
}

Write-Host ''
Write-Host '-- 9f. a collector that WORKED is never reported as failed'
# Shipped broken once: the child's result was read from Process.ExitCode, which
# on Windows PowerShell stays null until WaitForExit has cached the handle - and
# null is not 0, so every collector came back FAILED with the detail "exit code "
# while all three had in fact written their output perfectly. Two claims here:
# a step that succeeded says so, and no failure detail is ever blank.
$r = Run-Case -Graph 'user-ok' -IniText $iniKeep -Desktop
$okSteps = @($r.Status.Steps | Where-Object { $_.Status -eq 'ok' } | ForEach-Object { $_.Step })
Check '9f every collector that ran is reported ok' (
    'entra-tenant-docs' -in $okSteps -and 'entra-security-snapshot' -in $okSteps -and
    'm365-license-waste-report' -in $okSteps)
Check '9f none of them is reported failed' (
    @($r.Status.Steps | Where-Object { $_.Status -eq 'FAILED' }).Count -eq 0)
Check '9f and the run reads as clean' ($r.Code -eq 0 -and $r.Status.Ok -eq $true)
Check '9f nothing says "exit code" with nothing after it' ($r.Text -notmatch 'exit code\s*$' -and $r.Text -notlike '*ERROR: exit code *')

Write-Host ''
Write-Host '-- 9g. a collector that FAILS says why, and never says it blankly'
$boom = Join-Path $tools 'm365-license-waste-report/Get-LicenseWasteReport.ps1'
$boomKeep = Get-Content $boom -Raw
Set-Content $boom @'
param([string]$JsonPath, [int]$StaleDays, [string]$PriceList)
Write-Host 'stub license: about to fail'
throw 'the licence report could not read the tenant'
'@
try {
    $r = Run-Case -Graph 'user-ok' -IniText $iniKeep -Desktop
    $lic = @($r.Status.Steps | Where-Object { $_.Step -eq 'm365-license-waste-report' })[0]
    Check '9g the failing step is reported failed' ($lic.Status -eq 'FAILED')
    Check '9g with a reason, not a blank one' ($lic.Detail -and $lic.Detail.Trim().Length -gt 0)
    Check '9g the reason is the collector''s own words' ($r.Text -like '*could not read the tenant*')
    Check '9g the OTHER collectors are still ok' (
        @($r.Status.Steps | Where-Object { $_.Step -eq 'entra-tenant-docs' -and $_.Status -eq 'ok' }).Count -eq 1)
    Check '9g the console was still built' ($r.Index.Length -gt 0)
} finally {
    Set-Content $boom $boomKeep
}

Write-Host ''
Write-Host '-- 10. -NoConnect: the session you opened yourself'
$r = Run-Case -Graph 'user-ok' -NoConnect
Check 'exit 0' ($r.Code -eq 0)
Check 'mode existing' ($r.Status.SignIn.Mode -eq 'existing')
Check 'no Graph calls at all' ($r.Log.Count -eq 0)
$steps = @{}; foreach ($s in $r.Status.Steps) { $steps[$s.Step] = $s }
Check 'alerts step skipped in words when no channel is configured' ($steps['alerts'].Status -eq 'skipped' -and $steps['alerts'].Detail -like 'no Teams or email channel*')
Check 'alerts.json written by the build' (Test-Path (Join-Path $out 'alerts.json'))
Check 'Alerts page built' (Test-Path (Join-Path $site 'alerts.html'))

Write-Host ''
Write-Host '-- 11. alerts: a channel configured -> notify.py runs against a local fake webhook'
$hookLog = Join-Path $work 'hook.log'
$hookPy = Join-Path $work 'hook.py'
Set-Content $hookPy @"
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', '0')); body = self.rfile.read(n)
        open(r'$hookLog', 'a', encoding='utf-8').write(body.decode('utf-8') + '\n')
        self.send_response(200); self.end_headers(); self.wfile.write(b'1')
    def log_message(self, *a): pass
srv = http.server.HTTPServer(('127.0.0.1', 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
"@
$hookProc = Start-Process -FilePath $python -ArgumentList @($hookPy) -PassThru -RedirectStandardOutput (Join-Path $work 'hook.port') -NoNewWindow
Start-Sleep -Seconds 2
$port = (Get-Content (Join-Path $work 'hook.port') | Select-Object -First 1).Trim()
$alertsIni = Join-Path $work 'alerts.ini'
Set-Content $alertsIni "[send]`nwhen = changes`n[teams]`nwebhook = http://127.0.0.1:$port/hook`n"
try {
    # Run-Case passes -RefreshConfig; alerts.ini is picked up from -AlertsConfig here by running the command directly.
    Remove-Item $out -Recurse -Force; Remove-Item $site -Recurse -Force; $null = New-Item -ItemType Directory -Path $out, $site -Force
    if (Test-Path (Join-Path $out 'alerts-state.json')) { Remove-Item (Join-Path $out 'alerts-state.json') }
    Remove-Item $ini -ErrorAction SilentlyContinue
    $env:ITOPS_STUB_GRAPH = 'user-ok'; $env:ITOPS_STUB_CERT_NOTAFTER = ''
    $cmd = $preamble + "`n& '$repo/run-all.ps1' -ToolRoot '$tools' -OutputRoot '$out' -SitePath '$site' -ConfigPath '$cfg' -RefreshConfig '$ini' -AlertsConfig '$alertsIni' -Python $python -NoStatusPage -Scheduled -SignInTimeoutSeconds 30"
    $text = (& pwsh -NoProfile -Command $cmd 2>&1 | Out-String); $code = $LASTEXITCODE
    $status = Get-Content (Join-Path $out 'refresh-status.json') -Raw | ConvertFrom-Json
    $steps = @{}; foreach ($s in $status.Steps) { $steps[$s.Step] = $s }
    Check 'exit 0 with alerts sent' ($code -eq 0 -and $steps['alerts'].Status -eq 'ok')
    Check 'one card posted to the webhook' ((Test-Path $hookLog) -and @(Get-Content $hookLog).Count -eq 1)
    $card = (Get-Content $hookLog -Raw | ConvertFrom-Json)
    $cardText = ($card.attachments[0].content.body | ForEach-Object { $_.text }) -join "`n"
    Check 'card announces new alerts from the sample data' ($cardText -like 'IT Ops Console: * new*' -and $cardText -like '*Admin without MFA*')
    Check 'alerts-state.json written' (Test-Path (Join-Path $out 'alerts-state.json'))
    Check 'plain words on screen' ($text -like '*Sent to Teams.*')
    # second run: nothing changed -> no post, step still ok
    $text = (& pwsh -NoProfile -Command $cmd 2>&1 | Out-String); $code = $LASTEXITCODE
    Check 'second run: quiet, still exit 0' ($code -eq 0 -and @(Get-Content $hookLog).Count -eq 1 -and $text -like '*No alert sent*')
    # webhook gone: the step fails in plain words, the console is still built, exit 1
    Stop-Process -Id $hookProc.Id -Force; Start-Sleep -Seconds 1; $hookProc = $null
    Copy-Item (Join-Path $repo 'sample/feeds/security-snapshot.json') (Join-Path $out 'security-snapshot.json')   # keep data current
    Remove-Item (Join-Path $out 'alerts-state.json')
    $text = (& pwsh -NoProfile -Command $cmd 2>&1 | Out-String); $code = $LASTEXITCODE
    $status = Get-Content (Join-Path $out 'refresh-status.json') -Raw | ConvertFrom-Json
    $steps = @{}; foreach ($s in $status.Steps) { $steps[$s.Step] = $s }
    Check 'unreachable webhook: alerts step FAILED, console built, exit 1' ($code -eq 1 -and $steps['alerts'].Status -eq 'FAILED' -and $steps['console build'].Status -eq 'ok')
    Check 'plain words name the Workflows URL' ($text -like '*alerts: The alert message could not be sent - check the Teams Workflows URL*')
} finally {
    if ($hookProc) { Stop-Process -Id $hookProc.Id -Force -ErrorAction SilentlyContinue }
}

Write-Host ''
Write-Host '-- 12. -NoStatusPage still WRITES the live page (the console button lands on it)'
# -NoStatusPage means "do not open a browser window", not "write no progress".
# The console's own Refresh button sends you to this page - a run that wrote
# nothing would leave it saying "waiting for the first step" for ever, or show
# the previous run's "All done". Every case above ran with -NoStatusPage, so
# the last one's site is the evidence.
$pjs = Join-Path $site 'progress.js'
Check 'progress.js written even with -NoStatusPage' (Test-Path $pjs)
$pj = if (Test-Path $pjs) { Get-Content $pjs -Raw } else { '' }
Check 'and it is the finished run, not an empty file' ($pj -like '*window.PROGRESS*' -and $pj -like '*"done"*')
Check 'the live page itself is beside it' (Test-Path (Join-Path $site 'status.html'))
$sp = if (Test-Path (Join-Path $site 'status.html')) { Get-Content (Join-Path $site 'status.html') -Raw } else { '' }
Check 'and it is the current template' ($sp -like "*progress.js?v=*")

Write-Host ''
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$env:ITOPS_STUB_GRAPH = $null; $env:ITOPS_STUB_CERT_NOTAFTER = $null; $env:ITOPS_STUB_LOG = $null
if ($fails.Count) { Write-Host "RESULT: $($fails.Count) FAILURES"; $fails | ForEach-Object { Write-Host "  - $_" }; exit 1 }
Write-Host 'RESULT: ALL PASS'
exit 0
