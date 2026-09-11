@echo off
rem Double-click launcher: starts API + Worker (calls scripts\start.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
pause
