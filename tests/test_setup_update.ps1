# Test: how setup replaces a tool folder.   pwsh tests/test_setup_update.ps1
#
# An upgrade used to delete the tool folder and then copy the new one in. When
# something on the machine was holding that folder open - a PowerShell window
# left over from a refresh, the console while it serves itself, an Explorer
# window sitting in it - the delete failed HALF WAY: files already gone, the
# fresh copy never made, and the settings it had just tucked away stranded in a
# temp folder nobody would find. That happened to a real install.
#
# So what is checked here is not "the update works" but "a failed update leaves
# the install exactly as it was":
#
#   - a settings file survives an update, and a file deleted upstream really goes
#   - the example templates never clobber the real .ini beside them
#   - the settings copy lands beside the install, is rebuilt each run, and never
#     resurrects a file that was deleted since
#   - when the folder CANNOT be replaced, nothing is touched at all, and the
#     message names what to close
#   - one tool that cannot be replaced does not take the rest of the install down
#
# BEING STRAIGHT ABOUT THE LIMIT: this suite runs on Linux, where a directory is
# not locked by a process using it. The locked case is forced with a Move-Item
# that refuses, so what is proven is the CONTRACT the fix rests on - nothing
# touched, the right words - and not the Windows detection itself. That is
# deliberate: the same class of gap as the Windows ExitCode bug in v1.5.4, and
# exactly why the fix is shaped so a failure is harmless rather than relying on
# detecting it.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSCommandPath
$repo = Split-Path -Parent $here
$work = Join-Path ([IO.Path]::GetTempPath()) "itops-setupupd-$([guid]::NewGuid().ToString('n').Substring(0, 6))"
$onWindows = ($env:OS -eq 'Windows_NT')

$fails = [System.Collections.Generic.List[string]]::new()
function Check { param([string]$Label, [bool]$Cond)
    Write-Host ("{0} {1}" -f ($(if ($Cond) { 'PASS' } else { 'FAIL' })), $Label)
    if (-not $Cond) { $fails.Add($Label) }
}

# ---- the function under test, taken from the shipped setup.ps1 ------------ #
# Parsed out of the real file rather than copied, so this can never drift from
# what people actually run.
$setup = Join-Path $repo 'setup.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($setup, [ref]$null, [ref]$null)
$wanted = @('Get-BundleFileList', 'Get-HeldFile', 'Copy-BundleOverTop', 'Install-FromBundle')
$defs = @{}
foreach ($f in $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($wanted -contains $f.Name) { $defs[$f.Name] = $f.Extent.Text }
}
foreach ($name in $wanted) {
    if (-not $defs.ContainsKey($name)) { Write-Host "FAIL setup.ps1 no longer defines $name"; exit 1 }
    . ([scriptblock]::Create($defs[$name]))
}
Check 'the functions come from the shipped setup.ps1' (
    @('Get-BundleFileList','Get-HeldFile','Copy-BundleOverTop','Install-FromBundle' |
      Where-Object { Get-Command $_ -ErrorAction SilentlyContinue }).Count -eq 4)

function New-Bundle {
    # what a release bundle carries for one tool: code and *.example.ini only
    param([string]$Path)
    $null = New-Item -ItemType Directory -Path $Path -Force
    Set-Content (Join-Path $Path 'run-all.ps1')          "# v-new"
    Set-Content (Join-Path $Path 'serve-console.py')     "# brand new in this version"
    Set-Content (Join-Path $Path 'alerts.example.ini')   "[teams]`nwebhook ="
    Set-Content (Join-Path $Path 'config.example.ini')   "[ranges]"
    # a stand-in for the page builder, so the test can see HOW setup calls it
    Set-Content (Join-Path $Path 'build.py') @'
import sys, os
out = sys.argv[sys.argv.index("--out") + 1]
open(os.path.join(out, "built.txt"), "w").write(" ".join(sys.argv[1:]) + "\ncwd=" + os.getcwd())
'@
    $null = New-Item -ItemType Directory -Path (Join-Path $Path 'console') -Force
    Set-Content (Join-Path $Path 'console/pages.py')     "# v-new"
}
function New-Installed {
    # what is on a person's machine before the upgrade: an older version, their
    # settings, a database, and a file that no longer exists upstream
    param([string]$Path)
    $null = New-Item -ItemType Directory -Path $Path -Force
    Set-Content (Join-Path $Path 'run-all.ps1')        "# v-old"
    Set-Content (Join-Path $Path 'apply-settings.ps1') "# deleted upstream"
    Set-Content (Join-Path $Path 'alerts.example.ini') "[teams]`nwebhook ="
    Set-Content (Join-Path $Path 'alerts.ini')         "[teams]`nwebhook = MINE"
    Set-Content (Join-Path $Path 'config.ini')         "[devices]`nLobby = 10.0.10.34"
    Set-Content (Join-Path $Path 'fleet.db')           "not really a database"
    $null = New-Item -ItemType Directory -Path (Join-Path $Path 'console') -Force
    Set-Content (Join-Path $Path 'console/pages.py')   "# v-old"
}

