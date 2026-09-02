<#
.SYNOPSIS
    Put the settings you just saved in the console into the files that use them.

.DESCRIPTION
    The companion to the "Save settings" button on the console's Alerts and
    Print fleet tabs. That button copies your settings to the clipboard (and
    drops a copy in your Downloads folder); this takes whichever it finds and
    puts each part where it belongs - which alerts count into alerts.ini, where
    to look for printers into the printer collector's config.ini - keeping your
    comments and everything else in both files. Each file's previous version is
    kept as <name>.bak, and nothing is written at all unless the result reads
    back cleanly.

    Double-click "Apply Settings" on your desktop. That is the whole
    instruction.

.PARAMETER SettingsPath
    Read the settings from this file instead of the clipboard.

.PARAMETER DryRun
    Say what would change, change nothing.
#>
[CmdletBinding()]
param(
    [string]$SettingsPath,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$onWindows = ($env:OS -eq 'Windows_NT')
# A block of settings always names at least one of the console's own sections.
$LOOKS_LIKE = '\[(send|identity|security|licensing|fleet|changes|refresh|ranges|discovery)\]'

function Get-PythonVersionText {
    # cmd.exe merges the Microsoft Store stub's stderr into plain text, so a
    # fake python.exe never turns into a red error here.
    param([string]$Candidate)
    $ErrorActionPreference = 'Continue'
    try {
        if ($onWindows) { return ((& cmd.exe /d /c "$Candidate --version 2>&1" | Out-String).Trim()) }
        return ((& $Candidate --version 2>&1 | Out-String).Trim())
    } catch { return '' }
}
function Get-WorkingPython {
    foreach ($cand in @('python', 'python3', 'py')) {
        if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
        if ((Get-PythonVersionText $cand) -match 'Python 3') { return $cand }
    }
    return $null
}

Write-Host ''
Write-Host '=== Apply settings ====================================================='
Write-Host ''

$python = Get-WorkingPython
if (-not $python) {
    Write-Warning 'Python 3 is not installed, so settings cannot be applied. Re-run setup, or install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").'
    exit 1
}

$text = ''
$from = ''
if ($SettingsPath) {
    if (-not (Test-Path -LiteralPath $SettingsPath)) {
        Write-Warning "Could not find $SettingsPath."
        exit 2
    }
    $text = Get-Content -LiteralPath $SettingsPath -Raw
    $from = $SettingsPath
} else {
    try { $text = Get-Clipboard -Raw } catch { $text = '' }
    if ("$text" -match $LOOKS_LIKE) {
        $from = 'what you copied in the console'
    } else {
        # Nothing useful on the clipboard - fall back to the copy the Save
        # button leaves in Downloads (Chrome names repeats "... (1).txt").
        $text = ''
        $dl = Join-Path $env:USERPROFILE 'Downloads'
        if (Test-Path $dl) {
            # 'it-ops-settings*.txt' is what Save writes now; 'alert-settings*'
            # is what an older console left behind, and still applies fine.
            # -Include needs a wildcard path; -Path $dl on its own matches nothing.
            $file = Get-ChildItem -Path (Join-Path $dl '*') `
                                  -Include 'it-ops-settings*.txt', 'alert-settings*.txt' `
                                  -File -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($file) {
                $text = Get-Content -LiteralPath $file.FullName -Raw
                $from = $file.FullName
            }
        }
    }
}

if (-not ("$text" -match $LOOKS_LIKE)) {
    Write-Host 'Nothing to apply yet.'
    Write-Host ''
    Write-Host '  1. Open "IT Ops Console" on your desktop and go to the Alerts tab'
    Write-Host '     (what you are told about) or the Print fleet tab (where to look for printers).'
    Write-Host '  2. Change what you want.'
    Write-Host '  3. Click "Save settings".'
    Write-Host '  4. Double-click this icon again.'
    Write-Host ''
    exit 2
}

Write-Host "Applying the settings from $from ..."
Write-Host ''
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("it-ops-settings-{0}.txt" -f [guid]::NewGuid().ToString('n').Substring(0, 6))
Set-Content -Path $tmp -Value $text -Encoding UTF8
try {
    $pyArgs = @((Join-Path $here 'apply-settings.py'), '--settings', $tmp, '--config', (Join-Path $here 'alerts.ini'))
    if ($DryRun) { $pyArgs += '--dry-run' }
    & $python @pyArgs
    $code = $LASTEXITCODE
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Write-Host ''
exit $code
