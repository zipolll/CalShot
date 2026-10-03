@echo off
set "CalShotSigner=%~dp0scripts\sign-shortcut.ps1"
if not exist "%CalShotSigner%" (
    echo CalShot signing tool not found. Keep this file in the project root.
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CalShotSigner%" -Clean %*
exit /b %errorlevel%
