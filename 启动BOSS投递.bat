@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PYTHON="

if exist "%~dp0python\pythonw.exe" set "PYTHON=%~dp0python\pythonw.exe"
if not defined PYTHON if exist "%~dp0python\python.exe" set "PYTHON=%~dp0python\python.exe"

if not defined PYTHON for /f "delims=" %%U in ('whoami 2^>nul') do (
    if exist "C:\Users\%%U\.local\share\TeleAgent\runtimes\python\pythonw.exe" (
        set "PYTHON=C:\Users\%%U\.local\share\TeleAgent\runtimes\python\pythonw.exe"
    )
    if not defined PYTHON if exist "C:\Users\%%U\.local\share\TeleAgent\runtimes\python\python.exe" (
        set "PYTHON=C:\Users\%%U\.local\share\TeleAgent\runtimes\python\python.exe"
    )
)

if not defined PYTHON (
    where pythonw >nul 2>&1 && (
        set "PYTHON=pythonw"
    ) || (
        where python >nul 2>&1 && (
            set "PYTHON=python"
        ) || (
            echo Python not found.
            pause
            exit /b 1
        )
    )
)

echo Starting: %PYTHON%
start "" "%PYTHON%" boss_gui.py