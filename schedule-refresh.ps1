<#
.SYNOPSIS
    Decide how this computer keeps the IT Ops Console fresh, and set it up.

.DESCRIPTION
    Setup runs this as its last step; it can also be run again on its own to
    change the answer. One question, three answers:

      1. I'll click "Refresh IT Ops Data" myself        (nothing scheduled)
      2. Refresh every day while I'm signed in           (Task Scheduler, as you)
      3. Refresh every day even when nobody is signed in (Task Scheduler, as
         SYSTEM, signing in as an app you register once with a certificate)

    Answer 2 keeps this computer signed in to Microsoft Graph (read-only)
    between refreshes, so the daily run does not ask you to sign in each time.
    That saved sign-in is encrypted to your Windows account (other local users
    and a copied disk cannot read it), the console footer and check-setup say
    in words that it is kept, and picking answer 1 later signs out on the spot.

    Answer 3 needs a Global Administrator once: this script makes a certificate
    whose private key never leaves this computer, exports the public half to
    your desktop, and prints the exact clicks to register the app and upload
    it. It never creates anything in your tenant itself - that one write stays
    a human step, on purpose, because everything else in this suite is
    read-only. It then proves the sign-in works before scheduling anything.

    What it writes: automatic-refresh.ini beside run-all.ps1 (no secrets - the
    schedule, the two IDs, a certificate thumbprint), and one Task Scheduler
    job named "IT Ops Console - automatic refresh".

.PARAMETER Root
    The install folder (default C:\IT-Ops).

.PARAMETER Mode
    off | while-signed-in | unattended. Omit to be asked.

.PARAMETER Time
    Time of day for the daily refresh, 24-hour HH:mm (default 07:00). Omit to
    be asked.

.PARAMETER Python
    The Python command setup found (used to record a full path for the SYSTEM
    task, which has no user PATH).

.PARAMETER TenantId
.PARAMETER ClientId
    For unattended mode: the Directory (tenant) ID and Application (client) ID
    from the app registration. Omit to be asked.

.PARAMETER NoPrompt
    Never wait for a keypress; take every value from the parameters. For
    scripted use and tests.

.EXAMPLE
    .\schedule-refresh.ps1
    .\schedule-refresh.ps1 -Mode off
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\IT-Ops',
    [ValidateSet('', 'off', 'while-signed-in', 'unattended')][string]$Mode = '',
    [string]$Time = '',
    [string]$Python = '',
    [string]$TenantId = '',
    [string]$ClientId = '',
    [switch]$NoPrompt,
    [switch]$Relaunched    # internal: this is the elevated copy; pause before closing
)

$ErrorActionPreference = 'Stop'
$onWindows  = ($env:OS -eq 'Windows_NT')
$TASK_NAME  = 'IT Ops Console - automatic refresh'
$CERT_YEARS = 2
$MODULES    = @('Microsoft.Graph.Authentication', 'Microsoft.Graph.Users', 'Microsoft.Graph.Identity.DirectoryManagement')
$APP_PERMISSIONS = @(
    'Directory.Read.All', 'Policy.Read.All', 'RoleManagement.Read.Directory', 'Application.Read.All',
    'Organization.Read.All', 'User.Read.All', 'AuditLog.Read.All',
    'DeviceManagementConfiguration.Read.All', 'DeviceManagementManagedDevices.Read.All', 'DeviceManagementApps.Read.All'
)

$tools      = Join-Path $Root 'tools'
$output     = Join-Path $Root 'output'
$site       = Join-Path $Root 'console-site'
$consoleDir = Join-Path $tools 'it-ops-console'
$runAll     = Join-Path $consoleDir 'run-all.ps1'
$iniPath    = Join-Path $consoleDir 'automatic-refresh.ini'

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
function Read-IniFile {
    param([string]$Path)
    $ini = @{}
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $ini }
    $section = ''
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $raw.Trim()
        if (-not $line -or $line[0] -in ';', '#') { continue }
        if ($line -match '^\[(.+)\]$') { $section = $matches[1].Trim().ToLowerInvariant(); if (-not $ini.ContainsKey($section)) { $ini[$section] = @{} }; continue }
        $eq = $line.IndexOf('='); if ($eq -lt 1) { continue }
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

