@echo off
title TimeTrack - Stop Server
color 0C
echo.
echo  Stopping TimeTrack server...
taskkill /f /im python.exe >nul 2>&1
echo  Done. TimeTrack is no longer running.
echo  (It will start again on next Windows boot.)
echo.
pause
