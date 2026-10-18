# ═══════════════════════════════════════════════════════════════
# Capacity Planner installer — standalone (not part of CrystalBull)
# Run from an elevated PowerShell:
#   cd $HOME\CapacityPlanner ; .\install_capacity.ps1
# Step 2 is interactive (Garmin password + MFA) — run it yourself.
# ═══════════════════════════════════════════════════════════════
$ErrorActionPreference = "Stop"
$Py     = "C:\Users\dppha\AppData\Local\Programs\Python\Python313\python.exe"
$Root   = $PSScriptRoot
$Script = Join-Path $Root "garmin_capacity.py"
$Log    = Join-Path $Root "data\task.log"

Write-Host "`n[1/5] Installing Python packages..." -ForegroundColor Cyan
& $Py -m pip install --upgrade garminconnect curl_cffi python-dotenv requests

Write-Host "`n[2/5] One-time Garmin login (email, password, MFA if enabled)..." -ForegroundColor Cyan
Write-Host "      Tokens cached in $env:USERPROFILE\.garminconnect - password is NOT stored." -ForegroundColor DarkGray
& $Py $Script --setup
if ($LASTEXITCODE -ne 0) { throw "Garmin login failed - fix and re-run." }

Write-Host "`n[3/5] Live test pull (prints card, publishes nothing)..." -ForegroundColor Cyan
& $Py $Script --dry-run

Write-Host "`n[4/5] Registering scheduled tasks under \Capacity\ ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

# Times match the live tasks (retimed 2026-10-16 from 07:25/08:40). Capacity_Now has NO trigger:
# it runs on demand (Start-ScheduledTask \Capacity\Capacity_Now, or /capacity on @dp_health_consultant_bot).
$jobs = @(
    @{ Name = "Capacity_Morning"; At = "09:00"; Args = "" },
    @{ Name = "Capacity_Retry";   At = "10:15"; Args = "--if-missing" },
    @{ Name = "Capacity_Now";     At = $null;   Args = "--now" }
)
foreach ($j in $jobs) {
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"`"$Py`" `"$Script`" $($j.Args) >> `"$Log`" 2>&1`"" -WorkingDirectory $Root
    $reg = @{ TaskPath = "\Capacity\"; TaskName = $j.Name; Action = $action; Settings = $settings; Principal = $principal; Force = $true }
    if ($j.At) { $reg.Trigger = New-ScheduledTaskTrigger -Daily -At $j.At }
    Register-ScheduledTask @reg | Out-Null
    $t = Get-ScheduledTask -TaskPath "\Capacity\" -TaskName $j.Name
    $when = if ($j.At) { $j.At } else { "on demand" }
    Write-Host ("  {0,-18} {1}  logon={2} batteryStop={3} startWhenAvailable={4}" -f $j.Name, $when,
        $t.Principal.LogonType, $t.Settings.StopIfGoingOnBatteries, $t.Settings.StartWhenAvailable) -ForegroundColor Green
}

# Capacity_Bot (2026-10-18): the planner's own Telegram bot, @dp_health_consultant_bot. Long-running:
# at logon + at startup, S4U, restart every 1 min on failure (up to 999), no time limit, one instance.
# No cmd `>>` redirect here: a days-long redirect handle locks data/task.log and the other Capacity tasks
# then fail to append ("file in use"). capacity_bot.py appends its own lines to task.log, one open per line.
$botAction = New-ScheduledTaskAction -Execute $Py `
    -Argument "`"$(Join-Path $Root 'capacity_bot.py')`"" -WorkingDirectory $Root
$botTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"), (New-ScheduledTaskTrigger -AtStartup))
$botSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskPath "\Capacity\" -TaskName "Capacity_Bot" -Action $botAction -Trigger $botTriggers `
    -Settings $botSettings -Principal $principal -Force | Out-Null
Write-Host "  Capacity_Bot       at logon + at startup  (restart 1 min x999, no time limit)" -ForegroundColor Green

Write-Host "`n[5/5] Local git repo..." -ForegroundColor Cyan
Push-Location $Root
if (-not (Test-Path ".git")) { git init -b main | Out-Null }
git add garmin_capacity.py install_capacity.ps1 README.md .gitignore .env.example
git commit -m "Capacity Planner: Garmin Body Battery -> capacity tier + circadian targets; tasks 07:25/08:40"
Pop-Location

Write-Host "`nDone. First real run tomorrow 09:00 (retry 10:15). On demand: Start-ScheduledTask -TaskPath '\Capacity\' -TaskName Capacity_Now" -ForegroundColor Yellow
