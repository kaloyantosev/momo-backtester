"""Minimal TradingView websocket client.

Used for:
  * historical OHLCV bars of OANDA:<PAIR> symbols (the exact feed the strategy is traded on)
  * running TradingView's own built-in studies (EMA / MACD / Supertrend) so the Python
    indicator replicas can be verified against TradingView's numbers.

Without a login TradingView serves roughly: 1m ~1 week, 5m ~4 weeks, 1H ~1.5-2 years.
With a TradingView `sessionid` cookie of a paid plan the limits are larger (plan dependent).
"""
from __future__ import annotations

import json
import random
import re
import string
import threading
import time
from typing import Any

import httpx
import pandas as pd
import websocket

WS_URL = "wss://data.tradingview.com/socket.io/websocket?type=chart"
HEADERS = ["Origin: https://www.tradingview.com", "User-Agent: Mozilla/5.0"]
# TradingView answers 429 when too many sockets are opened at once
_SOCKETS = threading.BoundedSemaphore(2)
HTTP_HEADERS = {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/",
                "User-Agent": "Mozilla/5.0"}


def _rand(prefix: str) -> str:
    return prefix + "".join(random.choice(string.ascii_lowercase) for _ in range(12))


def _frame(s: str) -> str:
    return f"~m~{len(s)}~m~{s}"


def _msg(func: str, params: list) -> str:
    return _frame(json.dumps({"m": func, "p": params}, separators=(",", ":")))


def auth_token_from_session(sessionid: str, sessionid_sign: str = "") -> str:
    """Exchange a TradingView browser `sessionid` cookie for a websocket auth token."""
    cookies = {"sessionid": sessionid}
    if sessionid_sign:
        cookies["sessionid_sign"] = sessionid_sign
    r = httpx.get("https://www.tradingview.com/", cookies=cookies, headers=HTTP_HEADERS, timeout=30,
                  follow_redirects=True)
    m = re.search(r'"auth_token":"([^"]+)"', r.text)
    if not m:
        raise RuntimeError("Could not obtain TradingView auth token from sessionid (expired cookie?)")
    return m.group(1)


