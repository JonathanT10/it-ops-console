<#
.SYNOPSIS
    Assemble the one-file suite bundle: IT-Ops-Suite-v<version>.zip

.DESCRIPTION
    Pulls the five published repos fresh from GitHub, stages them into the
    bundle layout, stamps the version, and zips it:

      IT-Ops-Suite/
        Setup-IT-Ops-Console.cmd    double-click this - that's the instruction
        setup.ps1                   installs from the bundled tools, no internet needed
        VERSION
        README.txt
        tools/<the five repos>

    Publish the zip as a GitHub Release on it-ops-console. Downloading THAT
    is the whole ship story: one file, one double-click, works offline.

.EXAMPLE
    .\make-release.ps1 -Version 1.0.0
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Version,
    [string]$OutDir = '.'
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072

$REPOS = @('entra-tenant-docs', 'entra-security-snapshot', 'm365-license-waste-report',
           'print-fleet-dashboard', 'it-ops-console')

$stage = Join-Path ([System.IO.Path]::GetTempPath()) "itops-release-$([guid]::NewGuid().ToString('n').Substring(0,8))"
$root = Join-Path $stage 'IT-Ops-Suite'
$toolsDir = Join-Path $root 'tools'
$null = New-Item -ItemType Directory -Path $toolsDir -Force

foreach ($r in $REPOS) {
    Write-Host "fetching $r..."
    $zip = Join-Path $stage "$r.zip"
    Invoke-WebRequest "https://github.com/JonathanT10/$r/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $stage -Force
    Move-Item (Join-Path $stage "$r-main") (Join-Path $toolsDir $r)
    Remove-Item $zip
}

# The launcher pair at the bundle root comes from the console repo just fetched.
$console = Join-Path $toolsDir 'it-ops-console'
Copy-Item (Join-Path $console 'Setup-IT-Ops-Console.cmd') $root
Copy-Item (Join-Path $console 'setup.ps1') $root

# Version stamp: at the root for setup, and inside the console for the footer.
$stamp = "$Version`r`nassembled $(Get-Date -Format yyyy-MM-dd) from the repos' main branches"
Set-Content -Path (Join-Path $root 'VERSION') -Value $stamp -Encoding ASCII
Set-Content -Path (Join-Path $console 'VERSION') -Value $stamp -Encoding ASCII

Set-Content -Path (Join-Path $root 'README.txt') -Encoding ASCII -Value @"
IT Ops Suite v$Version
======================

Double-click Setup-IT-Ops-Console.cmd. That is the whole instruction.

It installs to C:\IT-Ops (you can change that at the one question it asks),
puts three shortcuts on your desktop, asks how the console should stay fresh,
and offers to run the first collection:

  Refresh IT Ops Data   collects from Microsoft 365 (you sign in, read-only)
                        and rebuilds your console, with live progress
  IT Ops Console        opens the result in your browser
  Apply Alert Settings  applies alert changes you made on the Alerts tab

Keeping it fresh - setup's last question, three answers (re-run setup to
change it):
  1. I'll click Refresh myself                (nothing scheduled - the default)
  2. Refresh daily while I'm signed in        (stays signed in to Microsoft 365
                                               between refreshes so it does not
                                               ask you each morning; re-run
                                               setup and pick 1 to sign out)
  3. Refresh daily even when nobody is signed in
                                              (a Global Administrator registers
                                               a read-only app once - setup
                                               walks you through it; no
                                               password is stored anywhere)

Alerts (optional): the console works out what needs a person on every
refresh. To have that sent to a Teams channel or an email address, put your
channel's Workflows URL (or a mail relay) in
C:\IT-Ops\tools\it-ops-console\alerts.ini, then run  python notify.py --test
from that folder. Messages go out only when something is new, worse or
cleared - plus one weekly summary. After that you never need the file again:
change what you are told about on the console's Alerts tab, click "Save
settings", and double-click "Apply Alert Settings" on your desktop.

Everything against your tenant is read-only. Nothing stores a password.
Setup locks the C:\IT-Ops folder to you and Administrators, so the data it
collects (admin names, stale accounts, the app inventory) is not readable by
other people who use this computer.
Printers are optional: put their IPs in
C:\IT-Ops\tools\print-fleet-dashboard\config.ini and the next Refresh
picks them up automatically.

If something goes wrong: run check-setup.ps1 in C:\IT-Ops\tools\it-ops-console
(right-click > Run with PowerShell) and send check-setup.log to whoever helps you.

Source, docs and updates: https://github.com/JonathanT10/it-ops-console
"@

$zipName = Join-Path (Resolve-Path $OutDir) "IT-Ops-Suite-v$Version.zip"
if (Test-Path $zipName) { Remove-Item $zipName }
Compress-Archive -Path $root -DestinationPath $zipName
Remove-Item $stage -Recurse -Force
Write-Host ''
Write-Host "Bundle: $zipName"
Write-Host 'Publish it as a GitHub Release on it-ops-console.'
