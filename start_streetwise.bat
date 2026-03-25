@echo off
start "Streetwise Server" cmd /k "cd /d C:\Users\arulv\Downloads\streetwise && python server.py"
timeout /t 3
start "Cloudflare Tunnel" cmd /k "cd /d C:\Users\arulv\Downloads\streetwise && cloudflared.exe tunnel --url http://localhost:5000"