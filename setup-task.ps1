$vbs = "C:\Users\lpgvi\Time tracker\run-silent.vbs"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive
Register-ScheduledTask -TaskName "TimeTrack Server" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
Write-Host "TimeTrack auto-start registered successfully!" -ForegroundColor Green