Write-Host ''
Write-Host '-- 1. a fresh install: nothing there yet'
$root = Join-Path $work 'a'; $tools = Join-Path $root 'tools'
$bundle = Join-Path $work 'a-bundle'
$null = New-Item -ItemType Directory -Path $tools -Force
New-Bundle $bundle
Install-FromBundle -Name 'demo' -Source $bundle -Dest (Join-Path $tools 'demo') -BackupRoot (Join-Path $root '.settings-backup') | Out-Null
Check 'the tool lands' (Test-Path (Join-Path $tools 'demo/run-all.ps1'))
Check 'subfolders come with it' (Test-Path (Join-Path $tools 'demo/console/pages.py'))
Check 'no settings backup made when there was nothing to keep' (-not (Test-Path (Join-Path $root '.settings-backup')))

Write-Host ''
Write-Host '-- 2. an upgrade over an existing install'
$root = Join-Path $work 'b'; $tools = Join-Path $root 'tools'
$bundle = Join-Path $work 'b-bundle'; $dest = Join-Path $tools 'demo'
$null = New-Item -ItemType Directory -Path $tools -Force
New-Bundle $bundle; New-Installed $dest
Install-FromBundle -Name 'demo' -Source $bundle -Dest $dest -BackupRoot (Join-Path $root '.settings-backup') | Out-Null
Check 'the code is the new version' ((Get-Content (Join-Path $dest 'run-all.ps1') -Raw).Trim() -eq '# v-new')
Check 'a file new in this version arrives' (Test-Path (Join-Path $dest 'serve-console.py'))
Check 'a file DELETED upstream really goes' (-not (Test-Path (Join-Path $dest 'apply-settings.ps1')))
Check 'your alerts.ini survives, with your own value in it' ((Get-Content (Join-Path $dest 'alerts.ini') -Raw) -like '*MINE*')
Check 'your printer config.ini survives' ((Get-Content (Join-Path $dest 'config.ini') -Raw) -like '*10.0.10.34*')
Check 'the printer database survives' (Test-Path (Join-Path $dest 'fleet.db'))
Check 'the example template is the new one, and did not clobber your alerts.ini' (Test-Path (Join-Path $dest 'alerts.example.ini'))
Check 'subfolders are replaced too' ((Get-Content (Join-Path $dest 'console/pages.py') -Raw).Trim() -eq '# v-new')
Check 'nothing is left renamed aside once it worked' (@(Get-ChildItem -Path $tools -Directory -Filter 'demo.replaced-*').Count -eq 0)

Write-Host ''
Write-Host '-- 3. the settings copy is beside the install, where a person would look'
$backup = Join-Path (Join-Path $root '.settings-backup') 'demo'
Check 'it is under the install folder, not in %TEMP%' (Test-Path $backup)
Check 'it holds the settings as they were' (
    ((Get-Content (Join-Path $backup 'alerts.ini') -Raw) -like '*MINE*') -and
    (Test-Path (Join-Path $backup 'config.ini')) -and (Test-Path (Join-Path $backup 'fleet.db')))
Check 'and only the settings - never the code' (-not (Test-Path (Join-Path $backup 'run-all.ps1')))

