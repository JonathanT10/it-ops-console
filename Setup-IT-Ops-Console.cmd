@echo off
setlocal
title IT Ops Console setup
rem ------------------------------------------------------------------
rem  Double-click me. That is the whole instruction.
rem  Downloads setup.ps1 next to this file if it is not already here,
rem  then runs it with the right settings - no PowerShell knowledge,
rem  no right-click menus, no execution policy.
rem ------------------------------------------------------------------
set "HERE=%~dp0"
set "ITOPS_CMD=1"
if exist "%HERE%setup.ps1" goto run
echo Downloading setup.ps1 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor 3072; Invoke-WebRequest 'https://raw.githubusercontent.com/JonathanT10/it-ops-console/main/setup.ps1' -OutFile '%HERE%setup.ps1' -UseBasicParsing"
if not exist "%HERE%setup.ps1" (
  echo.
  echo Could not download setup.ps1 - check your internet connection and try again.
  pause
  exit /b 1
)
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%setup.ps1"
echo.
pause
