# Test: run-all.ps1's Invoke-Native (how every collector step is run).
#   pwsh tests/test_invoke_native.ps1
#
# 20 Oct 2026, the desktop: the network step finished, exited 0, and was
# reported FAILED - its "Detail" was a harmless Python warning. Under
# $ErrorActionPreference = 'Stop', Windows PowerShell 5.1 turns the first line
# a program writes to stderr into a terminating error. pwsh 7 does not, so the
# run-all tests (pwsh) could never see it.
#
# This test reproduces the same mechanism in pwsh: the "program" is a script
# that writes an error record (what 5.1 makes of a stderr line) and then exits
# with a chosen code, run under 'Stop' exactly as run-all runs. The rule being
# pinned: the exit code alone decides ok / FAILED; stderr is only log text.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSCommandPath
$repo = Split-Path -Parent $here
$work = Join-Path ([IO.Path]::GetTempPath()) "itops-native-$([guid]::NewGuid().ToString('n').Substring(0, 6))"
$null = New-Item -ItemType Directory -Path $work -Force

$fails = [System.Collections.Generic.List[string]]::new()
function Check { param([string]$Label, [bool]$Cond)
    Write-Host ("{0} {1}" -f ($(if ($Cond) { 'PASS' } else { 'FAIL' })), $Label)
    if (-not $Cond) { $fails.Add($Label) }
}

# The real function, lifted out of run-all.ps1 by the parser (not a copy).
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'run-all.ps1'), [ref]$null, [ref]$null)
$fn = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-Native' }, $true))
Check 'run-all.ps1 still defines Invoke-Native' ($fn.Count -eq 1)
. ([scriptblock]::Create($fn[0].Extent.Text))

# What Invoke-Native reports through - recorded, not acted on.
$script:results = [System.Collections.Generic.List[object]]::new()
$script:lines = [System.Collections.Generic.List[string]]::new()
function Add-Result { param($Name, $Status, $Detail, $Seconds) $script:results.Add([pscustomobject]@{ Step = $Name; Status = $Status; Detail = "$Detail" }) }
function Set-StepState { param($Key, $State, $Seconds, $Plain, $Detail) }
function Add-StatusLine { param($Key, $Line) $script:lines.Add($Line) }
function Get-PlainWords { param($Step, $Detail) "$Detail" }
function Say { param($Line) }
function Warn { param($Line) }

function New-Stub { param([string]$Name, [int]$Exit)
    $p = Join-Path $work "$Name.ps1"
    Set-Content $p @"
Write-Output 'a normal line'
Write-Error 'CryptographyDeprecationWarning: CFB has been moved'
exit $Exit
"@
    return $p
}

function Run { param([string]$Name, [string]$Exe)
    $script:results.Clear(); $script:lines.Clear()
    Invoke-Native -Name $Name -Exe $Exe -NativeArgs @() -StepKey $Name
    return $script:results[0]
}

$r = Run 'warns-then-ok' (New-Stub 'warn0' 0)
Check 'a step that writes to stderr and exits 0 is ok' ($r.Status -eq 'ok')
Check '... and its stderr line is kept in the log, not lost' (@($script:lines | Where-Object { $_ -like '*CryptographyDeprecationWarning*' }).Count -eq 1)
Check '... alongside its normal output' ($script:lines -contains 'a normal line')

$r = Run 'warns-then-fails' (New-Stub 'warn3' 3)
Check 'a step that exits non-zero is still FAILED' ($r.Status -eq 'FAILED' -and $r.Detail -eq 'exit code 3')

# Right after a clean exit 0, so a stale exit code cannot fail it by luck.
$null = Run 'clean' (New-Stub 'clean0' 0)
$r = Run 'not-there' (Join-Path $work 'no-such-program.exe')
Check 'a program that does not exist still fails the step, saying so' ($r.Status -eq 'FAILED' -and $r.Detail -like '*no-such-program*')

Check 'the caller''s ErrorActionPreference is back to Stop afterwards' ($ErrorActionPreference -eq 'Stop')

Write-Host ''
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
if ($fails.Count) { Write-Host "RESULT: $($fails.Count) FAILURES"; $fails | ForEach-Object { Write-Host "  - $_" }; exit 1 }
Write-Host 'RESULT: ALL PASS'
exit 0