function Write-RefreshIni {
    <# The whole file, every time - one source of truth, no stale keys. The
       app registration details are kept across mode changes so switching
       unattended -> off -> unattended again never means re-entering them. #>
    param([string]$ModeValue, [string]$TimeValue, [string]$RunAs, [bool]$KeepSignedIn,
          [string]$Tenant, [string]$Client, [string]$Thumbprint, [string]$Expires)
    $keepTxt = if ($KeepSignedIn) { 'yes' } else { 'no' }
    $text = @"
# Written by setup ($(Get-Date -Format yyyy-MM-dd)). Change it by re-running setup, not by hand.
# There is nothing secret in this file: no password, no key. The certificate's
# private key lives in this computer's certificate store and cannot be exported.

[schedule]
mode = $ModeValue
time = $TimeValue
run_as = $RunAs
task = $TASK_NAME

[signin]
keep_signed_in = $keepTxt
tenant_id = $Tenant
client_id = $Client
certificate_thumbprint = $Thumbprint
certificate_expires = $Expires
"@
    $null = New-Item -ItemType Directory -Path (Split-Path $iniPath -Parent) -Force
    Set-Content -Path $iniPath -Value $text -Encoding UTF8
}

function Test-Elevated {
    if (-not $onWindows) { return $true }   # no UAC to satisfy off Windows
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Restart-Elevated {
    <# Re-run this script as administrator (one UAC prompt), wait for it, then
       let the caller re-read the ini to report what happened. #>
    param([string]$ModeValue)
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                 '-Root', "`"$Root`"", '-Mode', $ModeValue, '-Relaunched')
    if ($Time)     { $argList += @('-Time', $Time) }
    if ($Python)   { $argList += @('-Python', "`"$Python`"") }
    if ($TenantId) { $argList += @('-TenantId', $TenantId) }
    if ($ClientId) { $argList += @('-ClientId', $ClientId) }
    if ($NoPrompt) { $argList += '-NoPrompt' }
    Write-Host ''
    Write-Host '  This step needs administrator rights - Windows will ask you to allow it.'
    try {
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -PassThru -Wait
        return ($p.ExitCode -eq 0)
    } catch {
        Write-Warning "  Not allowed (or cancelled). Automatic refresh stays as it was."
        return $false
    }
}

function Ask-Line {
    param([string]$Prompt, [string]$Default)
    if ($NoPrompt) { return $Default }
    $v = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($v)) { $Default } else { $v.Trim() }
}

function Get-TaskIfAny {
    try { return Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction Stop } catch { return $null }
}
function Remove-TaskIfAny {
    if (Get-TaskIfAny) { Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction Stop; return $true }
    return $false
}

function New-RefreshTask {
    <# One Task Scheduler job, either as the signed-in person (runs only while
       they are logged on, no password stored) or as SYSTEM (unattended). #>
    param([string]$RunAs, [string]$TimeValue, [string]$PythonForTask)
    $argText = "-NoProfile -ExecutionPolicy Bypass -File `"$runAll`" -ToolRoot `"$tools`" -OutputRoot `"$output`" -SitePath `"$site`" -Scheduled -NoStatusPage"
    if ($PythonForTask) { $argText += " -Python `"$PythonForTask`"" }
    $action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argText -WorkingDirectory $consoleDir
    $trigger  = New-ScheduledTaskTrigger -Daily -At $TimeValue
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
                    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    if ($RunAs -eq 'SYSTEM') {
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    } else {
        # Interactive = "run only when user is logged on": no password is
        # stored, and a sign-in window (if ever needed) can actually be seen.
        $principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType Interactive -RunLevel Limited
    }
    $null = Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $trigger `
                -Settings $settings -Principal $principal -Force `
                -Description 'Refreshes the IT Ops Console: collects read-only data from Microsoft 365 and the printers, then rebuilds the pages. Set up by setup.ps1; change it by re-running setup.'
}

