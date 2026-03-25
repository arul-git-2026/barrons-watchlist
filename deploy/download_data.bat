@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  Streetwise — Download data FROM Oracle Cloud server to local machine
REM  Use this to pull tickers you added while away (work / holiday)
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
echo  Streetwise Data Sync  ^|  Download from Oracle Cloud
echo  ─────────────────────────────────────────────────────
echo  Server : %SERVER_USER%@%SERVER_IP%
echo  From   : %REMOTE_DIR%
echo  To     : %LOCAL_DIR%
echo.

REM Back up local file first
IF EXIST "%LOCAL_DIR%\streetwise_data.json" (
    copy /Y "%LOCAL_DIR%\streetwise_data.json" "%LOCAL_DIR%\streetwise_data.json.bak" > nul
    echo  Local backup saved: streetwise_data.json.bak
)

echo [1/1] Downloading streetwise_data.json ...
scp -i "%SSH_KEY%" -o StrictHostKeyChecking=no ^
    "%SERVER_USER%@%SERVER_IP%:%REMOTE_DIR%/streetwise_data.json" ^
    "%LOCAL_DIR%\streetwise_data.json"

IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Download failed. Restoring backup...
    copy /Y "%LOCAL_DIR%\streetwise_data.json.bak" "%LOCAL_DIR%\streetwise_data.json" > nul
    pause
    exit /b 1
)

echo  Download complete!
echo.
pause
