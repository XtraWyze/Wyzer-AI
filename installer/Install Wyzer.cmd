@echo off
setlocal
set "WYZER_INSTALLER=%~dp0install.ps1"

if not exist "%WYZER_INSTALLER%" (
    echo Wyzer installer not found: "%WYZER_INSTALLER%"
    pause
    exit /b 1
)

echo Starting the Wyzer installer...
echo This uses an execution-policy bypass for this installer process only.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%WYZER_INSTALLER%" %*
set "WYZER_INSTALL_EXIT=%ERRORLEVEL%"

if not "%WYZER_INSTALL_EXIT%"=="0" (
    echo.
    echo Wyzer installation did not complete. Exit code: %WYZER_INSTALL_EXIT%
    echo If your organization blocks PowerShell through Group Policy or AppLocker,
    echo contact the computer administrator; this launcher does not override those controls.
    pause
)

exit /b %WYZER_INSTALL_EXIT%