Write-Host ''
Write-Host '-- 4. a file you deleted since the last update does not come back'
# The backup folder is reused run after run. Left alone it would hand a deleted
# settings file back to you on the next upgrade, for ever.
Remove-Item (Join-Path $dest 'config.ini')
Install-FromBundle -Name 'demo' -Source $bundle -Dest $dest -BackupRoot (Join-Path $root '.settings-backup') | Out-Null
Check 'the deleted settings file stays deleted' (-not (Test-Path (Join-Path $dest 'config.ini')))
Check 'and it is gone from the backup too' (-not (Test-Path (Join-Path $backup 'config.ini')))
Check 'the ones you kept are still there' ((Get-Content (Join-Path $dest 'alerts.ini') -Raw) -like '*MINE*')

Write-Host ''
Write-Host '-- 5. THE BUG: the folder cannot be renamed out of the way'
# How the failure is forced: a Move-Item that refuses for one named folder.
# On Windows the real cause is a process using the directory - a rename against
# an open directory fails. This container cannot create that condition (it is
# Linux, and running as root at that), so what is proven here is the CONTRACT
# the fix rests on, not the Windows detection itself. That is deliberate - the
# same class of gap as the Windows ExitCode bug in v1.5.4.
# The stand-in for the refusal. On Windows the real cause is a process using
# the directory - a rename against an open directory fails. This container is
# Linux, and running as root at that, so it cannot create that condition.
function Move-Item {
    # $global:, not $script: - inside a function, $script: resolves against the
    # scope of whatever script is RUNNING, so when setup.ps1 calls this the name
    # would be read from setup.ps1's scope, where it is empty, and the stand-in
    # would quietly never refuse.
    # NOT -ErrorAction: CmdletBinding already supplies it, and declaring it too
    # makes every call fail with "defined multiple times" - which looks exactly
    # like the refusal this is meant to simulate, and quietly passed the checks
    # below for the wrong reason the first time this was written.
    [CmdletBinding()]
    param([string]$LiteralPath, [string]$Path, [string]$Destination, [switch]$Force)
    $src = if ($LiteralPath) { $LiteralPath } else { $Path }
    # FailMoveTimes stands in for a holder that is only passing through - a
    # scanner, the indexer - which is what the retry exists for.
    if ($global:FailMoveTimes -gt 0) {
        $global:FailMoveTimes--
        throw "The process cannot access the file '$src' because it is being used by another process."
    }
    if ($global:LockedName -and $src -like "*$global:LockedName*") {
        throw "The process cannot access the file '$src' because it is being used by another process."
    }
    Microsoft.PowerShell.Management\Move-Item -LiteralPath $src -Destination $Destination -Force:$Force
}
# The stand-in must reach the real thing when it is not refusing - proven here,
# so a broken stand-in can never make the cases below pass by failing early.
$probe = Join-Path $work 'mockprobe'
$null = New-Item -ItemType Directory -Path (Join-Path $probe 'from') -Force
Set-Content (Join-Path $probe 'from/x.txt') 'hello'
$global:LockedName = $null
$global:FailMoveTimes = 0
Move-Item -LiteralPath (Join-Path $probe 'from') -Destination (Join-Path $probe 'to') -ErrorAction Stop
Check 'the stand-in Move-Item really moves when it is not refusing' (
    (Test-Path (Join-Path $probe 'to/x.txt')) -and -not (Test-Path (Join-Path $probe 'from')))
# ...and that it really refuses when it should, so a silent no-op cannot pass either
$global:LockedName = 'mockprobe'
$refused = $false
try { Move-Item -LiteralPath (Join-Path $probe 'to') -Destination (Join-Path $probe 'to2') -ErrorAction Stop }
catch { $refused = $true }
Check 'and really refuses when it should' ($refused -and (Test-Path (Join-Path $probe 'to/x.txt')))

