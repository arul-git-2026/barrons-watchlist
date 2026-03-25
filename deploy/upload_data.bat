@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  Streetwise — Upload local data to Oracle Cloud server
REM  Double-click to run, or call from a terminal.
REM
REM  EDIT THE THREE LINES BELOW before first use:
REM ─────────────────────────────────────────────────────────────────────────────

SET SSH_KEY=C:\Users\vasanthaganesh.arulm\Downloads\ssh-key-streetwise.key
SET SERVER_IP=REPLACE_WITH_YOUR_ORACLE_IP
SET SERVER_USER=ubuntu

REM ─────────────────────────────────────────────────────────────────────────────
SET LOCAL_DIR=%~dp0..
SET REMOTE_DIR=/opt/streetwise

echo.
echo  Streetwise Data Sync  ^|  Upload to Oracle Cloud
echo  ────────────────────────────────────────────────
echo  Server : %SERVER_USER%@%SERVER_IP%
echo  From   : %LOCAL_DIR%
echo  To     : %REMOTE_DIR%
echo.

REM Upload streetwise_data.json
echo [1/2] Uploading streetwise_data.json ...
scp -i "%SSH_KEY%" -o StrictHostKeyChecking=no ^
    "%LOCAL_DIR%\streetwise_data.json" ^
    "%SERVER_USER%@%SERVER_IP%:%REMOTE_DIR%/streetwise_data.json"

IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Upload failed. Check SSH key path and server IP.
    pause
    exit /b 1
)
echo       Done.

REM Optional: upload price_history.db (comment out if you want the server to rebuild it)
echo [2/2] Uploading price_history.db ...
IF EXIST "%LOCAL_DIR%\price_history.db" (
    scp -i "%SSH_KEY%" -o StrictHostKeyChecking=no ^
        "%LOCAL_DIR%\price_history.db" ^
        "%SERVER_USER%@%SERVER_IP%:%REMOTE_DIR%/price_history.db"
    echo       Done.
) ELSE (
    echo       price_history.db not found locally - skipping.
    echo       Server will rebuild it from Yahoo Finance on first use.
)

echo.
echo  Upload complete!
echo  Dashboard: http://%SERVER_IP%:5000
echo.
pause
