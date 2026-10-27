# Registers \Capacity\Capacity_Checkin_Bot. Run from an ADMIN PowerShell (startup trigger needs it).
#   cd C:\Users\dppha\CapacityPlanner
#   .\install_checkin.ps1
$ErrorActionPreference = 'Stop'
$root = 'C:\Users\dppha\CapacityPlanner'
Set-Location $root

if (-not (Test-Path "$root\checkin_bot.py")) { throw "checkin_bot.py not found in $root" }

# Use the same Python the existing Capacity tasks use, if it's a direct python.exe call.
$ref = Get-ScheduledTask -TaskPath '\Capacity\' -TaskName 'Capacity_Morning' -ErrorAction SilentlyContinue
$py = $null
if ($ref -and $ref.Actions[0].Execute -match 'python(w)?\.exe$') { $py = $ref.Actions[0].Execute }
if (-not $py) { $py = (& python -c "import sys; print(sys.executable)").Trim() }
Write-Host "Python: $py"
if ($ref) { Write-Host "Capacity_Morning action for comparison: $($ref.Actions[0].Execute) $($ref.Actions[0].Arguments)" }

$action    = New-ScheduledTaskAction -Execute $py -Argument 'checkin_bot.py' -WorkingDirectory $root
$triggers  = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    New-ScheduledTaskTrigger -Daily -At '08:30'   # wakes the PC for the 08:35 morning sweat question
    New-ScheduledTaskTrigger -Daily -At '23:10'   # wakes the PC for the 23:15 check-in
)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskPath '\Capacity\' -TaskName 'Capacity_Checkin_Bot' `
    -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskPath '\Capacity\' -TaskName 'Capacity_Checkin_Bot'
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskPath '\Capacity\' -TaskName 'Capacity_Checkin_Bot' | Select-Object TaskName, State
Get-Content "$root\data\checkin_bot.log" -Tail 5
