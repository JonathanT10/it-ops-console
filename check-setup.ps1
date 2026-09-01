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
foreach ($c in 'python','py') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and ("$(& $c --version 2>&1)" -match 'Python 3')) { $py = $c; break }
}
if ($py) { Say "Python 3 ($py)" } else { Gap 'Python 3 not found - the console pages cannot rebuild without it. Install from python.org (tick "Add python.exe to PATH")' }

Write-Host ''
if ($problems -eq 0) { Write-Host 'Everything looks right. If a refresh still fails, the red text in its window says which step - send check-setup.log and that text to whoever is helping you.' }
else { Write-Host "$problems problem(s) found - the lines marked PROBLEM above say what to do for each." }
try { Stop-Transcript | Out-Null } catch { }
Write-Host ''
if (-not $env:ITOPS_CMD) { $null = Read-Host 'Done - press Enter to close this window' }
