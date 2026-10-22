"""OANDA v20 REST client (practice or live token).

OANDA is the price provider behind TradingView's OANDA:<PAIR> symbols, so this gives the same
feed with deep history, real bid/ask and 5-second (S5) candles for sub-minute precision.
A free practice (demo) account token is enough.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import httpx
import pandas as pd

HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}


def instrument(pair: str) -> str:
    return pair[:3] + "_" + pair[3:]


class OandaClient:
    def __init__(self, token: str, env: str = "practice"):
        if not token:
            raise ValueError("OANDA token missing")
        self.base = HOSTS.get(env, HOSTS["practice"])
        self._local = threading.local()
        self.token = token

    @property
    def http(self) -> httpx.Client:
        c = getattr(self._local, "client", None)
        if c is None:
            c = httpx.Client(headers={"Authorization": f"Bearer {self.token}",
                                      "Accept-Datetime-Format": "UNIX"}, timeout=60)
            self._local.client = c
        return c

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(6):
            r = self.http.get(self.base + path, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise RuntimeError("OANDA rejected the token (401). Check token and practice/live setting.")
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def check(self) -> bool:
        self._get("/v3/accounts", {})
        return True

    def candles(self, pair: str, granularity: str, start: dt.datetime, end: dt.datetime,
                count: int = 5000) -> pd.DataFrame:
        """All complete bid/ask candles with start time in [start, end)."""
        rows = []
        cur = start.timestamp()
        end_ts = end.timestamp()
        first = True
        while cur < end_ts:
            params = {"granularity": granularity, "price": "BA", "from": f"{cur:.0f}", "count": count,
                      "smooth": "false", "includeFirst": "true" if first else "false"}
            data = self._get(f"/v3/instruments/{instrument(pair)}/candles", params)
            cs = data.get("candles", [])
            if not cs:
                break
            for c in cs:
                t = float(c["time"])
                if t >= end_ts:
                    break
                if not c.get("complete", True):
                    continue
                b, a = c["bid"], c["ask"]
                rows.append((t, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                             float(a["o"]), float(a["h"]), float(a["l"]), float(a["c"]), int(c["volume"])))
            last_t = float(cs[-1]["time"])
            if len(cs) < count or last_t >= end_ts or (last_t <= cur and not first):
                break
            cur = last_t
            first = False
        df = pd.DataFrame(rows, columns=["t", "bo", "bh", "bl", "bc", "ao", "ah", "al", "ac", "v"])
        df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
        return df.drop_duplicates("t").set_index("t").sort_index()

    def s5_window(self, pair: str, start: dt.datetime, n: int) -> pd.DataFrame:
        """`n` S5 candles starting at `start` (includes incomplete=False only)."""
        params = {"granularity": "S5", "price": "BA", "from": f"{start.timestamp():.0f}", "count": n,
                  "smooth": "false", "includeFirst": "true"}
        data = self._get(f"/v3/instruments/{instrument(pair)}/candles", params)
        rows = []
        for c in data.get("candles", []):
            b, a = c["bid"], c["ask"]
            rows.append((float(c["time"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                         float(a["o"]), float(a["h"]), float(a["l"]), float(a["c"])))
        return pd.DataFrame(rows, columns=["t", "bo", "bh", "bl", "bc", "ao", "ah", "al", "ac"])
