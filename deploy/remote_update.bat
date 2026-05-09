@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  Streetwise — Trigger a code update on the Oracle Cloud server
REM  Runs deploy/update.sh on the server via SSH
REM
REM  EDIT THE THREE LINES BELOW before first use:
REM ─────────────────────────────────────────────────────────────────────────────

SET SSH_KEY=C:\Users\vasanthaganesh.arulm\Downloads\claude\streetwise\ssh-key-2026-03-27.key
SET SERVER_IP=132.145.243.138
SET SERVER_USER=opc

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
