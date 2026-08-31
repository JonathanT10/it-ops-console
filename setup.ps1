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
           "IT Ops Console"       opens the console in your browser
           "Refresh IT Ops Data"  runs every collector (you sign in), then
                                  rebuilds the console

    Everything the collectors do against your tenant is READ-ONLY. Nothing
    here stores a password - you sign in interactively when data is collected.

    Works in Windows PowerShell 5.1 (what "right-click > Run with PowerShell"
    gives you) and in PowerShell 7.

.PARAMETER Root
    Where everything lives. Default: C:\IT-Ops
    (tools\ for the downloaded repos, output\ for collected data,
     console-site\ for the built pages)

.PARAMETER Unattended
    No prompts: accept every default, skip the Python install offer and the
    run-everything-now offer. For scripted or repeated setups.

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
Write-Host 'It asks ONE question now and one at the end. When the window pauses,'
Write-Host 'it is waiting for you - pressing Enter accepts the suggested answer.'
Write-Host ''

$Root = Read-Default 'Install folder - press Enter to accept' $Root
Write-Host ''
Write-Host "Setting up in $Root. The rest runs on its own - takes a minute or two."
$tools = Join-Path $Root 'tools'
$output = Join-Path $Root 'output'
$site = Join-Path $Root 'console-site'
foreach ($d in @($Root, $tools, $output, $site)) { $null = New-Item -ItemType Directory -Path $d -Force }

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 1/5 Downloading the tools ---'
foreach ($r in $REPOS) {
    $dest = Join-Path $tools $r
    if (Test-Path $dest) {
        Write-Host "  already present: $r  (delete the folder and re-run setup to refresh it)"
        continue
    }
    Write-Host "  fetching: $r"
    $zip = Join-Path $tools "$r.zip"
    Invoke-WebRequest "https://github.com/JonathanT10/$r/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $tools -Force
    Rename-Item (Join-Path $tools "$r-main") $dest
    Remove-Item $zip
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 2/5 Microsoft Graph PowerShell modules ---'
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
Write-Host '--- 3/5 Python (builds the console pages) ---'
function Get-WorkingPython {
    foreach ($cand in @('python', 'python3', 'py')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # The Microsoft Store ships a fake python.exe that only opens the Store.
        # A real interpreter answers --version; the stub prints an install hint.
        $out = & $cand --version 2>&1
        if ("$out" -match 'Python 3') { return $cand }
    }
    return $null
}
$python = Get-WorkingPython
if ($python) {
    Write-Host "  found: $python ($(& $python --version 2>&1))"
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
Write-Host '--- 4/5 Wiring the console to the tools ---'
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
"@
Set-Content -Path (Join-Path $consoleDir 'sources.ini') -Value $sources -Encoding UTF8
Write-Host "  wrote $((Join-Path $consoleDir 'sources.ini'))"

$pfd = Join-Path $tools 'print-fleet-dashboard'
if ((Test-Path (Join-Path $pfd 'config.example.ini')) -and -not (Test-Path (Join-Path $pfd 'config.ini'))) {
    Copy-Item (Join-Path $pfd 'config.example.ini') (Join-Path $pfd 'config.ini')
    Write-Host "  printers are OPTIONAL: to add them later, put their IPs in $((Join-Path $pfd 'config.ini'))"
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '--- 5/5 Desktop shortcuts ---'
if ($onWindows) {
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('Desktop')

    $lnk = $shell.CreateShortcut((Join-Path $desktop 'IT Ops Console.lnk'))
    $lnk.TargetPath = Join-Path $site 'index.html'
    $lnk.Description = 'Open the IT ops console'
    $lnk.Save()

    $runArgs = "-NoProfile -NoExit -ExecutionPolicy Bypass -File `"$consoleDir\run-all.ps1`" -ToolRoot `"$tools`" -OutputRoot `"$output`" -SitePath `"$site`""
    if ($python) { $runArgs += " -Python $python" }
    $lnk = $shell.CreateShortcut((Join-Path $desktop 'Refresh IT Ops Data.lnk'))
    $lnk.TargetPath = 'powershell.exe'
    $lnk.Arguments = $runArgs
    $lnk.WorkingDirectory = $consoleDir
    $lnk.Description = 'Run every collector (you sign in), then rebuild the console'
    $lnk.Save()
    Write-Host '  created: "IT Ops Console" and "Refresh IT Ops Data"'
} else {
    Write-Host '  (not Windows - skipping shortcuts)'
}

# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '=== Setup complete ====================================================='
Write-Host ''
Write-Host "  Tools:    $tools"
Write-Host "  Data:     $output"
Write-Host "  Console:  $(Join-Path $site 'index.html')"
Write-Host ''
Write-Host 'Next: double-click "Refresh IT Ops Data" on your desktop, sign in when'
Write-Host 'asked, and when it finishes open "IT Ops Console". Do that on whatever'
Write-Host 'rhythm suits you - the console shows how old every number is, and says'
Write-Host 'so loudly when data has gone stale.'
Write-Host ''
try { Stop-Transcript | Out-Null } catch { }
if (-not $Unattended -and (Ask-YesNo 'Last question - run the first collection now? (you will sign in)' $false)) {
    $runAllArgs = @{ ToolRoot = $tools; OutputRoot = $output; SitePath = $site }
    if ($python) { $runAllArgs['Python'] = $python }
    & (Join-Path $consoleDir 'run-all.ps1') @runAllArgs
}
if (-not $Unattended) {
    Write-Host ''
    # "Run with PowerShell" closes the window the instant the script ends -
    # without this, a successful setup looks like a window that just vanished.
    $null = Read-Host 'All done - press Enter to close this window'
}