class TVClient:
    def __init__(self, auth_token: str = "unauthorized_user_token", timeout: float = 30):
        self.auth_token = auth_token
        self.timeout = timeout

    # ------------------------------------------------------------------ helpers
    def _open(self):
        delay = 3.0
        for attempt in range(6):
            try:
                ws = websocket.create_connection(WS_URL, header=HEADERS, timeout=self.timeout)
                break
            except websocket.WebSocketBadStatusException as e:
                if e.status_code != 429 or attempt == 5:
                    raise
                time.sleep(delay)
                delay *= 2
        ws.send(_msg("set_auth_token", [self.auth_token]))
        return ws

    @staticmethod
    def _parts(raw: str):
        for part in re.split(r"~m~\d+~m~", raw):
            if not part:
                continue
            yield part

    # ------------------------------------------------------------------ bars
    def get_bars(self, symbol: str, interval: str, n_bars: int = 5000, max_more: int = 20,
                 study: dict | None = None, to_ts: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame | None, dict]:
        """Download as many bars as TradingView allows (initial n_bars + `max_more` pages).

        interval: TradingView resolution string ("1", "5", "60", ...).
        study: optional {"pine_id": "STD;MACD", "inputs": {"in_1": 12, ...}} to also run a
               built-in study on the same series.
        Returns (bars_df, study_df_or_None, symbol_info).
        """
        with _SOCKETS:
            return self._get_bars(symbol, interval, n_bars, max_more, study, to_ts)

    def _get_bars(self, symbol, interval, n_bars, max_more, study, to_ts=None):
        ws = self._open()
        cs = _rand("cs_")
        ws.send(_msg("chart_create_session", [cs, ""]))
        ws.send(_msg("resolve_symbol", [cs, "sds_sym_1",
                                        '={"symbol":"%s","adjustment":"splits","session":"regular"}' % symbol]))
        # to_ts: n_bars bars ending at that time (how the chart's "go to date" asks for older history)
        rng = ["bar_count", int(to_ts), n_bars] if to_ts else n_bars
        ws.send(_msg("create_series", [cs, "sds_1", "s1", "sds_sym_1", interval, rng, ""]))
        if study:
            meta = _study_meta(study["pine_id"])
            params: dict[str, Any] = {"text": meta["il"], "pineId": study["pine_id"],
                                      "pineVersion": meta["version"]}
            for k, v in study.get("inputs", {}).items():
                t = meta["input_types"].get(k, "integer")
                params[k] = {"v": v, "f": True, "t": t}
            ws.send(_msg("create_study", [cs, "st1", "st1", "sds_1", "Script@tv-scripting-101!", params]))

        bars: dict[float, list] = {}
        study_rows: dict[float, list] = {}
        info: dict = {}
        more = 0
        last_count = -1
        deadline = time.time() + 180
        study_done = study is None
        series_done = False
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            for part in self._parts(raw):
                if part.startswith("~h~"):
                    ws.send(_frame(part))
                    continue
                try:
                    d = json.loads(part)
                except ValueError:
                    continue
                m = d.get("m")
                p = d.get("p", [])
                if m == "symbol_resolved":
                    info = p[2]
                elif m in ("timescale_update", "du"):
                    payload = p[1] if len(p) > 1 else {}
                    for b in payload.get("sds_1", {}).get("s", []) or []:
                        bars[b["v"][0]] = b["v"]
                    st = payload.get("st1", {})
                    for row in st.get("st", []) or []:
                        study_rows[row["v"][0]] = row["v"]
                elif m == "series_completed":
                    series_done = True
                    if len(bars) != last_count and more < max_more:
                        last_count = len(bars)
                        more += 1
                        ws.send(_msg("request_more_data", [cs, "sds_1", n_bars]))
                        series_done = False
                elif m == "study_completed":
                    study_done = True
                elif m in ("critical_error", "protocol_error", "symbol_error", "series_error", "study_error"):
                    ws.close()
                    raise RuntimeError(f"TradingView error for {symbol}: {d}")
            if series_done and study_done:
                break
        ws.close()
        if not bars:
            if to_ts:
                return pd.DataFrame(columns=["o", "h", "l", "c", "v"], index=pd.DatetimeIndex([], tz="UTC", name="t")), None, info
            raise RuntimeError(f"TradingView returned no bars for {symbol} {interval}")
        df = pd.DataFrame(sorted(bars.values()), columns=["t", "o", "h", "l", "c", "v"][: len(next(iter(bars.values())))])
        df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
        df = df.set_index("t").sort_index()
        sdf = None
        if study_rows:
            ncol = max(len(v) for v in study_rows.values())
            sdf = pd.DataFrame(sorted(study_rows.values()), columns=["t"] + [f"plot_{i}" for i in range(ncol - 1)])
            sdf["t"] = pd.to_datetime(sdf["t"], unit="s", utc=True)
            sdf = sdf.set_index("t").sort_index()
        return df, sdf, info


_META_CACHE: dict[str, dict] = {}


def _study_meta(pine_id: str) -> dict:
    if pine_id in _META_CACHE:
        return _META_CACHE[pine_id]
    url = f"https://pine-facade.tradingview.com/pine-facade/translate/{pine_id.replace(';', '%3B')}/last"
    j = httpx.get(url, headers=HTTP_HEADERS, timeout=30).json()
    res = j["result"]
    mi = res["metaInfo"]
    types = {i["id"]: i.get("type", "integer") for i in mi.get("inputs", [])}
    meta = {"il": res["ilTemplate"], "version": mi.get("pine", {}).get("version", "last"), "input_types": types,
            "plots": [p["id"] for p in mi.get("plots", [])]}
    _META_CACHE[pine_id] = meta
    return meta
