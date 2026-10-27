# Restart @dp_health_consultant_bot (task \Capacity\Capacity_Bot) and prove the new instance is alive.
# Stop -> wait for the old PID to exit (<= 15 s) -> Start -> wait for a heartbeat newer than the Start (<= 60 s).
# Prints the new PID + heartbeat time; exit 1 if the heartbeat does not refresh. (2026-10-27: a back-to-back
# Stop/Start let the new copy see the still-exiting old PID and quit with result 0, leaving the bot down.)
cd C:\Users\dppha\CapacityPlanner
$ErrorActionPreference = "Stop"
$TaskPath = "\Capacity\"
$TaskName = "Capacity_Bot"
$LockFile = "data\capacity_bot.lock"
$BeatFile = "data\bot_heartbeat"

function Read-Beat {
    # -> @{ Epoch = [double]; Iso = [string]; Pid = [int] } or $null
    if (-not (Test-Path $BeatFile)) { return $null }
    $lines = @(Get-Content $BeatFile -ErrorAction SilentlyContinue)
    if ($lines.Count -lt 3) { return $null }
    $beatPid = 0
    if ($lines[2] -match 'pid=(\d+)') { $beatPid = [int]$Matches[1] }
    return @{ Epoch = [double]$lines[0]; Iso = $lines[1]; Pid = $beatPid }
}

$oldPid = 0
if (Test-Path $LockFile) {
    $raw = (Get-Content $LockFile -Raw -ErrorAction SilentlyContinue)
    if ($raw -and $raw.Trim() -match '^\d+$') { $oldPid = [int]$raw.Trim() }
}
Write-Host "old PID: $oldPid"

Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
$deadline = (Get-Date).AddSeconds(15)
while ($oldPid -gt 0 -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
}
if ($oldPid -gt 0 -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
    Write-Warning "old PID $oldPid still running after 15 s; starting anyway (the bot waits for a live holder)"
} else {
    Write-Host "old PID gone"
}

$startEpoch = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
$deadline = (Get-Date).AddSeconds(60)
$beat = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $b = Read-Beat
    if ($b -and $b.Epoch -ge $startEpoch -and $b.Pid -ne $oldPid) { $beat = $b; break }
}
if (-not $beat) {
    Write-Host "FAIL: heartbeat did not refresh within 60 s. Last lines of data\task.log:" -ForegroundColor Red
    Get-Content data\task.log -Tail 5
    exit 1
}
Write-Host ("OK: Capacity_Bot PID {0}, heartbeat {1}" -f $beat.Pid, $beat.Iso) -ForegroundColor Green
exit 0
