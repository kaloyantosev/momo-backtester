#!/bin/bash
# Start the local backtest server on http://127.0.0.1:8000
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
(sleep 2 && open "http://127.0.0.1:8000") >/dev/null 2>&1 &
exec .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
