# Test: schedule-refresh.ps1 (setup's last step).   pwsh tests/test_schedule_refresh.ps1
#
# Runs the script against stub Task Scheduler, certificate and Graph cmdlets,
# and checks what it WRITES: the Task Scheduler job (who it runs as, when, with
# what arguments), automatic-refresh.ini (mode, stay-signed-in, app details
# kept across mode changes), the exported certificate, and the proof-before-
# scheduling rule for unattended mode. Nothing here touches Windows, Task
# Scheduler, a certificate store, or Microsoft 365.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSCommandPath
$repo = Split-Path -Parent $here
$work = Join-Path ([IO.Path]::GetTempPath()) "itops-sched-$([guid]::NewGuid().ToString('n').Substring(0, 6))"
$root = Join-Path $work 'IT-Ops'
$consoleDir = Join-Path (Join-Path $root 'tools') 'it-ops-console'
$tasks = Join-Path $work 'tasks'; $certs = Join-Path $work 'certs.json'; $log = Join-Path $work 'graph.log'
$mods = Join-Path $work 'mods'
$ini = Join-Path $consoleDir 'automatic-refresh.ini'
$null = New-Item -ItemType Directory -Path $consoleDir, $tasks, $mods -Force
Copy-Item (Join-Path $repo 'run-all.ps1') $consoleDir
Copy-Item (Join-Path $repo 'schedule-refresh.ps1') $consoleDir
$python = if (Get-Command python3 -ErrorAction SilentlyContinue) { 'python3' } else { 'python' }
$me = if ($env:OS -eq 'Windows_NT') { [Security.Principal.WindowsIdentity]::GetCurrent().Name } else { "$env:USER" }

$fails = [System.Collections.Generic.List[string]]::new()
function Check { param([string]$Label, [bool]$Cond)
    Write-Host ("{0} {1}" -f ($(if ($Cond) { 'PASS' } else { 'FAIL' })), $Label)
    if (-not $Cond) { $fails.Add($Label) }
}

# ---- stub Graph module (with the one request the proof step makes) -------- #
$modDir = Join-Path $mods 'Microsoft.Graph.Authentication'
$null = New-Item -ItemType Directory -Path $modDir -Force
Set-Content (Join-Path $modDir 'Microsoft.Graph.Authentication.psm1') @'
function Connect-MgGraph {
    param([string[]]$Scopes, [string]$ClientId, [string]$TenantId, [string]$CertificateThumbprint, [switch]$NoWelcome)
    Add-Content $env:ITOPS_STUB_LOG "connect app $ClientId $TenantId $CertificateThumbprint"
    if ("$env:ITOPS_STUB_GRAPH" -split ',' -contains 'app-ok') { return }
    throw 'AADSTS700027: Client assertion contains an invalid signature. [Reason - The key was not found.]'
}
function Invoke-MgGraphRequest { param([string]$Method, [string]$Uri)
    Add-Content $env:ITOPS_STUB_LOG "request $Method $Uri"
    return @{ value = @(@{ displayName = 'Contoso Ltd' }) }
}
function Get-MgContext { $null }
function Disconnect-MgGraph { Add-Content $env:ITOPS_STUB_LOG 'disconnect' }
Export-ModuleMember -Function Connect-MgGraph, Invoke-MgGraphRequest, Get-MgContext, Disconnect-MgGraph
'@
$env:PSModulePath = $mods + [IO.Path]::PathSeparator + $env:PSModulePath
$env:ITOPS_STUB_LOG = $log
$env:ITOPS_STUB_TASKS = $tasks
$env:ITOPS_STUB_CERTS = $certs

