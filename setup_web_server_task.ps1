$ErrorActionPreference = "Stop"

if (-not (Get-NetFirewallRule -DisplayName "DailyDigestWeb 8080" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "DailyDigestWeb 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow -Profile Any | Out-Null
    Write-Host "Firewall rule created."
} else {
    Write-Host "Firewall rule already exists, skipping."
}

$action = New-ScheduledTaskAction -Execute "D:\projects\daily-digest-bot\run_web_server.bat"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName "DailyDigestWeb" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Scheduled task registered."

Start-ScheduledTask -TaskName "DailyDigestWeb"
Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName "DailyDigestWeb" | Get-ScheduledTaskInfo | Select-Object TaskName, LastTaskResult, LastRunTime

Write-Host ""
Write-Host "Checking port 8080..."
Start-Sleep -Seconds 2
$conn = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "SUCCESS: something is listening on port 8080."
} else {
    Write-Host "WARNING: nothing is listening on port 8080 yet. Check Task Scheduler history for DailyDigestWeb."
}
