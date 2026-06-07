@echo off
chcp 65001 >nul
title Secondclass Auto Score - 2403740

set "DIR=%~dp0"
set "PY=%DIR%venv\Scripts\python.exe"
set "SCRIPT=%DIR%secondclass_auto_score.py"

if not "%~1"=="" goto run_args

goto menu

:run_args
"%PY%" "%SCRIPT%" %*
if errorlevel 1 pause
goto :eof

:menu
cls
echo ========================================
echo  Secondclass Auto Score - 2403740
echo ========================================
echo.
echo  1. Show score dashboard
echo  2. Signup dry-run preview
echo  3. Signup for real
echo  4. Probe action endpoints
echo  5. Start monitor
echo  6. Full auto mode
echo  7. access_token / ticket to SSID
echo  8. Launch GUI
echo  9. Scan new activities
echo  0. Exit
echo.
set /p "choice=Select: "

if "%choice%"=="1" "%PY%" "%SCRIPT%" status & pause & goto menu
if "%choice%"=="2" "%PY%" "%SCRIPT%" signup --dry-run --max 20 & pause & goto menu
if "%choice%"=="3" "%PY%" "%SCRIPT%" signup --max 10 & pause & goto menu
if "%choice%"=="4" "%PY%" "%SCRIPT%" probe & pause & goto menu
if "%choice%"=="5" "%PY%" "%SCRIPT%" monitor & pause & goto menu
if "%choice%"=="6" "%PY%" "%SCRIPT%" run --max-signup 10 & pause & goto menu
if "%choice%"=="7" "%PY%" "%SCRIPT%" ticket --json "%DIR%accounts.json" --student-id 2403740 & pause & goto menu
if "%choice%"=="8" start "" "%PY%" "%DIR%secondclass_auto_score_gui.py" & goto menu
if "%choice%"=="9" "%PY%" "%SCRIPT%" scan --range 80 & pause & goto menu
if "%choice%"=="0" exit /b 0
goto menu