"""Entrypoint for cloud platforms (Vercel, Render, Railway, Koyeb, FastAPI CLI)"""
import os
from server.main import app

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server.main:app", host="0.0.0.0", port=port, reload=False)