$global:LockedName = 'demo'
$global:FailMoveTimes = 0
$root = Join-Path $work 'c'; $tools = Join-Path $root 'tools'
$bundle = Join-Path $work 'c-bundle'; $dest = Join-Path $tools 'demo'
$null = New-Item -ItemType Directory -Path $tools -Force
New-Bundle $bundle; New-Installed $dest
$err = ''
$out = ''
try {
    $out = (Install-FromBundle -Name 'demo' -Source $bundle -Dest $dest -BackupRoot (Join-Path $root '.settings-backup') *>&1 | Out-String)
} catch { $err = $_.Exception.Message }
Check 'a folder that cannot be moved aside is still updated' ($err -eq '')
Check 'and you get this version, not the last one' (
    (Get-Content (Join-Path $dest 'run-all.ps1') -Raw).Trim() -eq '# v-new')
Check 'including files new in it' (Test-Path (Join-Path $dest 'serve-console.py'))
Check 'your settings are untouched' ((Get-Content (Join-Path $dest 'alerts.ini') -Raw) -like '*MINE*')
Check 'and were copied somewhere you can find' (
    Test-Path (Join-Path (Join-Path $root '.settings-backup') 'demo/alerts.ini'))
Check 'it says it wrote in place rather than pretending it was a clean swap' (
    $out -like '*updated in place*')
Check 'and admits what writing in place cannot do' ($out -like '*may still*')
Check 'nothing is left half-renamed' (
    @(Get-ChildItem -Path $tools -Directory -Filter 'demo.replaced-*').Count -eq 0)

Write-Host ''
Write-Host '-- 5b. the fresh copy itself fails: put back what was there'
# The other half of renaming aside rather than deleting. If the bundle copy
# cannot land, the tool folder must not be left renamed away and the install
# broken - it goes back exactly as it was.
$global:LockedName = $null
$root = Join-Path $work 'e'; $tools = Join-Path $root 'tools'; $dest = Join-Path $tools 'demo'
$null = New-Item -ItemType Directory -Path $tools -Force
New-Installed $dest
$before = @(Get-ChildItem -Path $dest -Recurse | ForEach-Object { $_.Name }) | Sort-Object
$err = ''
try {
    Install-FromBundle -Name 'demo' -Source (Join-Path $work 'no-such-bundle') -Dest $dest -BackupRoot (Join-Path $root '.settings-backup') | Out-Null
    $err = '(it did not fail at all)'
} catch { $err = $_.Exception.Message }
Check 'a copy that cannot happen is reported' ($err -like '*could not be copied from the bundle*')
Check 'the tool folder is back where it belongs' (Test-Path $dest)
Check 'with everything that was in it' (
    ((@(Get-ChildItem -Path $dest -Recurse | ForEach-Object { $_.Name }) | Sort-Object) -join '|') -eq ($before -join '|'))
Check 'your settings still say what you set' ((Get-Content (Join-Path $dest 'alerts.ini') -Raw) -like '*MINE*')
Check 'it says the copy you had was put back' ($err -like '*put back*')
Check 'nothing is left renamed aside' (@(Get-ChildItem -Path $tools -Directory -Filter 'demo.replaced-*').Count -eq 0)

Write-Host ''
Write-Host '-- 6. ONE tool that cannot be replaced does not take the install down'
$global:LockedName = 'print-fleet-dashboard'
$global:FailMoveTimes = 0
$root = Join-Path $work 'd'
$stage = Join-Path $work 'd-stage'
$REPOS = @('entra-tenant-docs', 'entra-security-snapshot', 'm365-license-waste-report',
           'print-fleet-dashboard', 'it-ops-console')
$null = New-Item -ItemType Directory -Path (Join-Path $stage 'tools') -Force
foreach ($r in $REPOS) { New-Bundle (Join-Path (Join-Path $stage 'tools') $r) }
Copy-Item $setup (Join-Path $stage 'setup.ps1')
# an existing install of every tool, so each one is an UPDATE
$null = New-Item -ItemType Directory -Path (Join-Path $root 'tools') -Force
foreach ($r in $REPOS) { New-Installed (Join-Path (Join-Path $root 'tools') $r) }
# ...and in ONE of them, a file that cannot be written either, so it is a real
# hard failure rather than an in-place update
$null = New-Item -ItemType Directory -Force -Path (
    Join-Path (Join-Path (Join-Path $root 'tools') 'print-fleet-dashboard') 'serve-console.py')
