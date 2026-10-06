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

$jobs = @(
    @{ Name = "Capacity_Morning"; At = "07:25"; Args = "" },
    @{ Name = "Capacity_Retry";   At = "08:40"; Args = "--if-missing" }
)
foreach ($j in $jobs) {
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"`"$Py`" `"$Script`" $($j.Args) >> `"$Log`" 2>&1`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Daily -At $j.At
    Register-ScheduledTask -TaskPath "\Capacity\" -TaskName $j.Name -Action $action `
        -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    $t = Get-ScheduledTask -TaskPath "\Capacity\" -TaskName $j.Name
    Write-Host ("  {0,-18} {1}  logon={2} batteryStop={3} startWhenAvailable={4}" -f $j.Name, $j.At,
        $t.Principal.LogonType, $t.Settings.StopIfGoingOnBatteries, $t.Settings.StartWhenAvailable) -ForegroundColor Green
}

Write-Host "`n[5/5] Local git repo..." -ForegroundColor Cyan
Push-Location $Root
if (-not (Test-Path ".git")) { git init -b main | Out-Null }
git add garmin_capacity.py install_capacity.ps1 README.md .gitignore .env.example
git commit -m "Capacity Planner: Garmin Body Battery -> capacity tier + circadian targets; tasks 07:25/08:40"
Pop-Location

Write-Host "`nDone. First real run tomorrow 07:25 (retry 08:40)." -ForegroundColor Yellow
