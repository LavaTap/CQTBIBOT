@echo off
chcp 936 >nul 2>&1
title 二课总表调度器
set DIR=%~dp0
set DIR=%DIR:~0,-1%
set PY=%DIR%\venv\Scripts\python.exe

"%PY%" "%DIR%\secondclass_tool_gui.py"
pause
