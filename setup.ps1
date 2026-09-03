<#
.SYNOPSIS
    One-file setup for the IT ops console and the four tools that feed it.

.DESCRIPTION
    For the admin who wants the console without assembling it by hand. Run this
    one script and it will:

      1. Create the folder layout (default C:\IT-Ops)
      2. Download the five tools from GitHub - no git needed
      3. Install the Microsoft Graph PowerShell modules the collectors use
      4. Check for Python (the console page builder needs it) and offer to
         install it if it is missing
      5. Write the console's sources.ini so everything already points at
         everything else
      6. Put two shortcuts on your desktop:
           "IT Ops Console"        starts the console on this computer and
                                   opens it in your browser. Its "Refresh now"
                                   and "Apply settings" buttons then work -
                                   there is nothing else to double-click
           "Refresh IT Ops Data"   runs every collector (you sign in), then
                                   rebuilds the console, with live progress
      7. Ask how the console should stay fresh: you click Refresh yourself,
         it refreshes daily while you are signed in, or it refreshes daily
         unattended (a Global Administrator registers a read-only app once)
      8. Offer to run that first collection right away - press Enter and
         you go from "installed" to looking at your own data in one motion

    Everything the collectors do against your tenant is READ-ONLY. Nothing
    here stores a password - you sign in interactively when data is collected,
    or, if you choose it, a certificate that never leaves this computer does.

    Works in Windows PowerShell 5.1 (what "right-click > Run with PowerShell"
    gives you) and in PowerShell 7.

.PARAMETER Root
    Where everything lives. Default: C:\IT-Ops
    (tools\ for the downloaded repos, output\ for collected data,
     console-site\ for the built pages)

.PARAMETER Unattended
    No prompts: accept every default, skip the Python install offer, leave the
    automatic-refresh choice as it is, and skip the run-everything-now offer.
    For scripted or repeated setups.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\IT-Ops',
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'

# Everything this run prints also lands in setup.log beside this script, so
# "did it work?" always has an answer, even after the window is gone.
try { Start-Transcript -Path (Join-Path $PSScriptRoot 'setup.log') -Force | Out-Null } catch { }

# GitHub requires TLS 1.2+; stock Windows PowerShell 5.1 does not always offer
# it by default, and the failure it produces looks nothing like the cause.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072

$REPOS = @('entra-tenant-docs', 'entra-security-snapshot', 'm365-license-waste-report',
           'print-fleet-dashboard', 'it-ops-console')
$MODULES = @('Microsoft.Graph.Authentication', 'Microsoft.Graph.Users',
             'Microsoft.Graph.Identity.DirectoryManagement')
$onWindows = ($env:OS -eq 'Windows_NT')

function Read-Default {
    param([string]$Prompt, [string]$Default)
    if ($Unattended) { return $Default }
    $v = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($v)) { $Default } else { $v }
}

function Ask-YesNo {
    param([string]$Prompt, [bool]$DefaultYes = $true)
    if ($Unattended) { return $DefaultYes }
    $suffix = if ($DefaultYes) { '[Y/n]' } else { '[y/N]' }
    $v = Read-Host "$Prompt $suffix"
    if ([string]::IsNullOrWhiteSpace($v)) { return $DefaultYes }
    return $v -match '^[Yy]'
}

Write-Host ''
Write-Host '=== IT Ops Console setup ==============================================' 
Write-Host ''
Write-Host 'This will download five small open-source tools, wire them together,'
Write-Host 'and put two shortcuts on your desktop. Collection against your tenant'
Write-Host 'is read-only, and you sign in yourself - nothing stores a password.'
Write-Host ''
Write-Host 'It asks ONE question now and two at the end. When the window pauses,'
Write-Host 'it is waiting for you - pressing Enter accepts the suggested answer.'
Write-Host ''

