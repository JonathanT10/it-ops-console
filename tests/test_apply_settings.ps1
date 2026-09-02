# Test: the "Apply Settings" icon.          pwsh tests/test_apply_settings.ps1
#
# apply-settings.ps1 is the whole instruction a person gets: click Save in the
# console, double-click this icon. So what it has to get right is FINDING the
# settings - the clipboard, or the copy the Save button leaves in Downloads -
# and handing them to apply-settings.py with the right two files.
#
# The Downloads glob is here because it broke once: Get-ChildItem -Include
# matches nothing unless the path itself ends in a wildcard, which fails
# silently - the icon simply says "nothing to apply" forever.
#
# Everything runs in a temp copy laid out the way setup.ps1 lays out a real
# install (tools\it-ops-console beside tools\print-fleet-dashboard), so the
# default path from one to the other is tested rather than assumed.
# Runs on Linux (pwsh) or Windows.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSCommandPath
$repo = Split-Path -Parent $here
$work = Join-Path ([IO.Path]::GetTempPath()) "itops-apply-$([guid]::NewGuid().ToString('n').Substring(0, 6))"
$tools = Join-Path $work 'tools'
$console = Join-Path $tools 'it-ops-console'
$fleet = Join-Path $tools 'print-fleet-dashboard'
$home_ = Join-Path $work 'home'
$dl = Join-Path $home_ 'Downloads'
$null = New-Item -ItemType Directory -Path $console, $fleet, $dl -Force

$fails = [System.Collections.Generic.List[string]]::new()
function Check { param([string]$Label, [bool]$Cond)
    Write-Host ("{0} {1}" -f ($(if ($Cond) { 'PASS' } else { 'FAIL' })), $Label)
    if (-not $Cond) { $fails.Add($Label) }
}

# ---- a temp install ------------------------------------------------------- #
Copy-Item (Join-Path $repo 'apply-settings.ps1') $console
Copy-Item (Join-Path $repo 'apply-settings.py') $console
Copy-Item (Join-Path $repo 'alerts.example.ini') $console
Copy-Item (Join-Path $repo 'console') $console -Recurse
# A printer config with the parts this must never touch.
Set-Content (Join-Path $fleet 'config.example.ini') @'
; Copy to config.ini and point at your fleet.
[snmp]
community = public
timeout = 2

[devices]
; Display Name = ip
Front Office = 10.0.10.21

[ranges]
; WHERE TO LOOK for printers you have not listed above.
; Office = 10.0.10.0/24

[discovery]
rescan_hours = 24
ignore =
'@

$SETTINGS = @'
# IT Ops Console settings, made in the console.
[send]
when = every-refresh

[ranges]
Front Desk = 10.0.10.0/24

[discovery]
rescan_hours = 6
'@
$ALERTS_ONLY = "# IT Ops Console settings, made in the console.`n[send]`nwhen = every-refresh`n"

$script = Join-Path $console 'apply-settings.ps1'
$alertsIni = Join-Path $console 'alerts.ini'
$fleetIni = Join-Path $fleet 'config.ini'
$oldProfile = $env:USERPROFILE
$env:USERPROFILE = $home_

function Run { param([string[]]$Arguments = @())
    $out = & pwsh -NoProfile -File $script @Arguments 2>&1 | Out-String
    return @{ Text = $out; Code = $LASTEXITCODE }
}
function Reset {
    Remove-Item $alertsIni, "$alertsIni.bak", $fleetIni, "$fleetIni.bak" -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path (Join-Path $dl '*') -File -ErrorAction SilentlyContinue | Remove-Item -Force
}

