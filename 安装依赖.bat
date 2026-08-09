@echo off
echo ============================================
echo  BOSS Auto-deliver - Dependency Installer
echo ============================================
echo.

set "PYTHON="

if exist "%~dp0python\python.exe" set "PYTHON=%~dp0python\python.exe"

if not defined PYTHON for /f "delims=" %%U in ('whoami 2^>nul') do (
    if exist "C:\Users\%%U\.local\share\TeleAgent\runtimes\python\python.exe" (
        set "PYTHON=C:\Users\%%U\.local\share\TeleAgent\runtimes\python\python.exe"
    )
)

if not defined PYTHON where python >nul 2>&1 && set "PYTHON=python"
if not defined PYTHON where python3 >nul 2>&1 && set "PYTHON=python3"

if not defined PYTHON (
    echo Python not found. Please install Python 3.12+ first.
    pause
    exit /b 1
)

echo Using: %PYTHON%
echo.
echo Installing dependencies to libs folder...
echo.

"%PYTHON%" -m pip install --target "%~dp0libs" opencv-python numpy rapidocr-onnxruntime onnxruntime

echo.
echo ============================================
echo  Done!
echo ============================================
pause