$Root = Read-Default 'Install folder - press Enter to accept' $Root
Write-Host ''
Write-Host "Setting up in $Root. The rest runs on its own - takes a minute or two."
$tools = Join-Path $Root 'tools'
$output = Join-Path $Root 'output'
$site = Join-Path $Root 'console-site'
foreach ($d in @($Root, $tools, $output, $site)) { $null = New-Item -ItemType Directory -Path $d -Force }

# --- Lock the install folder to you + admins ------------------------------- #
# Everything the tools collect lands under here - admin UPNs, stale-account
# lists, the app inventory. A folder under C:\ is readable by every local user
# by default; that read access is the whole exposure. Restrict it to the
# installing user, Administrators and SYSTEM, and let children inherit. Done
# before the tools are written, so everything created afterward inherits it.
# Best effort: a machine where this can't be set still installs, with a warning.
if ($onWindows) {
    try {
        # Start from the folder's OWN descriptor rather than a new, empty
        # DirectorySecurity. A fresh descriptor carries an empty audit section,
        # so Set-Acl tries to write the SACL too - and THAT needs
        # SeSecurityPrivilege, which an ordinary un-elevated run does not have.
        # The lock then fails with a privilege error nobody can act on, and the
        # collected data stays readable by every local user. Modifying what
        # Get-Acl returns writes the permissions only, and needs nothing beyond
        # owning the folder.
        $acl = Get-Acl -Path $Root
        $acl.SetAccessRuleProtection($true, $false)     # drop inherited ACEs
        foreach ($rule in @($acl.Access)) {
            if (-not $rule.IsInherited) { $null = $acl.RemoveAccessRuleSpecific($rule) }
        }
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $full = [System.Security.AccessControl.FileSystemRights]::FullControl
        $inh  = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
        $none = [System.Security.AccessControl.PropagationFlags]::None
        $allow = [System.Security.AccessControl.AccessControlType]::Allow
        # SIDs, not "BUILTIN\Administrators" strings - language-independent.
        foreach ($id in @($me,
            (New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'),  # Administrators
            (New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-18'))) {   # SYSTEM
            $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
                $id, $full, $inh, $none, $allow)))
        }
        Set-Acl -Path $Root -AclObject $acl
        Write-Host "  locked $Root to you + Administrators - other local users can't read collected data"
    } catch {
        Write-Warning "  could not lock $Root down ($($_.Exception.Message))"
        Write-Warning "  The data collected here - admin names, stale accounts, the app inventory -"
        Write-Warning "  is readable by other people who use this computer until that is fixed."
        Write-Warning "  Right-click the folder > Properties > Security, or see the README."
    }
}

