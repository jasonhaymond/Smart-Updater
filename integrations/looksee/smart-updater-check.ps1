# Looksee "Custom script" check for Smart Updater (Windows).  NOT TESTED on a real Windows machine yet.
#
# Put this file in the Looksee agent's script_dir (C:\ProgramData\LookseeAgent\scripts) and add a Custom
# script check named smart-updater-check.ps1. The agent runs it as SYSTEM, so smart-updater.pyz must live
# somewhere only administrators can write: C:\Program Files\SmartUpdater\ (not C:\ProgramData, which
# ordinary users can add files to).
#
# Prints Smart Updater's status line plus what needs a person, why, and what to do. The exit code is the
# Nagios one Looksee understands: 0 = up, 1 = warn, 2 = down (critical), 3 = unknown.

$pyz = Join-Path $env:ProgramFiles "SmartUpdater\smart-updater.pyz"
if (-not (Test-Path -LiteralPath $pyz)) {
    Write-Output "SMART_UPDATER UNKNOWN - $pyz not found (copy smart-updater.pyz there, or edit this script)"
    exit 3
}

# The "py" launcher is installed system-wide by python.org's installer; fall back to a machine-wide Python.
$python = $null
$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($launcher) {
    $python = @($launcher.Source, "-3")
} else {
    $found = Get-ChildItem -Path "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { $python = @($found.FullName) }
}
if (-not $python) {
    Write-Output "SMART_UPDATER UNKNOWN - no machine-wide Python 3.9+ found (install Python for all users)"
    exit 3
}

$exe = $python[0]
$prefix = @()
if ($python.Count -gt 1) { $prefix = $python[1..($python.Count - 1)] }
& $exe @prefix $pyz check --details @args
exit $LASTEXITCODE
