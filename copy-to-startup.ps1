$src = "C:\Users\lpgvi\Time tracker\run-silent.vbs"
$startup = [System.Environment]::GetFolderPath("Startup")
$dst = Join-Path $startup "TimeTrack.vbs"
Copy-Item $src $dst -Force
Write-Host "Copied to: $dst" -ForegroundColor Green
Write-Host "TimeTrack will now auto-start on every login!" -ForegroundColor Green
