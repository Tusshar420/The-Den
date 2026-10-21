@echo off
rem Double-click this file to install The Den. It starts the PowerShell script next to it and
rem keeps the window open at the end, so you can read what happened.
title Install The Den
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
