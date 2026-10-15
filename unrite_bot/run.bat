@echo off
cd /d "%~dp0"
if not exist .env (
  echo First copy .env.example to .env and fill DISCORD_TOKEN.
  pause
  exit /b 1
)
python main.py
pause
