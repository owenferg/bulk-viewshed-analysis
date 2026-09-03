@echo off
setlocal
rem start the gui with the standard windows python launcher when available
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 bulk_viewshed_gui.py
) else (
    python bulk_viewshed_gui.py
)
if errorlevel 1 pause
