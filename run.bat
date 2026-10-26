@echo off
rem Runs NZ Height Fix from source with its own private Python 3.13 (kept in .tools here).
rem Most people should use the installer from the Releases page instead.
cd /d "%~dp0"
for /f "delims=" %%u in ('powershell -NoProfile -ExecutionPolicy Bypass -File scripts\get-uv.ps1') do set "UV=%%u"
if not exist "%UV%" (
    echo Could not download uv - check your internet connection and try again.
    pause
    exit /b 1
)
set "UV_PYTHON_INSTALL_DIR=%~dp0.tools\python"
set "UV_CACHE_DIR=%~dp0.tools\cache"
set "UV_PYTHON_PREFERENCE=only-managed"
echo Starting NZ Height Fix... (the first start downloads Python and libraries, 1-2 minutes)
"%UV%" run --no-project --python 3.13 --with-requirements requirements.txt nz_height_fix.py %*
if errorlevel 1 pause
