@echo off
rem Double-click this file to remove The Den from this computer.
title Remove The Den
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
echo.
pause
