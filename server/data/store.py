"""Price data store: downloads, caches (parquet / sqlite) and serves bars per pair.

Column convention (all prices):
  bo,bh,bl,bc  bid OHLC   (for TradingView data: the chart price, which TradingView plots as bid)
  ao,ah,al,ac  ask OHLC   (OANDA only; TradingView data has no ask -> engine adds a fixed spread)
Index: bar OPEN time, UTC.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import oanda as oanda_mod
from . import tradingview as tv_mod

PAIRS = ["EURJPY", "EURUSD", "AUDUSD", "GBPUSD", "USDJPY", "USDCHF"]
BID = ["bo", "bh", "bl", "bc"]
ASK = ["ao", "ah", "al", "ac"]


def pip_size(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


def tick_size(pair: str) -> float:
    return pip_size(pair) / 10


@dataclass
class PairData:
    pair: str
    source: str
    m5: pd.DataFrame
    h1: pd.DataFrame
    m1: pd.DataFrame | None
    has_ask: bool
    notes: list[str] = field(default_factory=list)


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {}
    for side in ("b", "a"):
        if f"{side}o" in df.columns:
            agg.update({f"{side}o": "first", f"{side}h": "max", f"{side}l": "min", f"{side}c": "last"})
    if "v" in df.columns:
        agg["v"] = "sum"
    out = df.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["bo"])


class S5Cache:
    """5-second OANDA candles, fetched lazily in 5-minute blocks and kept in sqlite."""

    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS s5 (pair TEXT, t INTEGER, bo REAL, bh REAL, bl REAL, bc REAL,"
                        " ao REAL, ah REAL, al REAL, ac REAL, PRIMARY KEY(pair, t))")
        self.db.execute("CREATE TABLE IF NOT EXISTS blocks (pair TEXT, b INTEGER, PRIMARY KEY(pair, b))")
        self.db.commit()
        self._have: dict[str, set[int]] = {}

    def _known(self, pair: str) -> set[int]:
        if pair not in self._have:
            with self.lock:
                rows = self.db.execute("SELECT b FROM blocks WHERE pair=?", (pair,)).fetchall()
            self._have[pair] = {r[0] for r in rows}
        return self._have[pair]

    def ensure(self, client: oanda_mod.OandaClient, items: list[tuple[str, int]], workers: int = 8):
        todo = sorted({(p, int(b // 300 * 300)) for p, b in items if int(b // 300 * 300) not in self._known(p)})
        if not todo:
            return

        def fetch(item):
            p, b = item
            df = client.s5_window(p, dt.datetime.fromtimestamp(b, dt.timezone.utc), 60)
            df = df[(df["t"] >= b) & (df["t"] < b + 300)]
            return p, b, df

        with ThreadPoolExecutor(workers) as ex:
            for p, b, df in ex.map(fetch, todo):
                with self.lock:
                    self.db.executemany("INSERT OR REPLACE INTO s5 VALUES (?,?,?,?,?,?,?,?,?,?)",
                                        [(p, int(r.t), r.bo, r.bh, r.bl, r.bc, r.ao, r.ah, r.al, r.ac)
                                         for r in df.itertuples()])
                    self.db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?)", (p, b))
                self._known(p).add(b)
        with self.lock:
            self.db.commit()

    def get(self, pair: str, t0: int, t1: int) -> np.ndarray:
        """rows with t in [t0, t1): columns t,bo,bh,bl,bc,ao,ah,al,ac"""
        with self.lock:
            rows = self.db.execute("SELECT t,bo,bh,bl,bc,ao,ah,al,ac FROM s5 WHERE pair=? AND t>=? AND t<? ORDER BY t",
                                   (pair, int(t0), int(t1))).fetchall()
        return np.array(rows, dtype=float).reshape(-1, 9)

    def has_block(self, pair: str, t: int) -> bool:
        return int(t // 300 * 300) in self._known(pair)


class DataStore:
    def __init__(self, cache_dir: Path):
        self.cache = cache_dir
        (self.cache / "tv").mkdir(parents=True, exist_ok=True)
        (self.cache / "oanda").mkdir(parents=True, exist_ok=True)
        self.s5 = S5Cache(self.cache / "oanda" / "s5.sqlite")
        self._mem: dict = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------- credentials
    @staticmethod
    def oanda_client() -> oanda_mod.OandaClient | None:
        tok = os.environ.get("OANDA_TOKEN", "").strip()
        if not tok:
            return None
        return oanda_mod.OandaClient(tok, os.environ.get("OANDA_ENV", "practice").strip() or "practice")

    @staticmethod
    def tv_token() -> str:
        sid = os.environ.get("TV_SESSIONID", "").strip()
        if not sid:
            return "unauthorized_user_token"
        return tv_mod.auth_token_from_session(sid, os.environ.get("TV_SESSIONID_SIGN", "").strip())

    # ------------------------------------------------------------- TradingView
    def _tv_update(self, pair: str, force: bool = False):
        files = [self.cache / "tv" / f"{pair}_{tf}.parquet" for tf in ("1", "5", "60")]
        fresh = all(f.exists() and time.time() - f.stat().st_mtime < 15 * 60 for f in files)
        if (fresh and not force) or (getattr(self, "downloading", False) and all(f.exists() for f in files)):
            return
        client = tv_mod.TVClient(self.tv_token())

        def one(spec):
            tf, n, more = spec
            new, _, _ = client.get_bars(f"OANDA:{pair}", tf, n, max_more=more)
            return tf, new

        with ThreadPoolExecutor(3) as ex:
            got = list(ex.map(one, (("1", 5000, 30), ("5", 5000, 30), ("60", 5000, 6))))
        for tf, new in got:
            f = self.cache / "tv" / f"{pair}_{tf}.parquet"
            new = new.rename(columns={"o": "bo", "h": "bh", "l": "bl", "c": "bc"})
            if f.exists():
                old = pd.read_parquet(f)
                # newer download wins for overlapping bars (last bar may have been incomplete)
                new = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
            new.to_parquet(f)

    def _tv_load(self, pair: str, start: pd.Timestamp, end: pd.Timestamp, force: bool) -> PairData:
        self._tv_update(pair, force)
        m1 = pd.read_parquet(self.cache / "tv" / f"{pair}_1.parquet")
        m5 = pd.read_parquet(self.cache / "tv" / f"{pair}_5.parquet")
        h1 = pd.read_parquet(self.cache / "tv" / f"{pair}_60.parquet")
        # drop the still-forming last bar of each series
        now = pd.Timestamp.now(tz="UTC")
        m1 = m1[m1.index + pd.Timedelta(minutes=1) <= now]
        m5 = m5[m5.index + pd.Timedelta(minutes=5) <= now]
        h1 = h1[h1.index + pd.Timedelta(hours=1) <= now]
        notes = []
        if len(m5) and m5.index[0] > start:
            notes.append(f"{pair}: TradingView 5m history starts {m5.index[0]:%Y-%m-%d %H:%M} UTC "
                         f"(requested {start:%Y-%m-%d}). Earlier dates are NOT tested.")
        if len(m1) and m1.index[0] > start:
            notes.append(f"{pair}: 1m bars only from {m1.index[0]:%Y-%m-%d %H:%M} UTC; before that intrabar "
                         f"order is resolved on 5m bars (pessimistic).")
        sl = slice(start - pd.Timedelta(days=12), end)
        return PairData(pair, "tradingview", m5.loc[sl], h1.loc[start - pd.Timedelta(days=60):end],
                        m1.loc[sl] if len(m1) else None, False, notes)

    def tv_deep_download(self, pairs: list[str], tfs: list[str], since: pd.Timestamp, job: dict):
        """Page TradingView history backwards ("go to date" requests) until `since` or until the
        account's limit, merging everything into the local cache. `job` receives progress."""
        self.downloading = True
        try:
            self._tv_deep(pairs, tfs, since, job)
        finally:
            self.downloading = False

    def _tv_deep(self, pairs, tfs, since, job):
        client = tv_mod.TVClient(self.tv_token())
        for pair in pairs:
            for tf in tfs:
                if job.get("cancel"):
                    job["log"].append("cancelled")
                    return
                f = self.cache / "tv" / f"{pair}_{tf}.parquet"
                key = f"{pair} {tf}"
                job["current"] = key
                latest, _, _ = client.get_bars(f"OANDA:{pair}", tf, 5000, max_more=30)
                latest = latest.rename(columns={"o": "bo", "h": "bh", "l": "bl", "c": "bc"})
                df = latest
                if f.exists():
                    old = pd.read_parquet(f)
                    df = pd.concat([old[~old.index.isin(latest.index)], latest]).sort_index()
                df.to_parquet(f)
                pages = 0
                while df.index[0] > since and pages < 2000 and not job.get("cancel"):
                    earliest = df.index[0]
                    older, _, _ = client.get_bars(f"OANDA:{pair}", tf, 5000, max_more=0,
                                                  to_ts=int(earliest.timestamp()) - 1)
                    older = older.rename(columns={"o": "bo", "h": "bh", "l": "bl", "c": "bc"})
                    older = older[older.index < earliest]
                    if older.empty:
                        break
                    df = pd.concat([older, df]).sort_index()
                    df = df[~df.index.duplicated(keep="last")]
                    df.to_parquet(f)
                    pages += 1
                    job["status"][key] = [str(df.index[0])[:16], str(df.index[-1])[:16], len(df)]
                    time.sleep(0.4)  # be gentle with the account
                job["status"][key] = [str(df.index[0])[:16], str(df.index[-1])[:16], len(df)]
                reached = "reached requested date" if df.index[0] <= since else "account/feed limit reached"
                job["log"].append(f"{key}: {df.index[0]:%Y-%m-%d %H:%M} → {df.index[-1]:%Y-%m-%d %H:%M} "
                                  f"({len(df)} bars, {pages} older pages; {reached})")

    def tv_entitlements(self) -> dict:
        """What the configured TradingView session can see (depth per timeframe, seconds bars)."""
        client = tv_mod.TVClient(self.tv_token())
        out = {"logged_in": client.auth_token != "unauthorized_user_token"}
        for tf in ("1", "5"):
            try:
                df, _, _ = client.get_bars("OANDA:EURUSD", tf, 5000, max_more=30)
                out[f"{tf}m"] = f"{len(df)} bars back to {df.index[0]:%Y-%m-%d}"
            except Exception as e:
                out[f"{tf}m"] = f"error: {e}"
        try:
            df, _, _ = client.get_bars("OANDA:EURUSD", "5S", 500, max_more=0)
            out["seconds"] = f"available ({len(df)} bars of 5s, back to {df.index[0]:%Y-%m-%d %H:%M})"
        except Exception as e:
            out["seconds"] = "not available" if "seconds_not_entitled" in str(e) else f"error: {e}"
        return out

    # ------------------------------------------------------------- OANDA
    def _oanda_month(self, client, pair: str, y: int, m: int, force: bool) -> pd.DataFrame:
        d = self.cache / "oanda" / pair
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"M1_{y:04d}-{m:02d}.parquet"
        a = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        b = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=dt.timezone.utc)
        now = dt.datetime.now(dt.timezone.utc)
        complete = b <= now - dt.timedelta(hours=1)
        if f.exists() and not force:
            if complete or time.time() - f.stat().st_mtime < 15 * 60:
                return pd.read_parquet(f)
        df = client.candles(pair, "M1", a, min(b, now))
        df.to_parquet(f)
        return df

    def _oanda_load(self, pair: str, start: pd.Timestamp, end: pd.Timestamp, force: bool) -> PairData:
        client = self.oanda_client()
        if client is None:
            raise RuntimeError("OANDA source selected but no OANDA_TOKEN configured (see Data connections).")
        a = (start - pd.Timedelta(days=25)).to_pydatetime()
        b = end.to_pydatetime()
        months = []
        y, m = a.year, a.month
        while (y, m) <= (b.year, b.month):
            months.append((y, m))
            m += 1
            if m == 13:
                y, m = y + 1, 1
        with ThreadPoolExecutor(4) as ex:
            parts = list(ex.map(lambda ym: self._oanda_month(client, pair, ym[0], ym[1], force), months))
        m1 = pd.concat([p for p in parts if len(p)]).sort_index()
        m1 = m1[~m1.index.duplicated(keep="last")]
        m5 = resample(m1, "5min")
        h1 = resample(m1, "1h")
        notes = []
        if len(m5) and m5.index[0] > start:
            notes.append(f"{pair}: OANDA data starts {m5.index[0]:%Y-%m-%d}.")
        sl = slice(start - pd.Timedelta(days=12), end)
        return PairData(pair, "oanda", m5.loc[sl], h1.loc[start - pd.Timedelta(days=25):end], m1.loc[sl], True, notes)

    # ------------------------------------------------------------- public
    def get_many(self, pairs: list[str], source: str, start: pd.Timestamp, end: pd.Timestamp,
                 force: bool = False) -> dict[str, PairData]:
        with ThreadPoolExecutor(6) as ex:
            res = list(ex.map(lambda p: self.get(p, source, start, end, force), pairs))
        return dict(zip(pairs, res))

    def get(self, pair: str, source: str, start: pd.Timestamp, end: pd.Timestamp, force: bool = False) -> PairData:
        key = (pair, source, start, end)
        with self.lock:
            if not force and key in self._mem and time.time() - self._mem[key][0] < 10 * 60:
                return self._mem[key][1]
        if source == "oanda":
            pdata = self._oanda_load(pair, start, end, force)
        else:
            pdata = self._tv_load(pair, start, end, force)
        with self.lock:
            self._mem[key] = (time.time(), pdata)
        return pdata

    def coverage(self) -> dict:
        out = {}
        for pair in PAIRS:
            info = {}
            for tf in ("1", "5", "60"):
                f = self.cache / "tv" / f"{pair}_{tf}.parquet"
                if f.exists():
                    idx = pd.read_parquet(f, columns=["bc"]).index
                    info[f"tv_{tf}"] = [str(idx[0]), str(idx[-1]), len(idx)]
            d = self.cache / "oanda" / pair
            if d.exists():
                files = sorted(d.glob("M1_*.parquet"))
                if files:
                    info["oanda_months"] = [files[0].stem[3:], files[-1].stem[3:], len(files)]
            out[pair] = info
        return out
