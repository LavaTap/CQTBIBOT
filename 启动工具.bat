@echo off
chcp 936 >nul 2>&1
title SSO Tools
set DIR=%~dp0
set DIR=%DIR:~0,-1%
set PY=%DIR%\venv\Scripts\python.exe

:menu
cls
echo ========================================
echo   SSO Tools
echo ========================================
echo.
echo   1. SSO Login
echo   2. Schedule
echo   3. Capture
echo   4. QQ Forward
echo   5. # Commands
echo   6. History Forward
echo   0. Exit
echo.
set /p choice=Select:

if "%choice%"=="1" goto sso_login
if "%choice%"=="2" goto schedule
if "%choice%"=="3" goto capture
if "%choice%"=="4" goto qq_forward
if "%choice%"=="5" goto commands
if "%choice%"=="6" goto history_forward
if "%choice%"=="0" exit
goto menu

:sso_login
"%PY%" "%DIR%\sso_login.py"
pause
goto menu

:schedule
"%PY%" "%DIR%\schedule_tool.py"
pause
goto menu

:capture
"%PY%" "%DIR%\sso_tool.py"
pause
goto menu

:qq_forward
"%PY%" "%DIR%\qq_forward.py"
pause
goto menu

:commands
"%PY%" "%DIR%\monitor_forward.py"
pause
goto menu

:history_forward
"%PY%" "%DIR%\qq_history.py"
pause
goto menu
