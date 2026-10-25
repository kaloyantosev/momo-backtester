@echo off
cd /d "%~dp0"

if not exist .venv_win (
    echo Creating virtual environment...
    python -m venv .venv_win
    call .venv_win\Scripts\pip install -r requirements.txt
)

echo Starting MOMO Backtester...
start "" http://127.0.0.1:8000
.venv_win\Scripts\python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 8000
pause
