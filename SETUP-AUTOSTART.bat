@echo off
title TimeTrack - Auto-Start Setup
color 0A
echo.
echo  =======================================
echo    TimeTrack - Setting up Auto-Start
echo  =======================================
echo.
echo  TimeTrack will start automatically every
echo  time you log into Windows. No console
echo  window, no manual start needed.
echo.

set FOLDER=%~dp0
set FOLDER=%FOLDER:~0,-1%
set SRC=%FOLDER%\run-silent.vbs

powershell -ExecutionPolicy Bypass -Command "$src='%SRC%'; $dst=Join-Path ([Environment]::GetFolderPath('Startup')) 'TimeTrack.vbs'; Copy-Item $src $dst -Force; Write-Host 'Done'"

if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Setup failed. Try right-clicking
    echo  this file and selecting "Run as administrator"
    echo.
) else (
    echo.
    echo  =======================================
    echo   SUCCESS! Auto-start is now active.
    echo  =======================================
    echo.
    echo  Starting TimeTrack now in the background...
    wscript.exe "%SRC%"
    timeout /t 2 /nobreak >nul
    echo  Done! Open your browser and go to:
    echo  http://localhost:5000
    echo.
)
pause