try {
    # ---- 1. nothing anywhere: it says so, in words, and changes nothing ----- #
    Reset
    $r = Run
    Check 'nothing to apply: exit 2 and plain instructions' ($r.Code -eq 2 -and $r.Text -like '*Nothing to apply yet.*' -and $r.Text -like '*Save settings*')
    Check 'nothing to apply: no files invented' (-not (Test-Path $alertsIni) -and -not (Test-Path $fleetIni))

    # ---- 2. the Downloads fallback (the bug that started this file) --------- #
    Reset
    Set-Content (Join-Path $dl 'it-ops-settings.txt') $ALERTS_ONLY
    $r = Run
    Check 'Downloads: the file Save writes is found' ($r.Code -eq 0 -and $r.Text -like '*it-ops-settings.txt*')
    Check 'Downloads: it actually applied' ((Get-Content $alertsIni -Raw) -match 'when\s*=\s*every-refresh')

    # ---- 3. a copy from an older console still works ------------------------ #
    Reset
    Set-Content (Join-Path $dl 'alert-settings (1).txt') $ALERTS_ONLY
    $r = Run
    Check 'Downloads: an older alert-settings.txt is still accepted' ($r.Code -eq 0 -and $r.Text -like '*alert-settings (1).txt*')

    # ---- 4. two copies: the newest one wins --------------------------------- #
    Reset
    Set-Content (Join-Path $dl 'alert-settings.txt') "[send]`nwhen = changes`n"
    Start-Sleep -Milliseconds 1100
    Set-Content (Join-Path $dl 'it-ops-settings.txt') $ALERTS_ONLY
    $r = Run
    Check 'Downloads: the newest copy wins' ($r.Code -eq 0 -and $r.Text -like '*it-ops-settings.txt*' -and (Get-Content $alertsIni -Raw) -match 'when\s*=\s*every-refresh')

    # ---- 5. a block carrying both kinds reaches both files ------------------ #
    Reset
    $both = Join-Path $work 'both.txt'
    Set-Content $both $SETTINGS
    $r = Run @('-SettingsPath', $both)
    $fleetText = if (Test-Path $fleetIni) { Get-Content $fleetIni -Raw } else { '' }
    Check 'both kinds: exit 0 and both files written' ($r.Code -eq 0 -and (Test-Path $alertsIni) -and (Test-Path $fleetIni))
    Check 'both kinds: the printer config is found without being told where' ($r.Text -like '*Printer settings: changed*')
    Check 'both kinds: the range landed' ($fleetText -match '(?m)^Front Desk = 10\.0\.10\.0/24$')
    Check 'both kinds: rescan_hours merged, not replaced' ($fleetText -match '(?m)^rescan_hours = 6$' -and $fleetText -match '(?m)^ignore =\s*$')
    Check 'both kinds: [snmp] and [devices] untouched' ($fleetText -match 'community = public' -and $fleetText -match 'Front Office = 10\.0\.10\.21')
    Check 'both kinds: the section comments survive' ($fleetText -match 'WHERE TO LOOK for printers')
    Check 'both kinds: the alert half landed too' ((Get-Content $alertsIni -Raw) -match 'when\s*=\s*every-refresh')

    # ---- 6. a dry run changes nothing at all -------------------------------- #
    Reset
    $r = Run @('-SettingsPath', $both, '-DryRun')
    Check 'dry run: says what would change' ($r.Code -eq 0 -and $r.Text -like '*Would change*')
    Check 'dry run: creates no files whatsoever' (-not (Test-Path $alertsIni) -and -not (Test-Path $fleetIni))

    # ---- 7. something that is not settings ---------------------------------- #
    Reset
    $junk = Join-Path $work 'junk.txt'
    Set-Content $junk 'I copied a sentence out of an email by mistake'
    $r = Run @('-SettingsPath', $junk)
    Check 'not settings: refused in words, nothing written' ($r.Code -eq 2 -and $r.Text -like '*Nothing to apply yet*' -and -not (Test-Path $alertsIni))
} finally {
    $env:USERPROFILE = $oldProfile
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($fails.Count) { Write-Host "RESULT: $($fails.Count) FAILURES"; $fails | ForEach-Object { Write-Host "  - $_" }; exit 1 }
Write-Host 'RESULT: ALL PASS'
exit 0