# ---- stub Task Scheduler + certificate cmdlets, defined in the child session -- #
$preamble = @'
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) @{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory } }
function New-ScheduledTaskTrigger { param([switch]$Daily, $At) @{ Daily = [bool]$Daily; At = "$At" } }
function New-ScheduledTaskSettingsSet { param([switch]$StartWhenAvailable, $MultipleInstances, $ExecutionTimeLimit, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries)
    @{ StartWhenAvailable = [bool]$StartWhenAvailable; MultipleInstances = "$MultipleInstances"; ExecutionTimeLimit = "$ExecutionTimeLimit" } }
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel) @{ UserId = $UserId; LogonType = "$LogonType"; RunLevel = "$RunLevel" } }
function Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Settings, $Principal, [switch]$Force, $Description)
    $t = @{ TaskName = $TaskName; Action = $Action; Trigger = $Trigger; Settings = $Settings; Principal = $Principal; Description = $Description }
    $t | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $env:ITOPS_STUB_TASKS ("$TaskName.json" -replace '[^A-Za-z0-9. -]', '_'))
    $t }
function Get-ScheduledTask { [CmdletBinding()] param($TaskName)
    $f = Join-Path $env:ITOPS_STUB_TASKS ("$TaskName.json" -replace '[^A-Za-z0-9. -]', '_')
    if (Test-Path $f) { return (Get-Content $f -Raw | ConvertFrom-Json) }
    throw "No MSFT_ScheduledTask objects found with property 'TaskName' equal to '$TaskName'." }
function Unregister-ScheduledTask { [CmdletBinding()] param($TaskName, [switch]$Confirm)
    $f = Join-Path $env:ITOPS_STUB_TASKS ("$TaskName.json" -replace '[^A-Za-z0-9. -]', '_')
    if (Test-Path $f) { Remove-Item $f } }
function New-SelfSignedCertificate { param($Subject, $CertStoreLocation, $KeyExportPolicy, $KeySpec, $KeyAlgorithm, $KeyLength, $HashAlgorithm, $KeyUsage, $NotAfter)
    $list = @(); if (Test-Path $env:ITOPS_STUB_CERTS) { $list = @(Get-Content $env:ITOPS_STUB_CERTS -Raw | ConvertFrom-Json) }
    $thumb = ('C{0:D3}' -f ($list.Count + 1)).PadRight(40, 'F')
    $c = @{ Thumbprint = $thumb; NotAfter = ([datetime]$NotAfter).ToString('o'); Subject = $Subject; Store = $CertStoreLocation; Export = "$KeyExportPolicy" }
    $list += $c
    ConvertTo-Json @($list) | Set-Content $env:ITOPS_STUB_CERTS
    [pscustomobject]@{ Thumbprint = $thumb; NotAfter = [datetime]$NotAfter; Subject = $Subject } }
function Export-Certificate { param($Cert, $FilePath, [switch]$Force) Set-Content $FilePath "PUBLIC $($Cert.Thumbprint)"; Get-Item $FilePath }
function Get-Item {
    [CmdletBinding()] param([Parameter(Position=0)][string]$Path, [string]$LiteralPath, [switch]$Force)
    if ($Path -like 'Cert:*') {
        $thumb = ($Path -split '[\\/]')[-1]
        if (Test-Path $env:ITOPS_STUB_CERTS) {
            foreach ($c in @(Get-Content $env:ITOPS_STUB_CERTS -Raw | ConvertFrom-Json)) {
                if ($c.Thumbprint -eq $thumb) { return [pscustomobject]@{ Thumbprint = $c.Thumbprint; NotAfter = [datetime]$c.NotAfter } }
            }
        }
        throw "Cannot find path '$Path' because it does not exist."
    }
    if ($LiteralPath) { return Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force }
    Microsoft.PowerShell.Management\Get-Item -Path $Path -Force:$Force
}
'@

