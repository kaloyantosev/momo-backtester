"""Entrypoint for cloud platforms (Vercel, Render, Railway, FastAPI CLI).

If the real app fails to import (missing package, read-only FS, ...), a fallback app
is served that returns the traceback as JSON, so the error is visible in the browser.
"""
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from server.main import app
except Exception:  # noqa: BLE001
    _TB = traceback.format_exc()
    print(_TB, file=sys.stderr)
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="MOMO backtester (startup error)")

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def _startup_error(path: str = ""):
        return JSONResponse({"detail": "Server startup failed: " + _TB.strip().splitlines()[-1],
                             "traceback": _TB}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
