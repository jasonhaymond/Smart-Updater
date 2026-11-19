# Registers a daily Smart Updater run in Task Scheduler.
# Run this from an ELEVATED PowerShell prompt. NOT TESTED on a real Windows machine yet.
#
# Run the task as an administrator account, not SYSTEM: winget does not reliably see
# per-user installs or find the App Installer when run as SYSTEM.

param(
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [string]$Python = "py.exe",
    [string]$Config = "$env:ProgramData\SmartUpdater\config.toml",
    [string]$At = "3:30am"
)

$arguments = "-m smart_updater apply --quiet --config `"$Config`""
$action    = New-ScheduledTaskAction -Execute $Python -Argument $arguments
$trigger   = New-ScheduledTaskTrigger -Daily -At $At
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)

# -RunLevel Highest = "run with highest privileges". You will be asked for the account password.
Register-ScheduledTask -TaskName "Smart Updater" -Action $action -Trigger $trigger `
    -Settings $settings -User $User -RunLevel Highest -Password (Read-Host -AsSecureString "Password for $User" | ForEach-Object { [System.Net.NetworkCredential]::new("", $_).Password })

# Exit codes: 0 ok, 1 warning, 2 critical, 3 could not run. Look at the result with:
#   py -m smart_updater check