function Run-Sched {
    param([string]$ArgText, [string]$Graph = '')
    if (Test-Path $log) { Remove-Item $log }
    $env:ITOPS_STUB_GRAPH = $Graph
    $cmd = $preamble + "`n& '$consoleDir/schedule-refresh.ps1' -Root '$root' -NoPrompt $ArgText"
    $text = (& pwsh -NoProfile -Command $cmd 2>&1 | Out-String)
    $code = $LASTEXITCODE
    $task = $null
    $tf = Join-Path $tasks 'IT Ops Console - automatic refresh.json'
    if (Test-Path $tf) { $task = Get-Content $tf -Raw | ConvertFrom-Json }
    $cfg = @{}
    if (Test-Path $ini) {
        $section = ''
        foreach ($l in Get-Content $ini) {
            $l = $l.Trim(); if (-not $l -or $l[0] -in ';', '#') { continue }
            if ($l -match '^\[(.+)\]$') { $section = $matches[1]; continue }
            $k, $v = $l.Split('=', 2); $cfg["$section.$($k.Trim())"] = $v.Trim()
        }
    }
    $entries = @(); if (Test-Path $log) { $entries = @(Get-Content $log) }
    return @{ Code = $code; Text = $text; Task = $task; Ini = $cfg; Log = $entries }
}

Write-Host ''
Write-Host '-- 1. off, nothing set up before: writes the ini, no task'
$r = Run-Sched "-Mode off"
Check 'exit 0' ($r.Code -eq 0)
Check 'no task' ($null -eq $r.Task)
Check 'ini: mode off, not keeping signed in' ($r.Ini['schedule.mode'] -eq 'off' -and $r.Ini['signin.keep_signed_in'] -eq 'no')
Check 'words: off, every run signs out' ($r.Text -like '*Automatic refresh is OFF*' -and $r.Text -like '*signs out of Microsoft 365*')
Check 'ini says there is nothing secret in it' ((Get-Content $ini -Raw) -like '*nothing secret in this file*')