# data already collected, so setup has something to rebuild the pages from
$null = New-Item -ItemType Directory -Path (Join-Path $root 'output') -Force
Set-Content (Join-Path $root 'output/tenant.json') '{}'
# Run the WHOLE of setup in this scope, so the refusing Move-Item above is what
# it calls. -Unattended answers every question and skips the schedule step.
$text = (& (Join-Path $stage 'setup.ps1') -Root $root -Unattended *>&1 | Out-String)
$global:LockedName = $null
Check 'setup still runs to the end' ($text -like '*Setup complete*')
Check 'it names the one tool it could not update' ($text -like '*print-fleet-dashboard was NOT updated*')
# Not "Refresh IT Ops Data" - setup's ordinary closing text says that too, and a
# check that passes on a run where nothing failed is worse than no check.
Check 'and says what to close' ($text -like '*File Explorer window*')
Check 'the other four DID update' (
    @($REPOS | Where-Object {
        $_ -ne 'print-fleet-dashboard' -and
        (Get-Content (Join-Path (Join-Path $root 'tools') "$_/run-all.ps1") -Raw).Trim() -eq '# v-new'
    }).Count -eq 4)
Check 'the one that failed is untouched, old version and all' (
    (Get-Content (Join-Path (Join-Path $root 'tools') 'print-fleet-dashboard/run-all.ps1') -Raw).Trim() -eq '# v-old')
Check 'and it named the file, not just the folder' ($text -like '*serve-console.py*')
Check 'and sums it up in one line at the end' ($text -like '*Not updated: print-fleet-dashboard*')
Check 'it says the settings are safe' ($text -like '*settings are safe*')
Check 'every settings file came through, updated or not' (
    @($REPOS | Where-Object {
        (Get-Content (Join-Path (Join-Path $root 'tools') "$_/alerts.ini") -Raw) -like '*MINE*'
    }).Count -eq $REPOS.Count)

# The pages a person looks at are BUILT. After an update the ones on disk were
# made by the PREVIOUS version, so its buttons and wording are what they see
# until something rebuilds them - which normally means waiting for a refresh.
# That is how a whole afternoon went on "the Apply button does not work".
$built = Join-Path $root 'console-site/built.txt'
Check 'setup rebuilt the console pages, so they match what it just installed' (Test-Path $built)
$args_ = if (Test-Path $built) { Get-Content $built -Raw } else { '' }
Check 'it built from the wiring file it just wrote' ($args_ -like '*--config sources.ini*')
Check 'into the console folder people actually open' ($args_ -like "*--out*console-site*")
Check 'and ran it from the tool folder, so relative paths mean what they say' (
    $args_ -like '*cwd=*it-ops-console*')

Write-Host ''
Write-Host '-- 7. a holder that is only passing through: try again before giving up'
$global:LockedName = $null
$global:FailMoveTimes = 2
$root = Join-Path $work 'f'; $tools = Join-Path $root 'tools'
$bundle = Join-Path $work 'f-bundle'; $dest = Join-Path $tools 'demo'
$null = New-Item -ItemType Directory -Path $tools -Force
New-Bundle $bundle; New-Installed $dest
Install-FromBundle -Name 'demo' -Source $bundle -Dest $dest -BackupRoot (Join-Path $root '.settings-backup') | Out-Null
Check 'it kept trying instead of failing on the first refusal' ($global:FailMoveTimes -eq 0)
Check 'and the update went through normally' (
    (Get-Content (Join-Path $dest 'run-all.ps1') -Raw).Trim() -eq '# v-new')
Check 'a file deleted upstream still goes, so it really was the whole-folder path' (
    -not (Test-Path (Join-Path $dest 'apply-settings.ps1')))
Check 'your settings survived' ((Get-Content (Join-Path $dest 'alerts.ini') -Raw) -like '*MINE*')