# --------------------------------------------------------------------------- #
Write-Host ''
# A release bundle ships the tools right next to this script; installing from
# it needs no internet at all. A bare setup.ps1 downloads instead.
$bundleTools = Join-Path $PSScriptRoot 'tools'
$haveBundle = Test-Path $bundleTools
$bundleIsInstall = $haveBundle -and ((Resolve-Path $bundleTools).Path -eq (Resolve-Path $tools).Path)
if ($haveBundle -and -not $bundleIsInstall) {
    $v = Join-Path $PSScriptRoot 'VERSION'
    $vTxt = if (Test-Path $v) { " v$((Get-Content $v -TotalCount 1).Trim())" } else { '' }
    Write-Host "--- 1/6 Installing the tools from the bundle$vTxt (no download needed) ---"
} else {
    Write-Host '--- 1/6 Downloading the tools ---'
}
function Get-BundleFileList {
    # Every file the bundle would write, as paths relative to the tool folder.
    param([string]$Source)
    $n = $Source.Length
    @(Get-ChildItem -LiteralPath $Source -Recurse -File | ForEach-Object {
        $_.FullName.Substring($n).TrimStart('\', '/')
    })
}

function Get-HeldFile {
    <# The first file that cannot be opened for writing, or $null.

       This asks the OS the same question the copy is about to ask, before the
       copy starts. It is what turns "something is using the folder" into "this
       one file is open in something", which a person can actually act on - and
       it is what makes writing in place safe: a half-written tool folder is the
       one outcome worse than either doing nothing or replacing it whole. #>
    param([string]$Dest, [string[]]$Relative)
    foreach ($rel in $Relative) {
        $p = Join-Path $Dest $rel
        if (-not (Test-Path -LiteralPath $p)) { continue }   # new file, nothing to hold
        try {
            $fs = [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $fs.Close(); $fs.Dispose()
        } catch { return $p }
    }
    return $null
}

function Copy-BundleOverTop {
    # Write the bundle's files into the folder that is already there, one by
    # one, then check every one arrived. Nothing is deleted, so a file this
    # version dropped stays behind - said out loud by the caller.
    param([string]$Source, [string]$Dest, [string[]]$Relative)
    foreach ($rel in $Relative) {
        $to = Join-Path $Dest $rel
        $dir = Split-Path $to -Parent
        if (-not (Test-Path -LiteralPath $dir)) { $null = New-Item -ItemType Directory -Path $dir -Force }
        Copy-Item -LiteralPath (Join-Path $Source $rel) -Destination $to -Force -ErrorAction Stop
    }
    $bad = @()
    foreach ($rel in $Relative) {
        $a = Get-Item -LiteralPath (Join-Path $Source $rel)
        $b = Get-Item -LiteralPath (Join-Path $Dest $rel) -ErrorAction SilentlyContinue
        if (-not $b -or $b.Length -ne $a.Length) { $bad += $rel }
    }
    return $bad
}

function Install-FromBundle {
    <#
      Replace one tool folder with the bundle's copy, keeping the settings files
      that live in it.

      The old folder is RENAMED ASIDE, never deleted in place. Anything on this
      computer holding it open - a PowerShell window left over from a refresh,
      the console while it is serving itself, an Explorer window sitting in it -
      made "delete then copy" fail HALF WAY: some files already gone, the fresh
      copy never made, and the kept settings stranded in a temp folder nobody
      would ever find. A rename either works or it does not, and when it does
      not, nothing has been touched at all.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Dest,
        [Parameter(Mandatory)][string]$BackupRoot
    )
    $keep = @(); $stash = $null; $aside = $null
    if (Test-Path $Dest) {
        # Your settings survive an update: any .ini you edited (config.ini,
        # prices.ini, sources.ini, alerts.ini) and any database in the folder.
        # The bundle only ever ships the *.example.ini templates, so without
        # this an update would silently replace your printer list with the
        # example and the printers vanish.
        $keep = @(Get-ChildItem -LiteralPath $Dest -File | Where-Object {
            ($_.Extension -eq '.ini' -and $_.Name -notlike '*.example.ini') -or $_.Extension -eq '.db' })
        if ($keep.Count) {
            # Beside the install, not in %TEMP%: if anything goes wrong from
            # here these are somewhere a person would actually look. Rebuilt
            # each run, so it is always "your settings before this update" -
            # never a pile of old ones, and never a file you deleted coming back.
            $stash = Join-Path $BackupRoot $Name
            if (Test-Path $stash) { Remove-Item -LiteralPath $stash -Recurse -Force }
            $null = New-Item -ItemType Directory -Path $stash -Force
            $keep | Copy-Item -Destination $stash -Force
        }
        $aside = "$Dest.replaced-$(Get-Date -Format yyyyMMdd-HHmmss)"
        # A folder can be held for a moment by something only passing through -
        # a virus scanner reading what the last tool just wrote, the search
        # indexer. Three tries over a few seconds gets past those and costs
        # nothing when nothing is wrong.
        $renamed = $false
        for ($try = 1; $try -le 3; $try++) {
            try { Move-Item -LiteralPath $Dest -Destination $aside -ErrorAction Stop; $renamed = $true; break }
            catch { if ($try -lt 3) { Start-Sleep -Seconds 2 } }
        }
        if (-not $renamed) {
            $aside = $null
            $where = if ($stash) { " Your settings are also copied to $stash." } else { '' }
            # Refusing outright looked right, and is not: it leaves a folder a
            # half-finished update already broke with no way back, since the
            # same thing holds it every time. So write the fresh copy over the
            # top instead - and only after the OS confirms every file can be
            # written, because a half-written folder is the one outcome worse
            # than either doing nothing or replacing the lot.
            $rel = Get-BundleFileList -Source $Source
            $held = Get-HeldFile -Dest $Dest -Relative $rel
            if ($held) {
                throw ("$held is open in another program, so $Name could not be replaced " +
                       "- and nothing was changed. That is usually a PowerShell window left " +
                       'open by "Refresh IT Ops Data", the "IT Ops Console" window while the ' +
                       'console is running, or a File Explorer window showing that folder. ' +
                       "Close whatever has that file, then run this setup again.$where")
            }
            $bad = Copy-BundleOverTop -Source $Source -Dest $Dest -Relative $rel
            if ($bad.Count) {
                throw ("$Name is only part replaced - these did not land: $($bad -join ', '). " +
                       "Close anything using $Dest and run this setup again.$where")
            }
            Write-Host "  updated in place: $Name"
            Write-Host "    (the folder is in use, so the old copy could not be moved aside first."
            Write-Host "     Everything this version ships is now there; a file it REMOVED may still"
            Write-Host "     be too. Harmless - close what is using the folder and re-run to tidy.)"
            return
        }
    }
    try {
        Copy-Item -LiteralPath $Source -Destination $Dest -Recurse -ErrorAction Stop
    } catch {
        # The fresh copy did not land. Put back exactly what was there rather
        # than leaving the tool folder renamed aside and the install broken.
        # Anything in $Dest at this point came from the bundle - the settings
        # have not been put back yet - so clearing a half-copy loses nothing
        # of yours.
        if ($aside -and (Test-Path $aside)) {
            if (Test-Path $Dest) { Remove-Item -LiteralPath $Dest -Recurse -Force -ErrorAction SilentlyContinue }
            if (-not (Test-Path $Dest)) { Move-Item -LiteralPath $aside -Destination $Dest -ErrorAction SilentlyContinue }
        }
        $back = if ($aside -and (Test-Path $Dest)) { ' The copy you had has been put back.' } else { '' }
        $where = if ($stash) { " Your settings are also copied to $stash." } else { '' }
        throw "$Name could not be copied from the bundle ($($_.Exception.Message)).$back$where"
    }
    if ($keep.Count) {
        Get-ChildItem -LiteralPath $stash -File | Copy-Item -Destination $Dest -Force
        Write-Host "  installed from bundle: $Name  (kept your $($keep.Name -join ', '))"
    } else {
        Write-Host "  installed from bundle: $Name"
    }
    # From here the fresh copy is in place and correct. Clearing the old one
    # away is tidying, not installing - if something still holds it, say so and
    # carry on rather than failing an update that already worked.
    if ($aside -and (Test-Path $aside)) {
        try { Remove-Item -LiteralPath $aside -Recurse -Force -ErrorAction Stop }
        catch { Write-Host "  (the previous copy is still in use - left at $aside; delete it whenever you like)" }
    }
}

$notUpdated = @()
foreach ($r in $REPOS) {
    $dest = Join-Path $tools $r
    $isConsole = ($r -eq 'it-ops-console')
    $present = Test-Path $dest
    $bundled = Join-Path $bundleTools $r
    $fromBundle = $haveBundle -and -not $bundleIsInstall -and (Test-Path $bundled)
    # A release bundle IS the update: every tool refreshes from it, with your
    # settings kept (see below). Without a bundle, only the console refreshes -
    # it is the renderer and carries no data of yours (this setup rewrites its
    # sources.ini) - and the other tools stay as installed.
    if ($present -and -not $isConsole -and -not $fromBundle) {
        Write-Host "  already present: $r  (install from a release bundle to update it)"
        continue
    }
    if ($present -and $isConsole -and $PSScriptRoot -and
        (Resolve-Path $PSScriptRoot).Path -eq (Resolve-Path $dest).Path) {
        Write-Host "  already present: it-ops-console (setup is running from inside it)"
        continue
    }
    if ($present) { Write-Host "  refreshing: $r" } else { Write-Host "  fetching: $r" }
    if ($fromBundle) {
        # One tool that cannot be replaced must not take the rest of the install
        # down with it: say what is wrong, in words that name what to close, and
        # carry on. Everything else still updates.
        try {
            Install-FromBundle -Name $r -Source $bundled -Dest $dest -BackupRoot (Join-Path $Root '.settings-backup')
        } catch {
            Write-Warning "  $r was NOT updated. $($_.Exception.Message)"
            $notUpdated += $r
        }
        continue
    }
    $zip = Join-Path $tools "$r.zip"
    try {
        Invoke-WebRequest "https://github.com/JonathanT10/$r/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
        Expand-Archive $zip -DestinationPath $tools -Force
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        Rename-Item (Join-Path $tools "$r-main") $dest
        Remove-Item $zip
    } catch {
        foreach ($leftover in @((Join-Path $tools "$r-main"), $zip)) {
            if (Test-Path $leftover) { Remove-Item $leftover -Recurse -Force -ErrorAction SilentlyContinue }
        }
        if (Test-Path $dest) {
            Write-Warning "  could not refresh $r - keeping the copy you have. ($($_.Exception.Message))"
        } else { throw }
    }
}

if ($notUpdated.Count) {
    Write-Host ''
    Write-Warning "  Not updated: $($notUpdated -join ', '). Everything else was, and your"
    Write-Warning "  settings are safe. Close what is named above and run this setup again."
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 2/6 Microsoft Graph PowerShell modules ---'
$toInstall = @($MODULES | Where-Object { -not (Get-Module -ListAvailable -Name $_) })
if ($toInstall.Count -eq 0) {
    Write-Host '  all present.'
} else {
    Write-Host "  installing: $($toInstall -join ', ')  (user scope, from the PowerShell Gallery)"
    try {
        if (-not (Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue)) {
            $null = Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope CurrentUser
        }
        foreach ($m in $toInstall) { Install-Module $m -Scope CurrentUser -Force -AllowClobber }
        Write-Host '  done.'
    } catch {
        Write-Warning "  Module install failed: $($_.Exception.Message)"
        Write-Warning '  You can install them yourself later:  Install-Module Microsoft.Graph -Scope CurrentUser'
    }
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 3/6 Python (builds the console pages) ---'
function Get-PythonVersionText {
    # Probe one candidate and return whatever it printed, as plain text.
    # The Microsoft Store ships a stub python.exe (an "App execution alias")
    # that only opens the Store: it prints "Python was not found..." to STDERR
    # and exits. Under Windows PowerShell 5.1 that stderr line surfaces as a red
    # NativeCommandError in the middle of setup - alarming to a reader, and it
    # aborts the probing statement. So let cmd.exe run the probe and merge
    # stderr into stdout itself: PowerShell then only ever sees strings.
    param([string]$Candidate)
    $ErrorActionPreference = 'Continue'   # function-scoped; the caller's 'Stop' is untouched
    try {
        if ($onWindows) { return ((& cmd.exe /d /c "$Candidate --version 2>&1" | Out-String).Trim()) }
        return ((& $Candidate --version 2>&1 | Out-String).Trim())   # no Store stub off Windows
    } catch { return '' }
}
function Get-WorkingPython {
    # A real interpreter answers --version with "Python 3.x"; the Store stub and
    # anything else do not. The verdict rests on that text alone.
    foreach ($cand in @('python', 'python3', 'py')) {
        if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
        if ((Get-PythonVersionText $cand) -match 'Python 3') { return $cand }
    }
    return $null
}
$python = Get-WorkingPython
if ($python) {
    Write-Host "  found: $python ($(Get-PythonVersionText $python))"
} else {
    Write-Warning '  Python 3 not found. The collectors still work without it, but the'
    Write-Warning '  console pages cannot be built until it is installed.'
    if ($onWindows -and (Get-Command winget -ErrorAction SilentlyContinue) -and (Ask-YesNo '  Install Python 3 now with winget?' $false)) {
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        $python = Get-WorkingPython
        if ($python) { Write-Host "  installed: $python" }
        else { Write-Warning '  Still not on PATH - close this window, reopen PowerShell, and re-run setup.' }
    } else {
        Write-Warning '  Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),'
        Write-Warning '  then re-run this setup.'
    }
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 4/6 Wiring the console to the tools ---'
$consoleDir = Join-Path $tools 'it-ops-console'
$sources = @"
# Written by setup.ps1 on $(Get-Date -Format yyyy-MM-dd). Safe to edit.
[console]
base_path = $output

[sources]
tenant      = tenant-docs\tenant.json
run_summary = tenant-docs\run-summary.json
history     = tenant-docs\history
security    = security-snapshot.json
licensing   = licensing.json
fleet       = fleet.db
# Refresh archives each run here, so the console can show posture over time.
security_history  = history\security
licensing_history = history\licensing
# Refresh writes this each run: how it signed in, the schedule, certificate days left.
refresh_status    = refresh-status.json
# The printer collector writes this when config.ini names places to look.
fleet_discovery   = fleet-discovery.json
"@
Set-Content -Path (Join-Path $consoleDir 'sources.ini') -Value $sources -Encoding UTF8
Write-Host "  wrote $((Join-Path $consoleDir 'sources.ini'))"

$pfd = Join-Path $tools 'print-fleet-dashboard'
if ((Test-Path (Join-Path $pfd 'config.example.ini')) -and -not (Test-Path (Join-Path $pfd 'config.ini'))) {
    Copy-Item (Join-Path $pfd 'config.example.ini') (Join-Path $pfd 'config.ini')
    Write-Host "  printers are OPTIONAL: to add them later, put their IPs in $((Join-Path $pfd 'config.ini'))"
}
# Alerts start with every rule at its default and no channel: nothing is sent
# until a person pastes a Teams Workflows URL (or a mail relay) into the file.
if ((Test-Path (Join-Path $consoleDir 'alerts.example.ini')) -and -not (Test-Path (Join-Path $consoleDir 'alerts.ini'))) {
    Copy-Item (Join-Path $consoleDir 'alerts.example.ini') (Join-Path $consoleDir 'alerts.ini')
    Write-Host "  alerts are OPTIONAL: to get Teams or email messages, edit $((Join-Path $consoleDir 'alerts.ini'))"
}

# The console's pages are BUILT. After an update the ones sitting on disk were
# made by the PREVIOUS version, so its wording and its buttons are what you see
# until something rebuilds them - which normally means waiting for the next
# refresh. Do it here, when there is data to build from, so an upgrade is not
# invisible.
if ($python -and (Test-Path (Join-Path $consoleDir 'build.py')) -and
    @(Get-ChildItem -Path $output -ErrorAction SilentlyContinue).Count) {
    $was = Get-Location
    try {
        Set-Location $consoleDir
        $null = & $python 'build.py' '--config' 'sources.ini' '--out' $site 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Host '  rebuilt the console pages, so they match this version' }
        else { Write-Warning "  the console pages could not be rebuilt (build.py said $LASTEXITCODE) - the next refresh will." }
    } catch {
        Write-Warning "  the console pages could not be rebuilt ($($_.Exception.Message)) - the next refresh will."
    } finally { Set-Location $was }
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 5/6 Desktop shortcuts ---'
if ($onWindows) {
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('Desktop')
    $iconOpensFilesOnly = $false

    # The console is SERVED from this computer now, by a small local server this
    # icon starts - 127.0.0.1 only, so nothing else on the network can reach it.
    # That is what makes "Refresh now" and "Apply settings" real buttons on the
    # page: a page opened straight off the disk cannot write anything here, which
    # is why applying a setting used to mean copying text and hunting an icon.
    # Pressing this icon again opens the console you already have, not a second.
    $serve = Join-Path $consoleDir 'serve-console.py'
    $pyExe = if ($python) { (Get-Command $python -ErrorAction SilentlyContinue).Source } else { $null }
    $lnk = $shell.CreateShortcut((Join-Path $desktop 'IT Ops Console.lnk'))
    if ($pyExe -and (Test-Path $serve)) {
        $lnk.TargetPath = $pyExe
        $lnk.Arguments = "`"$serve`" --site `"$site`" --tool-root `"$tools`" --output-root `"$output`" --python $python --open"
        # NOT the console folder. A running program's working directory cannot be
        # deleted or renamed on Windows, and this server runs for as long as the
        # console is open - pointing it at the folder setup replaces would make
        # every upgrade collide with the console itself. Nothing here needs that
        # folder as its working directory: serve-console.py finds itself.
        $lnk.WorkingDirectory = $Root
        $lnk.Description = 'Start the IT ops console on this computer and open it'
    } else {
        # Without Python there is no server - and no rebuilt pages either, since
        # Python is what builds them. The icon still opens whatever was built,
        # and the page itself says why its buttons cannot do anything.
        $lnk.TargetPath = Join-Path $site 'index.html'
        $lnk.Description = 'Open the IT ops console pages as files (its buttons will not work)'
        $iconOpensFilesOnly = $true
    }
    $lnk.Save()

    $runArgs = "-NoProfile -NoExit -ExecutionPolicy Bypass -File `"$consoleDir\run-all.ps1`" -ToolRoot `"$tools`" -OutputRoot `"$output`" -SitePath `"$site`""
    if ($python) { $runArgs += " -Python $python" }
    $lnk = $shell.CreateShortcut((Join-Path $desktop 'Refresh IT Ops Data.lnk'))
    $lnk.TargetPath = 'powershell.exe'
    $lnk.Arguments = $runArgs
    # Same reason, and this one bit for real: the shortcut runs with -NoExit, so
    # its window stays open afterwards - with the console folder as its working
    # directory, one left open from last week is enough to block an upgrade.
    # run-all.ps1 is launched by full path and uses $PSScriptRoot, so it does
    # not care where the window started.
    $lnk.WorkingDirectory = $Root
    $lnk.Description = 'Run every collector (you sign in), then rebuild the console'
    $lnk.Save()

    # There used to be a third icon for applying settings, because a page opened
    # off the disk could not do it. The console's own "Apply settings" button
    # does it now, so the icon is removed rather than left on the desktop
    # teaching a ritual that is no longer needed.
    foreach ($gone in @('Apply Settings.lnk', 'Apply Alert Settings.lnk')) {
        $old = Join-Path $desktop $gone
        if (Test-Path $old) {
            Remove-Item $old -Force -ErrorAction SilentlyContinue
            Write-Host ("  removed the old '{0}' icon - the console's own Apply settings button does that now" -f [IO.Path]::GetFileNameWithoutExtension($gone))
        }
    }
    Write-Host '  created: "IT Ops Console" and "Refresh IT Ops Data"'
    # Saying "created" and stopping there would be a lie by omission: that icon
    # is not the console, it just opens its pages, and every button on them is
    # dead. Better a loud paragraph now than someone clicking Apply all week.
    if ($iconOpensFilesOnly) {
        Write-Warning '  BUT the "IT Ops Console" icon can only OPEN the pages, not run the console:'
        if (-not $pyExe) {
            Write-Warning '    Python 3 was not found, and the console is built and served by it.'
        } else {
            Write-Warning "    serve-console.py is missing from $consoleDir."
        }
        Write-Warning '    Opened that way, "Refresh now" and "Apply settings" cannot do anything.'
        Write-Warning '    The pages now say so at the top. Fix what is named above, re-run setup,'
        Write-Warning '    and the icon will start the console properly.'
    }
} else {
    Write-Host '  (not Windows - skipping shortcuts)'
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 6/6 Keeping the console fresh ---'
# One question, three answers, handled by schedule-refresh.ps1 (which can also
# be run again later on its own). It writes automatic-refresh.ini beside
# run-all.ps1 and, for answers 2 and 3, one Task Scheduler job. Unattended
# setups never change this - a schedule is a person's decision.
$scheduler = Join-Path $consoleDir 'schedule-refresh.ps1'
if ($Unattended) {
    Write-Host '  automatic refresh: left as it is (run setup without -Unattended to choose)'
} elseif (-not $onWindows) {
    Write-Host '  (not Windows - automatic refresh uses Task Scheduler; skipping)'
} elseif (-not (Test-Path $scheduler)) {
    Write-Warning "  schedule-refresh.ps1 is missing from $consoleDir - re-run setup from a current release bundle to add automatic refresh."
} else {
    # A hashtable, not an array: array splatting hands a script its elements
    # positionally, so '-Root' would land IN the Root parameter.
    $schedArgs = @{ Root = $Root }
    if ($python) { $schedArgs['Python'] = $python }
    try { & $scheduler @schedArgs } catch { Write-Warning "  Automatic refresh was not changed ($($_.Exception.Message))." }
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '=== Setup complete ====================================================='
Write-Host ''
Write-Host "  Tools:    $tools"
Write-Host "  Data:     $output"
Write-Host '  Console:  double-click "IT Ops Console" on your desktop'
Write-Host ''
try { Stop-Transcript | Out-Null } catch { }

# One motion from "installed" to "looking at your own data": collecting now is
# the default - Enter starts it, a plain N leaves the desktop icons for later.
$ranNow = $false
if (-not $Unattended) {
    Write-Host 'Last question - setup can collect your data right now. A Microsoft'
    Write-Host 'sign-in window will open (read-only, no password is stored), a live'
    Write-Host 'progress page follows along, and the console opens when it is done.'
    if (Ask-YesNo 'Collect your data now? (press Enter to start)' $true) {
        $ranNow = $true
        Write-Host ''
        # Run EXACTLY what the "Refresh IT Ops Data" icon runs - one behaviour,
        # not two - in a fresh process so this window's settings stay out of
        # the collectors.
        $runArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                     '-File', (Join-Path $consoleDir 'run-all.ps1'),
                     '-ToolRoot', $tools, '-OutputRoot', $output, '-SitePath', $site)
        if ($python) { $runArgs += @('-Python', $python) }
        $engine = if ($onWindows) { 'powershell.exe' } else { 'pwsh' }
        & $engine @runArgs
        Write-Host ''
        Write-Host 'From here on, "Refresh IT Ops Data" on your desktop is the whole'
        Write-Host 'routine - run it on whatever rhythm suits you. The console shows how'
        Write-Host 'old every number is, and says so loudly when data has gone stale.'
    }
}
if (-not $ranNow) {
    Write-Host ''
    Write-Host 'Next: double-click "Refresh IT Ops Data" on your desktop, sign in when'
    Write-Host 'asked, and when it finishes open "IT Ops Console". Do that on whatever'
    Write-Host 'rhythm suits you - the console shows how old every number is, and says'
    Write-Host 'so loudly when data has gone stale.'
    Write-Host ''
}
if (-not $Unattended -and -not $env:ITOPS_CMD) {
    Write-Host ''
    # "Run with PowerShell" closes the window the instant the script ends -
    # without this, a successful setup looks like a window that just vanished.
    # (Skipped when the .cmd launcher started us - it holds the window itself.)
    $null = Read-Host 'All done - press Enter to close this window'
}