Write-Host ''
Write-Host '-- 2. while-signed-in at 07:00: a task as me, interactive, no password; stays signed in'
$r = Run-Sched "-Mode while-signed-in -Time 07:00 -Python $python"
Check 'exit 0' ($r.Code -eq 0)
Check 'task registered' ($null -ne $r.Task)
Check 'runs as me' ($r.Task.Principal.UserId -eq $me)
Check 'only while logged on (Interactive), not elevated' ($r.Task.Principal.LogonType -eq 'Interactive' -and $r.Task.Principal.RunLevel -eq 'Limited')
Check 'daily at 07:00' ($r.Task.Trigger.Daily -eq $true -and $r.Task.Trigger.At -eq '07:00')
Check 'runs when a missed start is possible again' ($r.Task.Settings.StartWhenAvailable -eq $true)
Check 'runs run-all as a scheduled, page-less run' ($r.Task.Action.Argument -like "*run-all.ps1`"*-Scheduled -NoStatusPage*")
Check 'passes the install paths' ($r.Task.Action.Argument -like "*-ToolRoot `"$root/tools`"*" -or $r.Task.Action.Argument -like "*-ToolRoot `"$root\tools`"*")
Check 'passes python' ($r.Task.Action.Argument -like "*-Python `"$python`"*")
Check 'ini: while-signed-in, 07:00, keep yes, run_as me' ($r.Ini['schedule.mode'] -eq 'while-signed-in' -and $r.Ini['schedule.time'] -eq '07:00' -and $r.Ini['signin.keep_signed_in'] -eq 'yes' -and $r.Ini['schedule.run_as'] -eq $me)
Check 'words: stays signed in, how to stop' ($r.Text -like '*STAYS SIGNED IN*' -and $r.Text -like '*re-run setup and pick 1*')

Write-Host ''
Write-Host '-- 3. back to off from while-signed-in: task removed, signed out now'
$r = Run-Sched "-Mode off"
Check 'exit 0' ($r.Code -eq 0)
Check 'task removed' ($null -eq $r.Task)
Check 'signed out on the spot' (($r.Log -contains 'disconnect') -and $r.Text -like '*signed out of Microsoft Graph*')
Check 'ini: off, keep no' ($r.Ini['schedule.mode'] -eq 'off' -and $r.Ini['signin.keep_signed_in'] -eq 'no')

Write-Host ''
Write-Host '-- 4. time normalisation: 7:00 -> 07:00; nonsense -> 07:00 with a warning'
$r = Run-Sched "-Mode while-signed-in -Time 7:00"
Check '7:00 becomes 07:00' ($r.Ini['schedule.time'] -eq '07:00' -and $r.Task.Trigger.At -eq '07:00')
$r = Run-Sched "-Mode while-signed-in -Time noon"
Check 'nonsense time falls back to 07:00 and says so' ($r.Ini['schedule.time'] -eq '07:00' -and $r.Text -like "*'noon' is not a time*")
$null = Run-Sched "-Mode off"

Write-Host ''
Write-Host '-- 5. unattended without the two IDs: refuses, schedules nothing'
$r = Run-Sched "-Mode unattended -Time 06:30 -Python $python" -Graph 'app-ok'
Check 'exit 1' ($r.Code -eq 1)
Check 'says what is missing' ($r.Text -like '*Both IDs must look like*')
Check 'no task' ($null -eq $r.Task)
Check 'certificate was still made and exported (the human step needs it)' ((Test-Path $certs) -and (Test-Path (Join-Path $root 'IT-Ops-Console-refresh.cer')))
Check 'ini still off' ($r.Ini['schedule.mode'] -eq 'off')

Write-Host ''
Write-Host '-- 6. unattended, app sign-in rejected: plain reason, nothing scheduled, entries kept'
$tid = '11111111-1111-1111-1111-111111111111'; $cid = '22222222-2222-2222-2222-222222222222'
$r = Run-Sched "-Mode unattended -Time 06:30 -Python $python -TenantId $tid -ClientId $cid"
Check 'exit 1' ($r.Code -eq 1)
Check 'reason in plain words (certificate not uploaded)' ($r.Text -like '*step 6 (upload IT-Ops-Console-refresh.cer)*')
Check 'says nothing was scheduled and entries were kept' ($r.Text -like '*Nothing was scheduled; your entries were kept*')
Check 'no task' ($null -eq $r.Task)
Check 'ini: mode still off, IDs and thumbprint kept for the retry' ($r.Ini['schedule.mode'] -eq 'off' -and $r.Ini['signin.tenant_id'] -eq $tid -and $r.Ini['signin.client_id'] -eq $cid -and $r.Ini['signin.certificate_thumbprint'] -like 'C00*')
$certCount = @(Get-Content $certs -Raw | ConvertFrom-Json).Count
Check 'reused the certificate from the previous attempt (one made so far)' ($certCount -eq 1)

Write-Host ''
Write-Host '-- 7. unattended, app sign-in proven: SYSTEM task, full python path, ini complete'
$r = Run-Sched "-Mode unattended -Time 06:30 -Python $python -TenantId $tid -ClientId $cid" -Graph 'app-ok'
Check 'exit 0' ($r.Code -eq 0)
Check 'proved the sign-in before scheduling (connect, read organization, disconnect)' (($r.Log[0] -like "connect app $cid $tid C00*") -and ($r.Log[1] -like 'request GET https://graph.microsoft.com/v1.0/organization') -and ($r.Log[2] -eq 'disconnect'))
Check 'reports the organisation it read' ($r.Text -like "*signed in as the app and read 'Contoso Ltd'*")
Check 'task runs as SYSTEM, service account, highest' ($r.Task.Principal.UserId -eq 'SYSTEM' -and $r.Task.Principal.LogonType -eq 'ServiceAccount' -and $r.Task.Principal.RunLevel -eq 'Highest')
Check 'daily at 06:30' ($r.Task.Trigger.At -eq '06:30')
Check 'python passed as a full path, not a name' ($r.Task.Action.Argument -match '-Python "(/|[A-Za-z]:\\)[^"]*python[^"]*"')
Check 'ini: unattended, SYSTEM, keep no, app details, expiry ~2 years out' (
    $r.Ini['schedule.mode'] -eq 'unattended' -and $r.Ini['schedule.run_as'] -eq 'SYSTEM' -and $r.Ini['signin.keep_signed_in'] -eq 'no' -and
    $r.Ini['signin.tenant_id'] -eq $tid -and $r.Ini['signin.client_id'] -eq $cid -and
    ([datetime]::ParseExact($r.Ini['signin.certificate_expires'], 'yyyy-MM-dd', $null) -gt (Get-Date).AddDays(700)))
Check 'words: no password anywhere, expiry date, 30-day warning' ($r.Text -like '*No password is stored anywhere*' -and $r.Text -like "*expires on $($r.Ini['signin.certificate_expires'])*" -and $r.Text -like '*30 days*')
Check 'still one certificate (reused, not re-made)' (@(Get-Content $certs -Raw | ConvertFrom-Json).Count -eq 1)

Write-Host ''
Write-Host '-- 8. unattended again (re-run setup): same certificate, same task, no churn'
$r = Run-Sched "-Mode unattended -Time 06:30 -Python $python -TenantId $tid -ClientId $cid" -Graph 'app-ok'
Check 'exit 0' ($r.Code -eq 0)
Check 'one certificate still' (@(Get-Content $certs -Raw | ConvertFrom-Json).Count -eq 1)
Check 'task present' ($null -ne $r.Task -and $r.Task.Principal.UserId -eq 'SYSTEM')

Write-Host ''
Write-Host '-- 9. certificate close to expiry: a new one is made on re-run'
$list = @(Get-Content $certs -Raw | ConvertFrom-Json)
$list[0].NotAfter = (Get-Date).AddDays(20).ToString('o')
ConvertTo-Json @($list) | Set-Content $certs
$r = Run-Sched "-Mode unattended -Time 06:30 -Python $python -TenantId $tid -ClientId $cid" -Graph 'app-ok'
Check 'exit 0' ($r.Code -eq 0)
Check 'a second certificate was made' (@(Get-Content $certs -Raw | ConvertFrom-Json).Count -eq 2)
Check 'ini points at the new one' ($r.Ini['signin.certificate_thumbprint'] -like 'C002*')
Check 'the human step is shown again (new .cer to upload)' ((Get-Content (Join-Path $root 'IT-Ops-Console-refresh.cer') -Raw) -like 'PUBLIC C002*')

Write-Host ''
Write-Host '-- 10. unattended -> off: SYSTEM task removed, app details kept for next time'
$r = Run-Sched "-Mode off"
Check 'exit 0' ($r.Code -eq 0)
Check 'task removed' ($null -eq $r.Task)
Check 'ini: off, but tenant/client/thumbprint kept' ($r.Ini['schedule.mode'] -eq 'off' -and $r.Ini['signin.tenant_id'] -eq $tid -and $r.Ini['signin.certificate_thumbprint'] -like 'C002*')
Check 'no sign-out attempted (nothing was kept signed in)' (-not ($r.Log -contains 'disconnect'))

Write-Host ''
Write-Host '-- 11. run-all honours what schedule-refresh wrote (unattended -> app rung)'
$null = Run-Sched "-Mode unattended -Time 06:30 -Python $python -TenantId $tid -ClientId $cid" -Graph 'app-ok'
$thumb = (Run-Sched "-Mode unattended -Time 06:30 -Python $python -TenantId $tid -ClientId $cid" -Graph 'app-ok').Ini['signin.certificate_thumbprint']
Check 'thumbprint recorded' ($thumb -like 'C002*')

Write-Host ''
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$env:ITOPS_STUB_GRAPH = $null; $env:ITOPS_STUB_LOG = $null; $env:ITOPS_STUB_TASKS = $null; $env:ITOPS_STUB_CERTS = $null
if ($fails.Count) { Write-Host "RESULT: $($fails.Count) FAILURES"; $fails | ForEach-Object { Write-Host "  - $_" }; exit 1 }
Write-Host 'RESULT: ALL PASS'
exit 0
