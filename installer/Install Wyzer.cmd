@echo off
setlocal
title Wyzer Setup
color 0B
set "WYZER_INSTALLER=%~dp0install.ps1"

if not exist "%WYZER_INSTALLER%" (
    echo Wyzer installer not found: "%WYZER_INSTALLER%"
    pause
    exit /b 1
)

echo ============================================================
echo                       WYZER SETUP
echo ============================================================
echo.
echo This one setup installs Wyzer and anything it needs.
echo Keep this window open. The first install downloads several GB
echo and can take a while depending on the PC and internet speed.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%WYZER_INSTALLER%" %*
set "WYZER_INSTALL_EXIT=%ERRORLEVEL%"

if not "%WYZER_INSTALL_EXIT%"=="0" (
    echo.
    echo Wyzer installation did not complete. Exit code: %WYZER_INSTALL_EXIT%
    echo Details were saved to:
    echo   "%LOCALAPPDATA%\Wyzer\install.log"
    echo.
    echo If your organization blocks PowerShell through Group Policy or AppLocker,
    echo contact the computer administrator; this launcher does not override those controls.
    pause
    exit /b %WYZER_INSTALL_EXIT%
)

echo.
echo Setup finished successfully. Wyzer is starting now.
exit /b 0
