@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" (
    echo Missing .env. Configure it using .env.example first.
    exit /b 1
)
echo Local access: http://127.0.0.1:8000/
echo LAN access: http://YOUR-LAN-IP:8000/ (use ipconfig to find your IPv4 address)
uv run --locked --env-file .env uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