function Invoke-PythonText {
    <# Run one line of Python (-c) and return what it printed, as plain text.
       On Windows the call goes through cmd.exe so anything on stderr is merged
       into text instead of surfacing as a red error under PowerShell 5.1. #>
    param([string]$Exe, [string]$Code, [string[]]$Flags = @())
    $ErrorActionPreference = 'Continue'
    try {
        if ($onWindows) {
            $line = "`"$Exe`" " + (@($Flags) -join ' ') + " -c `"$Code`" 2>&1"
            return ((& cmd.exe /d /c $line | Out-String).Trim())
        }
        return ((& $Exe @Flags -c $Code 2>&1 | Out-String).Trim())
    } catch { return '' }
}

function Resolve-PythonExe {
    <# A SYSTEM task has no user PATH and no per-user "py" launcher registry, so
       it needs the interpreter's own full path. Ask Python where it lives. #>
    param([string]$Cmd)
    if (-not $Cmd) { return '' }
    $exe = Invoke-PythonText -Exe $Cmd -Code 'import sys; print(sys.executable)'
    if ($exe -and (Test-Path -LiteralPath $exe)) { return $exe }
    return $Cmd
}

function Get-CertByThumbprint {
    param([string]$Thumbprint)
    if (-not $Thumbprint) { return $null }
    try { return Get-Item "Cert:\LocalMachine\My\$Thumbprint" -ErrorAction Stop } catch { return $null }
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- Keeping the console fresh ---'
if (-not (Test-Path -LiteralPath $runAll)) {
    Write-Warning "  run-all.ps1 was not found at $runAll - install the console first (run setup)."
    exit 1
}
$current     = Read-IniFile $iniPath
$curMode     = (Get-IniValue $current 'schedule' 'mode' 'off').ToLowerInvariant()
$curTime     = Get-IniValue $current 'schedule' 'time' '07:00'
$curRunAs    = Get-IniValue $current 'schedule' 'run_as'
$curTenant   = Get-IniValue $current 'signin' 'tenant_id'
$curClient   = Get-IniValue $current 'signin' 'client_id'
$curThumb    = Get-IniValue $current 'signin' 'certificate_thumbprint'
$curExpires  = Get-IniValue $current 'signin' 'certificate_expires'
$curKeep     = (Get-IniValue $current 'signin' 'keep_signed_in' 'no') -match '^(yes|true|1)$'
$modeNumber  = @{ 'off' = '1'; 'while-signed-in' = '2'; 'unattended' = '3' }
$numberMode  = @{ '1' = 'off'; '2' = 'while-signed-in'; '3' = 'unattended' }

if (-not $Mode) {
    Write-Host '  How should this console stay up to date?'
    Write-Host ''
    Write-Host '    1. I''ll click "Refresh IT Ops Data" myself when I want new numbers.'
    Write-Host '    2. Refresh every day while I''m signed in to this computer.'
    Write-Host '       It stays signed in to Microsoft 365 (read-only) between refreshes,'
    Write-Host '       so it will not ask you to sign in each time.'
    Write-Host '    3. Refresh every day even when nobody is signed in.'
    Write-Host '       Needs a Global Administrator once (about 15 minutes) to register'
    Write-Host '       an app - this window walks you through it.'
    Write-Host ''
    $cur = $modeNumber[$curMode]; if (-not $cur) { $cur = '1' }
    $pick = ''
    while (-not $numberMode.ContainsKey($pick)) {
        $pick = Ask-Line '  Your choice (1, 2 or 3)' $cur
        if ($NoPrompt -and -not $numberMode.ContainsKey($pick)) { $pick = $cur }
    }
    $Mode = $numberMode[$pick]
}

if ($Mode -ne 'off' -and -not $Time) {
    $t = ''
    while (-not ($t -match '^([01]?\d|2[0-3]):[0-5]\d$')) {
        $t = Ask-Line '  Time of day for the daily refresh (24-hour, e.g. 07:00)' $curTime
        if ($NoPrompt -and -not ($t -match '^([01]?\d|2[0-3]):[0-5]\d$')) { $t = '07:00' }
    }
    $Time = $t
}
if ($Time -and -not ($Time -match '^([01]?\d|2[0-3]):[0-5]\d$')) { Write-Warning "  '$Time' is not a time like 07:00 - using 07:00."; $Time = '07:00' }
if ($Time) { $Time = ('{0:00}:{1}' -f [int]$Time.Split(':')[0], $Time.Split(':')[1]) }

# A SYSTEM task can only be replaced or removed by an administrator.
$needsAdmin = ($Mode -eq 'unattended') -or ($curRunAs -eq 'SYSTEM' -and (Get-TaskIfAny))
if ($needsAdmin -and -not (Test-Elevated)) {
    $ok = Restart-Elevated -ModeValue $Mode
    $after = Read-IniFile $iniPath
    $afterMode = (Get-IniValue $after 'schedule' 'mode' 'off')
    if ($ok -and $afterMode -eq $Mode) { Write-Host "  done: automatic refresh is now '$Mode'." }
    else { Write-Warning "  Automatic refresh is still '$afterMode'." }
    exit 0
}

$exitCode = 0
try {
    switch ($Mode) {
        'off' {
            $removed = Remove-TaskIfAny
            if ($curKeep) {
                # The person is turning the schedule off: close the saved sign-in now,
                # so "stays signed in" stops being true the moment they said so.
                try {
                    Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
                    Disconnect-MgGraph -ErrorAction Stop | Out-Null
                    Write-Host '  signed out of Microsoft Graph.'
                } catch { Write-Host '  (no saved Microsoft Graph sign-in to close)' }
            }
            Write-RefreshIni -ModeValue 'off' -TimeValue '' -RunAs '' -KeepSignedIn $false `
                -Tenant $curTenant -Client $curClient -Thumbprint $curThumb -Expires $curExpires
            if ($removed) { Write-Host '  removed the daily refresh task.' }
            Write-Host '  Automatic refresh is OFF. "Refresh IT Ops Data" on your desktop is the routine,'
            Write-Host '  and every run signs out of Microsoft 365 when it finishes.'
        }
        'while-signed-in' {
            $me = if ($onWindows) { [Security.Principal.WindowsIdentity]::GetCurrent().Name } else { "$env:USER" }
            $null = Remove-TaskIfAny
            New-RefreshTask -RunAs $me -TimeValue $Time -PythonForTask $Python
            Write-RefreshIni -ModeValue 'while-signed-in' -TimeValue $Time -RunAs $me -KeepSignedIn $true `
                -Tenant $curTenant -Client $curClient -Thumbprint $curThumb -Expires $curExpires
            Write-Host "  Every day at $Time, while you are signed in to this computer, the console"
            Write-Host '  refreshes on its own (if the computer was asleep, it runs when it wakes).'
            Write-Host '  It STAYS SIGNED IN to Microsoft 365 (read-only) between refreshes, so it'
            Write-Host '  will not ask you to sign in each time. That saved sign-in is encrypted to'
            Write-Host '  your Windows account. To stop and sign out: re-run setup and pick 1.'
            Write-Host '  The first run may still show a sign-in window once.'
        }
        'unattended' {
            # ---- 1. Graph modules where SYSTEM can see them ---- #
            $allUsersRoots = @()
            if ($env:ProgramFiles) { $allUsersRoots += (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'), (Join-Path $env:ProgramFiles 'PowerShell\Modules') }
            $missing = @($MODULES | Where-Object {
                $m = $_
                -not @(Get-Module -ListAvailable -Name $m | Where-Object { $mb = $_.ModuleBase; @($allUsersRoots | Where-Object { $mb -like "$_*" }).Count -gt 0 }).Count
            })
            if ($missing.Count -and $onWindows) {
                Write-Host "  installing for all users (a SYSTEM task cannot see your personal modules): $($missing -join ', ')"
                try {
                    if (-not (Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue)) {
                        $null = Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force
                    }
                    foreach ($m in $missing) { Install-Module $m -Scope AllUsers -Force -AllowClobber }
                } catch { throw "Could not install the Microsoft Graph modules for all users ($($_.Exception.Message))." }
            }

            # ---- 2. the certificate ---- #
            $cert = Get-CertByThumbprint $curThumb
            $renew = (-not $cert) -or ($cert.NotAfter -lt (Get-Date).AddDays(60))
            if ($renew) {
                Write-Host '  making a certificate for the app sign-in (private key stays on this computer, not exportable)...'
                $cert = New-SelfSignedCertificate -Subject "CN=IT Ops Console automatic refresh ($env:COMPUTERNAME)" `
                            -CertStoreLocation 'Cert:\LocalMachine\My' -KeyExportPolicy NonExportable `
                            -KeySpec Signature -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 `
                            -KeyUsage DigitalSignature -NotAfter (Get-Date).AddYears($CERT_YEARS)
            }
            $thumb   = $cert.Thumbprint
            $expires = $cert.NotAfter.ToString('yyyy-MM-dd')
            $desktop = [Environment]::GetFolderPath('Desktop')
            if (-not $desktop) { $desktop = $Root }
            $cerPath = Join-Path $desktop 'IT-Ops-Console-refresh.cer'
            $null = Export-Certificate -Cert $cert -FilePath $cerPath -Force
            Write-Host "  certificate ready (expires $expires). Public half saved to your desktop: IT-Ops-Console-refresh.cer"
            # Record it NOW, before the human step: whatever happens next (a typo
            # in an ID, a closed window), the .cer they upload stays the one used.
            Write-RefreshIni -ModeValue $curMode -TimeValue $curTime -RunAs $curRunAs -KeepSignedIn $curKeep `
                -Tenant $(if ($TenantId) { $TenantId } else { $curTenant }) -Client $(if ($ClientId) { $ClientId } else { $curClient }) `
                -Thumbprint $thumb -Expires $expires

            # ---- 3. the human step: register the app ---- #
            if (-not ($TenantId -and $ClientId)) {
                Write-Host ''
                Write-Host '  ONE-TIME STEP FOR A GLOBAL ADMINISTRATOR (about 15 minutes):'
                Write-Host '   1. Open https://entra.microsoft.com and sign in as a Global Administrator.'
                Write-Host '   2. Identity > Applications > App registrations > New registration.'
                Write-Host '      Name it "IT Ops Console (read-only)". Leave everything else as is. Register.'
                Write-Host '   3. On the app''s Overview page, copy BOTH of these - you will type them below:'
                Write-Host '        Application (client) ID     Directory (tenant) ID'
                Write-Host '   4. API permissions > Add a permission > Microsoft Graph > APPLICATION permissions.'
                Write-Host '      Tick these ten, then Add permissions:'
                foreach ($perm in $APP_PERMISSIONS) { Write-Host "        $perm" }
                Write-Host '   5. Still on API permissions: "Grant admin consent for <your organisation>" > Yes.'
                Write-Host '      Every row should now show a green tick.'
                Write-Host '   6. Certificates & secrets > Certificates > Upload certificate.'
                Write-Host "      Pick IT-Ops-Console-refresh.cer from your desktop ($cerPath). Add."
                Write-Host '      (Do NOT create a client secret - the certificate is the whole sign-in.)'
                Write-Host ''
                Write-Host '  Everything this app is allowed to do is READ. It cannot change anything in your tenant.'
                Write-Host ''
                if (-not $NoPrompt) { $null = Read-Host '  Press Enter when the certificate is uploaded and you have both IDs' }
            }
            $guid = '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
            $t = $TenantId; $c = $ClientId
            while (-not ($t -match $guid)) { $t = Ask-Line '  Directory (tenant) ID' $curTenant; if ($NoPrompt) { break } }
            while (-not ($c -match $guid)) { $c = Ask-Line '  Application (client) ID' $curClient; if ($NoPrompt) { break } }
            if (-not ($t -match $guid -and $c -match $guid)) { throw 'Both IDs must look like 12345678-1234-1234-1234-123456789abc.' }

            # ---- 4. prove it before scheduling anything ---- #
            Write-Host '  checking that the app can sign in and read your organisation name (read-only)...'
            $orgName = ''
            try {
                Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
                Connect-MgGraph -ClientId $c -TenantId $t -CertificateThumbprint $thumb -NoWelcome -ErrorAction Stop
                $org = Invoke-MgGraphRequest -Method GET -Uri 'https://graph.microsoft.com/v1.0/organization' -ErrorAction Stop
                $orgName = @($org.value)[0].displayName
                Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
            } catch {
                $why = $_.Exception.Message
                $plain = switch -Wildcard ($why) {
                    '*AADSTS700027*' { 'Microsoft does not recognise the certificate - step 6 (upload IT-Ops-Console-refresh.cer) is not done, or a different file was uploaded.' }
                    '*AADSTS7000215*' { 'Microsoft rejected the sign-in - the app was set up with a client secret instead of the certificate.' }
                    '*AADSTS700016*' { 'That Application (client) ID does not exist in that tenant - check both IDs.' }
                    '*AADSTS90002*'  { 'That Directory (tenant) ID was not found - check it.' }
                    '*Authorization_RequestDenied*' { 'The app signed in but is not allowed to read - step 5 (Grant admin consent) is not done.' }
                    '*Insufficient privileges*'     { 'The app signed in but is not allowed to read - step 5 (Grant admin consent) is not done.' }
                    default { "The app sign-in did not work: $why" }
                }
                Write-RefreshIni -ModeValue $curMode -TimeValue $curTime -RunAs $curRunAs -KeepSignedIn $curKeep `
                    -Tenant $t -Client $c -Thumbprint $thumb -Expires $expires
                throw "$plain Nothing was scheduled; your entries were kept, so re-run setup and try again once that is fixed."
            }
            Write-Host "  it works: signed in as the app and read '$orgName'."

            # ---- 5. the printer library, if printers are in use ---- #
            $pyExe = Resolve-PythonExe $Python
            $fleetCfg = Join-Path (Join-Path $tools 'print-fleet-dashboard') 'config.ini'
            $fleetEx  = Join-Path (Join-Path $tools 'print-fleet-dashboard') 'config.example.ini'
            $printersOn = (Test-Path $fleetCfg) -and (-not (Test-Path $fleetEx) -or
                ((Get-Content $fleetCfg -Raw) -replace '\s', '') -ne ((Get-Content $fleetEx -Raw) -replace '\s', ''))
            if ($printersOn -and $pyExe) {
                # -s ignores your personal package folder, which SYSTEM cannot see.
                $probe = Invoke-PythonText -Exe $pyExe -Flags @('-s') -Code "import pysnmp; print('ok')"
                if ($probe -ne 'ok') {
                    Write-Warning '  The printer library (pysnmp) is installed only for your account, so the unattended'
                    Write-Warning '  refresh would skip the printers. To fix, run once in a normal PowerShell window:'
                    Write-Warning "     & `"$pyExe`" -m pip install `"pysnmp>=7.1`""
                }
            }

            # ---- 6. schedule it as SYSTEM ---- #
            $null = Remove-TaskIfAny
            New-RefreshTask -RunAs 'SYSTEM' -TimeValue $Time -PythonForTask $pyExe
            Write-RefreshIni -ModeValue 'unattended' -TimeValue $Time -RunAs 'SYSTEM' -KeepSignedIn $false `
                -Tenant $t -Client $c -Thumbprint $thumb -Expires $expires
            Write-Host ''
            Write-Host "  Every day at $Time, whether or not anyone is signed in, this computer refreshes"
            Write-Host '  the console as the app you registered (read-only). No password is stored anywhere;'
            Write-Host '  the certificate''s private key stays in this computer and cannot be exported.'
            Write-Host "  The certificate expires on $expires - the console and check-setup warn you 30 days"
            Write-Host '  ahead. Re-run setup then and it makes a new one for you to upload.'
        }
    }
} catch {
    Write-Warning "  $($_.Exception.Message)"
    $exitCode = 1
}

if ($Relaunched -and -not $NoPrompt) {
    Write-Host ''
    $null = Read-Host '  Press Enter to close this window'
}
exit $exitCode
