@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  Streetwise — Trigger a code update on the Oracle Cloud server
REM  Runs deploy/update.sh on the server via SSH
REM
REM  EDIT THE THREE LINES BELOW before first use:
REM ─────────────────────────────────────────────────────────────────────────────

SET SSH_KEY=C:\Users\vasanthaganesh.arulm\Downloads\ssh-key-streetwise.key
SET SERVER_IP=REPLACE_WITH_YOUR_ORACLE_IP
SET SERVER_USER=ubuntu

REM ─────────────────────────────────────────────────────────────────────────────

echo.
echo  Streetwise Remote Update
echo  ─────────────────────────
echo  Connecting to %SERVER_USER%@%SERVER_IP% ...
echo.

ssh -i "%SSH_KEY%" -o StrictHostKeyChecking=no ^
    "%SERVER_USER%@%SERVER_IP%" ^
    "bash /opt/streetwise/deploy/update.sh"

IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Update failed. Check terminal output above.
) ELSE (
    echo.
    echo  Server updated successfully.
)

echo.
pause