Write-Host ''
Write-Host '-- 8. held for real, AND what is installed is already broken'
# Refusing outright is right while what is there still works. It is the wrong
# answer when a half-finished update already broke it - refusing for ever is
# then the same as never fixing it. This is the case that stranded a real
# machine: folder held open, and run-all.ps1 already gone.
$global:FailMoveTimes = 0
$global:LockedName = 'demo'
$root = Join-Path $work 'g'; $tools = Join-Path $root 'tools'
$bundle = Join-Path $work 'g-bundle'; $dest = Join-Path $tools 'demo'
$null = New-Item -ItemType Directory -Path $tools -Force
New-Bundle $bundle; New-Installed $dest
Remove-Item (Join-Path $dest 'run-all.ps1')          # the half-deleted state
Remove-Item (Join-Path $dest 'console/pages.py')
$err = ''
try {
    Install-FromBundle -Name 'demo' -Source $bundle -Dest $dest -BackupRoot (Join-Path $root '.settings-backup') | Out-Null
} catch { $err = $_.Exception.Message }
Check 'it did NOT give up on a folder nothing else can fix' ($err -eq '')
Check 'the missing file is back' (Test-Path (Join-Path $dest 'run-all.ps1'))
Check 'at the new version' ((Get-Content (Join-Path $dest 'run-all.ps1') -Raw).Trim() -eq '# v-new')
Check 'the missing subfolder file is back too' (
    (Get-Content (Join-Path $dest 'console/pages.py') -Raw).Trim() -eq '# v-new')
Check 'a file new in this version arrived' (Test-Path (Join-Path $dest 'serve-console.py'))
Check 'your settings were not touched' ((Get-Content (Join-Path $dest 'alerts.ini') -Raw) -like '*MINE*')
Check 'nothing was renamed aside, because nothing could be' (
    @(Get-ChildItem -Path $tools -Directory -Filter 'demo.replaced-*').Count -eq 0)
# writing in place cannot remove what this version dropped - say so, do not hide it
Check 'a file this version removed is still there (in-place cannot delete)' (
    Test-Path (Join-Path $dest 'apply-settings.ps1'))

Write-Host ''
Write-Host '-- 9. held for real, and ONE FILE cannot be written: change nothing, name it'
# The half-written folder is the one outcome worse than doing nothing, so the
# in-place path asks the OS first. Here a directory sits where a file belongs,
# which is a thing [IO.File]::Open cannot open for writing - the same answer it
# gives for a file another program is holding.
$global:LockedName = 'demo'
$root = Join-Path $work 'h'; $tools = Join-Path $root 'tools'
$bundle = Join-Path $work 'h-bundle'; $dest = Join-Path $tools 'demo'
$null = New-Item -ItemType Directory -Path $tools -Force
New-Bundle $bundle; New-Installed $dest
Remove-Item (Join-Path $dest 'run-all.ps1')
$null = New-Item -ItemType Directory -Path (Join-Path $dest 'serve-console.py') -Force
$before = @(Get-ChildItem -Path $dest -Recurse | ForEach-Object { $_.Name + '|' + $_.Length }) | Sort-Object
$err = ''
try {
    Install-FromBundle -Name 'demo' -Source $bundle -Dest $dest -BackupRoot (Join-Path $root '.settings-backup') | Out-Null
    $err = '(it did not fail at all)'
} catch { $err = $_.Exception.Message }
Check 'it refuses rather than writing half a folder' (
    $err -ne '' -and $err -ne '(it did not fail at all)')
Check 'it names the FILE, not just "the folder"' ($err -like '*serve-console.py*')
Check 'it says what to do about that file' ($err -like '*Close whatever has that file*')
Check 'it says nothing was changed' ($err -like '*nothing was changed*')
Check 'and nothing WAS changed' (
    ((@(Get-ChildItem -Path $dest -Recurse | ForEach-Object { $_.Name + '|' + $_.Length }) | Sort-Object) -join ',') -eq ($before -join ','))
Check 'your settings are still there' ((Get-Content (Join-Path $dest 'alerts.ini') -Raw) -like '*MINE*')
Check 'and were copied somewhere you can find' (
    Test-Path (Join-Path (Join-Path $root '.settings-backup') 'demo/alerts.ini'))
$global:LockedName = $null

Write-Host ''
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
if ($fails.Count) { Write-Host "RESULT: $($fails.Count) FAILURES"; $fails | ForEach-Object { Write-Host "  - $_" }; exit 1 }
Write-Host 'RESULT: ALL PASS'
exit 0
