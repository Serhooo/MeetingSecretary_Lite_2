@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PATH=%~dp0runtime;%PATH%"
set "HF_HOME=%~dp0whisper-cache"
set "PYTHONUTF8=1"
set "PYTHONFAULTHANDLER=1"
if not exist "%~dp0logs" mkdir "%~dp0logs"
set "STARTUP_LOG=%~dp0logs\startup.log"
echo Meeting Secretary Lite 2 startup: %date% %time% > "%STARTUP_LOG%"
if not exist "%~dp0runtime\python.exe" (
    echo ERROR: runtime\python.exe not found. Copy the complete application folder.>> "%STARTUP_LOG%"
    type "%STARTUP_LOG%"
    goto finish
)
if not exist "%~dp0meeting_secretary_app.py" (
    echo ERROR: meeting_secretary_app.py not found. Copy the complete application folder.>> "%STARTUP_LOG%"
    type "%STARTUP_LOG%"
    goto finish
)
"%~dp0runtime\python.exe" -X faulthandler "%~dp0meeting_secretary_app.py" >> "%STARTUP_LOG%" 2>&1
set "APP_EXIT_CODE=%ERRORLEVEL%"
echo.>> "%STARTUP_LOG%"
echo Python exit code: %APP_EXIT_CODE%>> "%STARTUP_LOG%"
type "%STARTUP_LOG%"
if not "%APP_EXIT_CODE%"=="0" echo The application exited with an error. Send logs\startup.log and logs\application.log for diagnosis.
:finish
echo.
echo Startup log: "%~dp0logs\startup.log"
pause
