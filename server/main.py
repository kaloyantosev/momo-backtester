"""Local backtest server.  Run:  .venv/bin/python -m uvicorn server.main:app --port 8000"""
from __future__ import annotations

import ctypes
import gc
import json
import math
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel
import tempfile

from .data import tradingview as tv_mod
from .data.calendar import load_events
from .data.store import NEWS_FIRST, PAIRS, DataStore
from .engine.backtest import run_backtest, run_backtest_by_year
from .engine.strategy import DEFAULTS, build_events, merge_params, news_times, pine_indicator_signals, prepare, session_table

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

def _resolve_cache_dir() -> Path:
    # On serverless platforms (Vercel, AWS Lambda), the deployment root is read-only.
    # Fallback to the writable system temp directory.
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT"):
        d = Path(tempfile.gettempdir()) / "momo_data_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    try:
        d = ROOT / "data_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except (OSError, PermissionError):
        d = Path(tempfile.gettempdir()) / "momo_data_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

CACHE = _resolve_cache_dir()


def _load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


_load_env()
store = DataStore(CACHE)
app = FastAPI(title="EMA+ST+MACD backtester")

def _find_web_dir() -> Path | None:
    candidates = [
        ROOT / "web",
        Path.cwd() / "web",
        Path(__file__).resolve().parent.parent / "web",
        Path("/var/task/web"),
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None

WEB_DIR = _find_web_dir()
if WEB_DIR is not None and WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")



def _clean(o):
    """JSON-safe: NaN/inf -> None, numpy -> python."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def _local_epochs(ts: list | np.ndarray, tz: str) -> list[int]:
    """UTC epoch seconds -> 'wall clock' epoch seconds in tz (for chart axes)."""
    if len(ts) == 0:
        return []
    idx = pd.to_datetime(np.asarray(ts, dtype=np.int64), unit="s", utc=True).tz_convert(tz).tz_localize(None)
    return (idx.asi8 // 10**9).tolist()


def _release_memory() -> None:
    """Hand freed memory back to the system; the C allocator otherwise keeps it for later."""
    gc.collect()
    try:
        libc = ctypes.CDLL(None)
        if sys.platform == "darwin":
            libc.malloc_zone_pressure_relief(None, 0)
        elif sys.platform.startswith("linux"):
            libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _period(s: str, e: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    a = pd.Timestamp(s, tz="UTC")
    b = pd.Timestamp(e, tz="UTC") + pd.Timedelta(days=1)  # end date inclusive
    if b <= a:
        raise HTTPException(400, "Period end must be after start")
    return a, b


def _load(P: dict, a: pd.Timestamp, b: pd.Timestamp, refresh: bool, keep: bool = True):
    pairs = [p for p in P["pairs"] if p in PAIRS]
    if not pairs:
        raise HTTPException(400, "Select at least one pair")
    need = list(pairs)
    extra = []
    if "EURJPY" in pairs and "USDJPY" not in pairs:
        need.append("USDJPY")
        extra.append("USDJPY")
    data = store.get_many(need, P["source"], a, b, refresh, keep)
    events = load_events(CACHE / "calendar", (a - pd.Timedelta(days=3)).date(),
                         (b + pd.Timedelta(days=10)).date())
    return {p: data[p] for p in pairs}, {p: data[p] for p in extra}, events


class BacktestReq(BaseModel):
    params: dict = {}
    periods: list[dict]
    refresh: bool = False


class ChartReq(BaseModel):
    params: dict = {}
    pair: str
    center: str
    start: str
    end: str
    before: int = 90
    after: int = 90


class SettingsReq(BaseModel):
    oanda_token: str | None = None
    oanda_env: str | None = None
    tv_sessionid: str | None = None
    tv_sessionid_sign: str | None = None


@app.get("/")
def index():
    if WEB_DIR is not None:
        idx = WEB_DIR / "index.html"
        if idx.is_file():
            return FileResponse(idx)
    return HTMLResponse("<h1>MOMO Backtester</h1><p>Server is running.</p>")



@app.get("/api/config")
def config():
    return _clean({
        "defaults": DEFAULTS, "pairs": PAIRS,
        "sources": {"tradingview": True, "oanda": bool(os.environ.get("OANDA_TOKEN")), "forexsb": True},
        "tv_login": bool(os.environ.get("TV_SESSIONID")),
        "oanda_env": os.environ.get("OANDA_ENV", "practice"),
        "coverage": store.coverage(),
    })


@app.post("/api/backtest")
def backtest(req: BacktestReq):
    P = merge_params(req.params)
    periods = [(p.get("name", f"P{i + 1}"), *_period(p["start"], p["end"])) for i, p in enumerate(req.periods)]
    lo = min(a for _, a, _ in periods)
    hi = max(b for _, _, b in periods)
    t0 = time.time()
    # OANDA history is large: hold one year at a time. Without the daily close a trade can stay open for
    # weeks, so those runs (and the small TradingView and forexsb files) load their whole range.
    by_year = P["source"] == "oanda" and P["close_at_day_end"]
    try:
        if by_year:
            pdatas, extra, events = None, None, None
            if req.refresh:
                _load(P, max(lo, hi - pd.Timedelta(days=62)), hi, True, keep=False)
        else:
            pdatas, extra, events = _load(P, lo, hi, req.refresh)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(502, f"Data load failed: {e}")
    t_load = time.time() - t0
    s5 = store.s5 if P["source"] == "oanda" else None
    client = store.oanda_client() if P["source"] == "oanda" else None
    loaded = {"sec": t_load, "events": 0 if by_year else len(events)}

    def load_year(params, x, y):
        _release_memory()
        t = time.time()
        out = _load(params, x, y, False, keep=False)
        loaded["sec"] += time.time() - t
        loaded["events"] += len(out[2])
        return out

    def execute(params, a, b):
        if by_year:
            return run_backtest_by_year(lambda x, y: load_year(params, x, y), params, a, b, hi, s5, client)
        return run_backtest(pdatas, events, params, a, b, s5, client, extra)

    runs = []
    for name, a, b in periods:
        t1 = time.time()
        try:
            res = execute(P, a, b)
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(500, f"Backtest failed: {e}")
        res["equity"]["tl"] = _local_epochs(res["equity"]["t"], P["tz"])
        # without 5s data some +30s momentum checks are undecidable; show what the strict reading gives
        amb = sum(1 for t in res["trades"] if "ambiguous" in (t["hook_check"] + t["entry_check"]))
        res["ambiguous_trades"] = amb
        if amb and P["momentum_no5s"] == "range_accept":
            strict = execute({**P, "momentum_no5s": "range_reject"}, a, b)
            res["strict_summary"] = strict["metrics"]["summary"]
        if P["use_news"] and a < NEWS_FIRST:
            res["notes"].append(f"News rules are inactive before {NEWS_FIRST:%Y-%m-%d}: the calendar has no earlier events.")
        res["runtime_sec"] = round(time.time() - t1, 2)
        runs.append({"name": name, "start": str(a.date()), "end": str((b - pd.Timedelta(days=1)).date()), "result": res})
    return JSONResponse(_clean({"runs": runs, "load_sec": round(loaded["sec"], 2), "params": P,
                                "events_loaded": int(loaded["events"])}), background=BackgroundTask(_release_memory))


@app.post("/api/chart")
def chart(req: ChartReq):
    ctx = _chart_context(req.params, req.pair, req.center, req.before, req.after)
    return JSONResponse(_clean(_chart_window(ctx, req.pair, req.center, req.before, req.after)))


@app.get("/api/table_days")
def table_days():
    """The user's backtesting table per day (percent results as fractions), for the day review."""
    f = ROOT / "calibration" / "table_days.json"
    return JSONResponse(json.loads(f.read_text()) if f.exists() else {})


class TradeChartsReq(BaseModel):
    params: dict = {}
    pair: str
    centers: list[str]
    start: str
    end: str
    before: int = 60
    after: int = 60


@app.post("/api/trade_charts")
def trade_charts(req: TradeChartsReq):
    """Chart windows for several trades of one pair."""
    return JSONResponse(_clean([_chart_window(_chart_context(req.params, req.pair, c, req.before, req.after), req.pair, c, req.before, req.after)
                                for c in req.centers[:50]]))


def _chart_context(params: dict, pair: str, center: str, before: int, after: int) -> dict:
    """Only the days around the window are loaded, whatever the period of the run was."""
    P = merge_params(params)
    c = pd.Timestamp(center).tz_convert("UTC").normalize()
    a = c - pd.Timedelta(days=before // 288 + 4)     # 288 bars a day; 4 days covers a weekend or holiday
    b = c + pd.Timedelta(days=after // 288 + 4)
    if pair not in PAIRS:
        raise HTTPException(400, "unknown pair")
    data = store.get(pair, P["source"], a, b, with_m1=False)
    events = load_events(CACHE / "calendar", (a - pd.Timedelta(days=3)).date(), (b + pd.Timedelta(days=10)).date())
    pp = prepare(data, P)
    eff, adj, hooks, bos, flips = build_events(pp, P)
    tab = session_table(pp, P)
    ntimes, nrows = news_times(events, pair, P)
    i1, i2 = pine_indicator_signals(pp, P, tab, ntimes)
    return dict(P=P, pp=pp, eff=eff, adj=adj, hooks=hooks, bos=bos, flips=flips, tab=tab,
                ntimes=ntimes, nrows=nrows, i1=i1, i2=i2)


def _chart_window(ctx: dict, pair: str, center_iso: str, before: int, after: int) -> dict:
    P, pp, eff, adj, hooks, bos, flips, tab = (ctx[k] for k in ("P", "pp", "eff", "adj", "hooks", "bos", "flips", "tab"))
    ntimes, nrows, i1, i2 = ctx["ntimes"], ctx["nrows"], ctx["i1"], ctx["i2"]
    center = int(pd.Timestamp(center_iso).timestamp())
    ci = int(np.searchsorted(pp.t5, center))
    lo, hi = max(ci - before, 0), min(ci + after, len(pp.t5))
    if hi <= lo:
        return {"error": "no bars around this time"}
    sl = slice(lo, hi)
    t = pp.t5[sl]
    tl = _local_epochs(t, P["tz"])
    marks = []
    for i, (d, k, ok, why) in hooks.items():
        if lo <= i < hi:
            marks.append({"i": i - lo, "kind": "hook", "d": d, "ok": ok, "k": k, "why": why})
    for i, (d, pierce, rng, ok, why) in bos.items():
        if lo <= i < hi:
            marks.append({"i": i - lo, "kind": "bos", "d": d, "ok": ok, "pierce": pierce, "range": rng, "why": why})
    for i, d in flips.items():
        if lo <= i < hi:
            marks.append({"i": i - lo, "kind": "st", "d": d})
    for i, d in i1:
        if lo <= i < hi:
            marks.append({"i": i - lo, "kind": "ind1", "d": d})
    for i, d in i2:
        if lo <= i < hi:
            marks.append({"i": i - lo, "kind": "ind2", "d": d})
    hist = pp.hist[sl]
    colors = []
    for j in range(lo, hi):
        hv, hp = pp.hist[j], pp.hist[j - 1] if j > 0 else np.nan
        colors.append(("#26a69a" if hv > hp else "#b2dfdb") if hv >= 0 else ("#ffcdd2" if hv > hp else "#ff5252"))
    news = []
    t_lo, t_hi = int(t[0]) - 3600, int(t[-1]) + 3600
    for nt, row in zip(ntimes, nrows):
        if t_lo <= nt <= t_hi:
            news.append({"t": int(nt), "tl": _local_epochs([int(nt)], P["tz"])[0], "title": f'{row["country"]} {row["title"]}',
                         "imp": int(row["importance"])})
    gate_ok = []
    from .engine.strategy import gate as gate_fn
    for j in range(lo, hi):
        gate_ok.append(bool(gate_fn(pp, P, tab, j)[0]))
    return {
        "t": t.tolist(), "tl": tl, "o": pp.o[sl].tolist(), "h": pp.h[sl].tolist(), "l": pp.l[sl].tolist(),
        "c": pp.c[sl].tolist(), "ema": pp.ema[sl].tolist(), "st": pp.st[sl].tolist(), "dir": pp.sdir[sl].tolist(),
        "hist": hist.tolist(), "hist_color": colors, "hist_adjusted": adj[sl].tolist(), "hist_eff_green": eff[sl].tolist(),
        "macd": pp.macd[sl].tolist(), "signal": pp.sig[sl].tolist(), "marks": marks, "news": news, "gate": gate_ok,
        "pip": pp.pip, "digits": 3 if pair.endswith("JPY") else 5,
    }


@app.post("/api/settings")
def settings(req: SettingsReq):
    cur = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cur[k.strip()] = v.strip()
    for k, v in (("OANDA_TOKEN", req.oanda_token), ("OANDA_ENV", req.oanda_env),
                 ("TV_SESSIONID", req.tv_sessionid), ("TV_SESSIONID_SIGN", req.tv_sessionid_sign)):
        if v is not None:
            cur[k] = v.strip()
            os.environ[k] = v.strip()
    try:
        ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in cur.items()))
        ENV_FILE.chmod(0o600)
    except OSError:
        pass

    status = {}
    if os.environ.get("OANDA_TOKEN"):
        try:
            store.oanda_client().check()
            status["oanda"] = "ok"
        except Exception as e:
            status["oanda"] = f"error: {e}"
    if os.environ.get("TV_SESSIONID"):
        try:
            store.tv_token()
            status["tradingview"] = "ok (logged in)"
        except Exception as e:
            status["tradingview"] = f"error: {e}"
    return status


_DL_JOB: dict = {"running": False, "status": {}, "log": [], "current": None}


@app.post("/api/tv/download")
def tv_download(body: dict):
    """Start a background download of as much TradingView history as the session allows."""
    if _DL_JOB.get("running"):
        raise HTTPException(409, "A download is already running")
    pairs = [p for p in body.get("pairs", PAIRS) if p in PAIRS]
    tfs = [t for t in body.get("tfs", ["5", "1", "60"]) if t in ("1", "5", "60")]
    since = pd.Timestamp(body.get("since", "2024-01-01"), tz="UTC")
    _DL_JOB.clear()
    _DL_JOB.update({"running": True, "status": {}, "log": [], "current": None, "started": time.time(),
                    "since": str(since.date()), "error": None})

    def work():
        try:
            store.tv_deep_download(pairs, tfs, since, _DL_JOB)
        except Exception as e:
            traceback.print_exc()
            _DL_JOB["error"] = str(e)
        finally:
            _DL_JOB["running"] = False
            _DL_JOB["current"] = None

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


@app.get("/api/tv/download")
def tv_download_status():
    return _clean({k: v for k, v in _DL_JOB.items()})


@app.post("/api/tv/download/cancel")
def tv_download_cancel():
    _DL_JOB["cancel"] = True
    return {"ok": True}


@app.get("/api/tv/entitlements")
def tv_entitlements():
    try:
        return store.tv_entitlements()
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/verify_tv")
def verify_tv(body: dict):
    """Compare the Python EMA / Supertrend / MACD with TradingView's own built-in studies (needs TV login)."""
    pair = body.get("pair", "EURUSD")
    P = merge_params(body.get("params", {}))
    token = store.tv_token()
    if token == "unauthorized_user_token":
        raise HTTPException(400, "TradingView does not run studies for anonymous sessions. Add your TradingView "
                                 "sessionid under Data connections first.")
    client = tv_mod.TVClient(token)
    specs = {
        "ema": ("STD;EMA", {"in_0": int(P["ema_len"])}),
        "supertrend": ("STD;Supertrend", {"in_0": int(P["st_atr"]), "in_1": float(P["st_factor"])}),
        "macd": ("STD;MACD", {"in_1": int(P["macd_fast"]), "in_2": int(P["macd_slow"]), "in_3": int(P["macd_signal"])}),
    }
    out = {}
    for name, (pid, inputs) in specs.items():
        bars, study, _ = client.get_bars(f"OANDA:{pair}", "5", 3000, max_more=0, study={"pine_id": pid, "inputs": inputs})
        if study is None:
            out[name] = {"error": "no study data returned"}
            continue
        df = bars.rename(columns={"o": "bo", "h": "bh", "l": "bl", "c": "bc"})
        from .data.store import PairData
        pp = prepare(PairData(pair, "tradingview", df, df.resample("1h").agg(
            {"bo": "first", "bh": "max", "bl": "min", "bc": "last"}).dropna(), None, False), P)
        s = study.reindex(df.index)
        warm = 500
        if name == "ema":
            mine, theirs = pp.ema, s["plot_0"].to_numpy(float)
        elif name == "supertrend":  # built-in plots: plot_0 = up-trend line, plot_1 = down-trend line
            up, dn = s["plot_0"].to_numpy(float), s["plot_1"].to_numpy(float)
            mine, theirs = pp.st, np.where(np.isnan(up), dn, up)
        else:
            mine, theirs = pp.hist, s["plot_0"].to_numpy(float)
        diff = np.abs(mine[warm:] - theirs[warm:])
        diff = diff[~np.isnan(diff)]
        out[name] = {"bars_compared": int(len(diff)), "max_abs_diff": float(diff.max()) if len(diff) else None,
                     "max_abs_diff_pips": float(diff.max() / pp.pip) if len(diff) else None,
                     "plots_available": list(study.columns)}
    return _clean(out)


# Alias routes without /api prefix to support all Vercel and reverse-proxy routing variations
app.add_api_route("/config", config, methods=["GET"])
app.add_api_route("/backtest", backtest, methods=["POST"])
app.add_api_route("/chart", chart, methods=["POST"])
app.add_api_route("/table_days", table_days, methods=["GET"])
app.add_api_route("/trade_charts", trade_charts, methods=["POST"])
app.add_api_route("/settings", settings, methods=["POST"])
app.add_api_route("/tv/download", tv_download, methods=["POST"])
app.add_api_route("/tv/download", tv_download_status, methods=["GET"])
app.add_api_route("/tv/download/cancel", tv_download_cancel, methods=["POST"])
app.add_api_route("/tv/entitlements", tv_entitlements, methods=["GET"])
app.add_api_route("/verify_tv", verify_tv, methods=["POST"])


@app.middleware("http")
async def normalize_api_path_middleware(request, call_next):
    path = request.scope.get("path", "")
    for prefix in ("/api/index.py", "/index.py", "/api/index", "/index"):
        if path.startswith(prefix):
            request.scope["path"] = path[len(prefix):] or "/"
            break
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server.main:app", host="0.0.0.0", port=port, reload=False)

