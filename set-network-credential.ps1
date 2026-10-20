<#
.SYNOPSIS
    Save one site's SNMPv3 credential for the network-health check, encrypted.

.DESCRIPTION
    The network check (network\collector.py) asks switches and access points how
    they are over SNMPv3, which needs a user name and password per site. This is
    the ONLY place those are typed, and they are never written down in plain
    text: this script encrypts them with Windows DPAPI in machine scope and saves
    the result as network-credential.bin in the output folder - the folder setup
    locks to the installer, Administrators and SYSTEM. Machine scope is what lets
    the daily refresh (which runs as SYSTEM) read it; the folder lock is what
    stops anybody else on this computer from doing the same.

    Run it once per site, from a PowerShell window opened with "Run as
    administrator", on the computer that refreshes the console. Running it again
    for a site replaces that site's entry and keeps every other site's. The
    values come from the LastPass item for that site - this script never shows
    them, never logs them, and never writes them anywhere else.

.PARAMETER OutputRoot
    The console's output folder. Default: C:\IT-Ops\output

.PARAMETER Site
    The site name network.ini uses for "credential =". Asked for when omitted.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\set-network-credential.ps1
#>
[CmdletBinding()]
param(
    [string]$OutputRoot = 'C:\IT-Ops\output',
    [string]$Site
)

$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    Write-Host 'This only works on Windows: the credential is encrypted by Windows itself.'
    exit 1
}
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) {
    Write-Host 'This must run from a PowerShell window opened with "Run as administrator". Nothing was saved.'
    exit 1
}
if (-not (Test-Path -LiteralPath $OutputRoot)) {
    Write-Host "The output folder $OutputRoot does not exist. Run setup first, or pass -OutputRoot. Nothing was saved."
    exit 1
}

Add-Type -AssemblyName System.Security
$scope = [System.Security.Cryptography.DataProtectionScope]::LocalMachine
$path = Join-Path $OutputRoot 'network-credential.bin'

function ConvertFrom-Secure {
    # SecureString -> plain string, freeing the unmanaged copy at once. The
    # returned .NET string cannot be wiped (strings are immutable), so it is
    # kept only as long as it takes to build the encrypted file.
    param([Security.SecureString]$Secure)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

# --- what is already saved (other sites are kept) ---------------------------- #
$entries = [ordered]@{}
if (Test-Path -LiteralPath $path) {
    $plain = $null
    try {
        $plain = [System.Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($path), $null, $scope)
        $doc = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
        foreach ($p in $doc.PSObject.Properties) {
            $entries[$p.Name] = [ordered]@{ user = "$($p.Value.user)"; password = "$($p.Value.password)" }
        }
    } catch {
        Write-Host "The credential file already in $OutputRoot could not be opened on this computer, so it was left exactly as it is and nothing was saved."
        Write-Host "If it came from another computer, move it aside and run this again. On this computer: Rename-Item -LiteralPath '$path' -NewName 'network-credential.bin.old'"
        exit 1
    } finally {
        if ($plain) { [Array]::Clear($plain, 0, $plain.Length) }
    }
    if ($entries.Count) { Write-Host "Already saved for: $(@($entries.Keys) -join ', ')" }
}

# --- the site, the user, the password ---------------------------------------- #
if (-not $Site) { $Site = Read-Host 'Site name, exactly as network.ini says after "credential =" (for example dfw)' }
$Site = "$Site".Trim().ToLower()
if ($Site -notmatch '^[a-z0-9_-]+$') {
    Write-Host 'A site name is letters, numbers, - and _ only. Nothing was saved.'
    exit 1
}
$user = "$(Read-Host "SNMPv3 user name for $Site")".Trim()
if (-not $user) { Write-Host 'No user name given. Nothing was saved.'; exit 1 }
$first = Read-Host "SNMPv3 password for $Site (from LastPass; it is not shown)" -AsSecureString
$again = Read-Host 'The same password again' -AsSecureString
$pw = ConvertFrom-Secure $first
$pw2 = ConvertFrom-Secure $again
$first.Dispose(); $again.Dispose()
if ($pw -cne $pw2) { $pw = $null; $pw2 = $null; Write-Host 'The two passwords did not match. Nothing was saved.'; exit 1 }
$pw2 = $null
if ($pw.Length -lt 8) { $pw = $null; Write-Host 'SNMPv3 passwords are at least 8 characters. Nothing was saved.'; exit 1 }

# --- encrypt and write -------------------------------------------------------- #
$entries[$Site] = [ordered]@{ user = $user; password = $pw }
$pw = $null
$bytes = $null; $sealed = $null
try {
    $bytes = [Text.Encoding]::UTF8.GetBytes(($entries | ConvertTo-Json -Depth 3 -Compress))
    $sealed = [System.Security.Cryptography.ProtectedData]::Protect($bytes, $null, $scope)
} finally {
    if ($bytes) { [Array]::Clear($bytes, 0, $bytes.Length) }
    foreach ($k in @($entries.Keys)) { $entries[$k]['password'] = $null }
    $entries = $null
}
# Write beside the target, then swap: a failure part way never leaves a
# half-written credential file that the next refresh cannot open.
$tmp = "$path.new"
[IO.File]::WriteAllBytes($tmp, $sealed)
Move-Item -LiteralPath $tmp -Destination $path -Force
Write-Host ''
Write-Host "Saved the $Site credential, encrypted, to $path"
Write-Host 'Only this computer can open it. The next "Refresh IT Ops Data" uses it.'
Write-Host "To check it, on this computer: & '$(Join-Path $PSScriptRoot 'check-setup.ps1')'"
