"""Price data store: downloads, caches (parquet / sqlite) and serves bars per pair.

Column convention (all prices):
  mo,mh,ml,mc  mid OHLC   (OANDA only; these are the candles TradingView's OANDA: charts plot)
  bo,bh,bl,bc  bid OHLC   (for TradingView data: the chart price, which is really OANDA mid)
  ao,ah,al,ac  ask OHLC   (OANDA only; TradingView data has no ask -> engine adds a fixed spread)
Index: bar OPEN time, UTC.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

from . import forexsb as fsb_mod
from . import oanda as oanda_mod
from . import tradingview as tv_mod

pa.set_memory_pool(pa.system_memory_pool())   # the default pool keeps freed Parquet buffers for itself

PAIRS = ["EURJPY", "EURUSD", "AUDUSD", "GBPUSD", "USDJPY", "USDCHF"]
BID = ["bo", "bh", "bl", "bc"]
ASK = ["ao", "ah", "al", "ac"]
MID = ["mo", "mh", "ml", "mc"]
S5_COLS = ["t"] + BID + ASK + MID


WARMUP_DAYS = 30                # bars loaded before a test starts, so indicators have converged
OANDA_FIRST_USABLE = dict.fromkeys(PAIRS, "2005-01-01")   # before this OANDA only has ~26 flat quotes a month
NEWS_FIRST = pd.Timestamp("2008-01-01", tz="UTC")         # TradingView's calendar has no events before this


def pip_size(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


def tick_size(pair: str) -> float:
    return pip_size(pair) / 10


def _ticks_per_unit(pair: str) -> float:
    return 1e3 if pair.endswith("JPY") else 1e5


def write_prices(df: pd.DataFrame, path: Path, pair: str) -> None:
    """Parquet with prices as whole ticks in delta encoding, less than half the size of plain floats.
    Prices that don't fit whole ticks exactly stay floats; read_prices takes both."""
    scale = _ticks_per_unit(pair)
    out = df.copy()
    prices = [c for c in df.columns if c != "v"]
    vals = df[prices].to_numpy(float) * scale
    if len(df) and np.isfinite(vals).all():
        ints = np.round(vals)
        if np.array_equal(ints / scale, df[prices].to_numpy(float)):
            out[prices] = ints.astype(np.int32)
    enc = {c: "DELTA_BINARY_PACKED" for c in out.columns if out[c].dtype.kind == "i"}
    enc[out.index.name or "__index_level_0__"] = "DELTA_BINARY_PACKED"
    tmp = path.with_suffix(".tmp")
    out.to_parquet(tmp, compression="zstd", compression_level=9, use_dictionary=False, column_encoding=enc)
    os.replace(tmp, path)


def _index_unit(idx: pd.DatetimeIndex) -> int:
    """Divisor from the index's integer values to seconds, whatever resolution the file stored."""
    return {"ns": 10**9, "us": 10**6, "ms": 10**3, "s": 1}[idx.unit]


def read_prices(path: Path, pair: str, cols: list[str] | None = None) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=cols)
    ints = [c for c in df.columns if c != "v" and df[c].dtype.kind == "i"]
    if not ints:
        return df
    # one float block: the engine then reads the prices without another copy
    out = pd.DataFrame(df[ints].to_numpy(float) / _ticks_per_unit(pair), index=df.index, columns=ints)
    for c in df.columns.difference(ints):
        out[c] = df[c]
    return out[list(df.columns)] if len(out.columns) > len(ints) else out


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
    for side in ("m", "b", "a"):
        if f"{side}o" in df.columns:
            agg.update({f"{side}o": "first", f"{side}h": "max", f"{side}l": "min", f"{side}c": "last"})
    if "v" in df.columns:
        agg["v"] = "sum"
    out = df.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["bo"])


class S5Cache:
    """5-second OANDA candles: whole months from the bulk download (S5_YYYY-MM.parquet), anything
    else fetched lazily in 5-minute blocks and kept in sqlite."""

    MAX_MONTHS = 6     # trades run in time order, so one month per pair is enough

    def __init__(self, path: Path):
        self.path = path
        self.root = path.parent
        self.packed_path = path.parent.parent / "s5.parquet"
        self.lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._packed: dict[str, tuple[np.ndarray, np.ndarray]] | None = None
        self._have: dict[str, set[int]] = {}
        self._months: OrderedDict = OrderedDict()

    def _load_packed(self) -> None:
        if self._packed is not None:
            return
        with self.lock:
            if self._packed is not None:
                return
            self._packed = {}
            if not self.packed_path.exists():
                return
            raw = pd.read_parquet(self.packed_path)
            for idx_p, pair in enumerate(PAIRS):
                sub = raw[raw["pair"] == idx_p]
                if sub.empty:
                    continue
                ts = sub["t"].to_numpy(np.int64)
                u_bc = sub["bo"].to_numpy(np.int32)
                u_prev_bc = np.empty_like(u_bc)
                u_prev_bc[0] = u_bc[0]
                u_prev_bc[1:] = u_bc[:-1]
                u_bo = u_prev_bc + sub["bh"].to_numpy(np.int32)
                u_bh = np.maximum(u_bo, u_bc) + sub["bl"].to_numpy(np.int32)
                u_bl = np.minimum(u_bo, u_bc) - sub["bc"].to_numpy(np.int32)
                u_so = sub["ao"].to_numpy(np.int32)
                u_ao = u_bo + u_so
                u_ah = u_bh + u_so + sub["ah"].to_numpy(np.int32)
                u_al = u_bl + u_so + sub["al"].to_numpy(np.int32)
                u_ac = u_bc + u_so + sub["ac"].to_numpy(np.int32)
                u_mo = (u_bo + u_ao + sub["mo"].to_numpy(np.int32)) // 2
                u_mh = (u_bh + u_ah + sub["mh"].to_numpy(np.int32)) // 2
                u_ml = (u_bl + u_al + sub["ml"].to_numpy(np.int32)) // 2
                u_mc = (u_bc + u_ac + sub["mc"].to_numpy(np.int32)) // 2
                scale = _ticks_per_unit(pair)
                prices = np.column_stack([u_bo, u_bh, u_bl, u_bc, u_ao, u_ah, u_al, u_ac, u_mo, u_mh, u_ml, u_mc]) / scale
                self._packed[pair] = (ts, np.column_stack([ts.astype(float), prices]))
                self._have.setdefault(pair, set()).update(int(x) for x in np.unique(ts // 300 * 300))

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS s5 (pair TEXT, t INTEGER, bo REAL, bh REAL, bl REAL, bc REAL,"
                             " ao REAL, ah REAL, al REAL, ac REAL, mo REAL, mh REAL, ml REAL, mc REAL, PRIMARY KEY(pair, t))")
            self._db.execute("CREATE TABLE IF NOT EXISTS blocks (pair TEXT, b INTEGER, PRIMARY KEY(pair, b))")
            self._db.commit()
        return self._db

    def _known(self, pair: str) -> set[int]:
        self._load_packed()
        if pair not in self._have:
            if not self.path.exists():
                self._have[pair] = set()
            else:
                with self.lock:
                    rows = self.db.execute("SELECT b FROM blocks WHERE pair=?", (pair,)).fetchall()
                self._have[pair] = {r[0] for r in rows}
        return self._have[pair]

    def _month_file(self, pair: str, t: int) -> Path:
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        return self.root / pair / f"S5_{d.year:04d}-{d.month:02d}.parquet"

    def _month(self, pair: str, t: int) -> tuple | None:
        """The bulk-downloaded month containing t as (times, prices in file form, divisor), or None."""
        f = self._month_file(pair, t)
        with self.lock:
            if f in self._months:
                self._months.move_to_end(f)
                return self._months[f]
        if not f.exists():
            return None
        df = pd.read_parquet(f, columns=S5_COLS[1:])
        ints = all(k == "i" for k in df.dtypes.map(lambda x: x.kind))
        a = (df.index.asi8 // _index_unit(df.index), df.to_numpy(np.int32 if ints else float), _ticks_per_unit(pair) if ints else 1.0)
        with self.lock:
            self._months[f] = a
            while len(self._months) > self.MAX_MONTHS:
                self._months.popitem(last=False)
        return a

    def ensure(self, client: oanda_mod.OandaClient, items: list[tuple[str, int]], workers: int = 8):
        todo = sorted({(p, int(b // 300 * 300)) for p, b in items
                       if int(b // 300 * 300) not in self._known(p) and not self._month_file(p, b).exists()})
        if not todo or client is None:
            return

        def fetch(item):
            p, b = item
            df = client.s5_window(p, dt.datetime.fromtimestamp(b, dt.timezone.utc), 60)
            df = df[(df["t"] >= b) & (df["t"] < b + 300)] if len(df) else df
            return p, b, df

        with ThreadPoolExecutor(workers) as ex:
            for p, b, df in ex.map(fetch, todo):
                with self.lock:
                    self.db.executemany("INSERT OR REPLACE INTO s5 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        [(p, int(r.t), r.bo, r.bh, r.bl, r.bc, r.ao, r.ah, r.al, r.ac,
                                          r.mo, r.mh, r.ml, r.mc) for r in df.itertuples()])
                    self.db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?)", (p, b))
                self._known(p).add(b)
        with self.lock:
            self.db.commit()

    def get(self, pair: str, t0: int, t1: int) -> np.ndarray:
        """rows with t in [t0, t1): columns t,bo,bh,bl,bc,ao,ah,al,ac,mo,mh,ml,mc"""
        self._load_packed()
        if self._packed and pair in self._packed:
            ts, arr = self._packed[pair]
            i, j = int(np.searchsorted(ts, t0, side="left")), int(np.searchsorted(ts, t1, side="left"))
            if j > i:
                return arr[i:j]
        a = self._month(pair, t0)
        if a is not None and self._month_file(pair, t1 - 1) == self._month_file(pair, t0):
            i, j = np.searchsorted(a[0], t0), np.searchsorted(a[0], t1)
            return np.column_stack([a[0][i:j], a[1][i:j] / a[2]])
        if not self.path.exists():
            return np.empty((0, len(S5_COLS)), dtype=float)
        with self.lock:
            rows = self.db.execute(f"SELECT {','.join(S5_COLS)} FROM s5 WHERE pair=? AND t>=? AND t<? ORDER BY t",
                                   (pair, int(t0), int(t1))).fetchall()
        return np.array(rows, dtype=float).reshape(-1, len(S5_COLS))

    def has_block(self, pair: str, t: int) -> bool:
        return self._month_file(pair, t).exists() or int(t // 300 * 300) in self._known(pair)


class DataStore:
    def __init__(self, cache_dir: Path):
        self.cache = cache_dir
        self.s5 = S5Cache(self.cache / "oanda" / "s5_blocks.sqlite")
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
    def _oanda_month(self, client, pair: str, y: int, m: int, force: bool) -> None:
        """Make sure M1 / M5 / H1 files of one month are on disk. M5 and H1 are built from M1; on the whole
        overlap with OANDA's own M5 and H1 candles they are identical (see DATA_QUALITY.md)."""
        d = self.cache / "oanda" / pair
        d.mkdir(parents=True, exist_ok=True)
        name = f"{y:04d}-{m:02d}.parquet"
        f = d / f"M1_{name}"
        a = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        b = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=dt.timezone.utc)
        now = dt.datetime.now(dt.timezone.utc)
        if f.exists() and not force:
            written = f.stat().st_mtime
            # a month saved before it ended is still partial, however old the file is
            if written >= (b + dt.timedelta(hours=1)).timestamp() or time.time() - written < 15 * 60 or client is None:
                return
        if client is None:
            if f.exists():
                return
            raise RuntimeError(f"OANDA data for {pair} {y:04d}-{m:02d} missing and no OANDA_TOKEN configured (see Data connections).")
        df = client.candles(pair, "M1", a, min(b, now))
        for frame, target in ((resample(df, "5min"), d / f"M5_{name}"), (resample(df, "1h"), d / f"H1_{name}"),
                              (df, f)):   # M1 last: its presence means the month is complete on disk
            write_prices(frame, target, pair)

    def _oanda_read(self, pair: str, tf: str, months: list, cols: list[str] | None = None) -> pd.DataFrame:
        d = self.cache / "oanda" / pair
        parts = [read_prices(d / f"{tf}_{y:04d}-{m:02d}.parquet", pair, cols) for y, m in months]
        parts = [x for x in parts if len(x)]
        if not parts:
            return pd.DataFrame(columns=cols or BID + ASK + MID, index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
        out = pd.concat(parts)
        return out if out.index.is_monotonic_increasing else out.sort_index()

    def _oanda_packed_slice(self, packed_path: Path, pair: str, lo: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        raw = pd.read_parquet(packed_path)
        ts = raw.index.to_numpy(np.int64)
        u_bc_all = raw["bo"].to_numpy(np.int32)
        u_prev_bc = np.empty_like(u_bc_all)
        u_prev_bc[0] = u_bc_all[0]
        u_prev_bc[1:] = u_bc_all[:-1]
        u_bo_all = u_prev_bc + raw["bh"].to_numpy(np.int32)
        t0 = int(lo.floor("1h").timestamp())
        t1 = int(end.ceil("1h").timestamp())
        i0 = int(np.searchsorted(ts, t0, side="left"))
        i1 = int(np.searchsorted(ts, t1, side="right"))
        if i0 >= i1:
            return pd.DataFrame(columns=MID + BID + ASK, index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
        sub = raw.iloc[i0:i1]
        u_bc = u_bc_all[i0:i1]
        u_bo = u_bo_all[i0:i1]
        u_bh = np.maximum(u_bo, u_bc) + sub["bl"].to_numpy(np.int32)
        u_bl = np.minimum(u_bo, u_bc) - sub["bc"].to_numpy(np.int32)
        u_so = sub["ao"].to_numpy(np.int32)
        u_ao = u_bo + u_so
        u_ah = u_bh + u_so + sub["ah"].to_numpy(np.int32)
        u_al = u_bl + u_so + sub["al"].to_numpy(np.int32)
        u_ac = u_bc + u_so + sub["ac"].to_numpy(np.int32)
        u_mo = (u_bo + u_ao + sub["mo"].to_numpy(np.int32)) // 2
        u_mh = (u_bh + u_ah + sub["mh"].to_numpy(np.int32)) // 2
        u_ml = (u_bl + u_al + sub["ml"].to_numpy(np.int32)) // 2
        u_mc = (u_bc + u_ac + sub["mc"].to_numpy(np.int32)) // 2
        arr = np.column_stack([u_mo, u_mh, u_ml, u_mc, u_bo, u_bh, u_bl, u_bc, u_ao, u_ah, u_al, u_ac])
        idx = pd.to_datetime(ts[i0:i1], unit="s", utc=True)
        return pd.DataFrame(arr / _ticks_per_unit(pair), index=idx, columns=MID + BID + ASK)

    def _oanda_load(self, pair: str, start: pd.Timestamp, end: pd.Timestamp, force: bool, with_m1: bool = True) -> PairData:
        client = self.oanda_client()
        first = OANDA_FIRST_USABLE.get(pair)
        lo = max(start - pd.Timedelta(days=WARMUP_DAYS), pd.Timestamp(first, tz="UTC")) if first else start - pd.Timedelta(days=WARMUP_DAYS)
        sl = slice(lo, end)
        packed = self.cache / f"{pair}.parquet"
        if packed.exists() and (not force or client is None):
            df_m1 = self._oanda_packed_slice(packed, pair, lo, end)
            m5 = resample(df_m1, "5min").loc[sl] if len(df_m1) else df_m1
            h1 = resample(df_m1, "1h").loc[sl] if len(df_m1) else df_m1
            m1 = df_m1[BID + ASK].loc[sl] if with_m1 else None
        else:
            months = []
            y, m = lo.year, lo.month
            while (y, m) <= (end.year, end.month):
                months.append((y, m))
                m += 1
                if m == 13:
                    y, m = y + 1, 1
            with ThreadPoolExecutor(4) as ex:
                # OANDA's past candles don't change: "re-download" only needs to redo the latest months
                list(ex.map(lambda ym: self._oanda_month(client, pair, ym[0], ym[1], force and ym in months[-2:]), months))
            m5 = self._oanda_read(pair, "M5", months, MID + BID + ASK).loc[sl]
            h1 = self._oanda_read(pair, "H1", months, MID + BID + ASK).loc[sl]
            m1 = self._oanda_read(pair, "M1", months, BID + ASK).loc[sl] if with_m1 else None
        notes = []
        if len(m5) and m5.index[0] > start:
            notes.append(f"{pair}: OANDA data used here starts {m5.index[0]:%Y-%m-%d}.")
        elif first and start - pd.Timedelta(days=WARMUP_DAYS) < pd.Timestamp(first, tz="UTC"):
            notes.append(f"{pair}: under {WARMUP_DAYS} days of indicator warm-up before {start:%Y-%m-%d}.")
        return PairData(pair, "oanda", m5, h1, m1, True, notes)

    # ------------------------------------------------------------- forexsb.com (Dukascopy bars)
    def _fsb_load(self, pair: str, start: pd.Timestamp, end: pd.Timestamp, force: bool) -> PairData:
        d = self.cache / "fsb"
        if force or not (d / f"{pair}_5.parquet").exists():
            fsb_mod.download([pair], [1, 5, 30], d / "raw")
            fsb_mod.build_cache(d / "raw", d, [pair])
        m5 = pd.read_parquet(d / f"{pair}_5.parquet")
        h1 = pd.read_parquet(d / f"{pair}_60.parquet")
        m1 = pd.read_parquet(d / f"{pair}_1.parquet") if (d / f"{pair}_1.parquet").exists() else None
        notes = []
        if len(m5) and m5.index[0] > start:
            notes.append(f"{pair}: Dukascopy 5m history (forexsb) starts {m5.index[0]:%Y-%m-%d %H:%M} UTC "
                         f"(requested {start:%Y-%m-%d}). Earlier dates are NOT tested.")
        if m1 is not None and len(m1) and m1.index[0] > start:
            notes.append(f"{pair}: 1m bars only from {m1.index[0]:%Y-%m-%d}; before that intrabar order is "
                         f"resolved on 5m bars (pessimistic).")
        notes.append(f"{pair}: Dukascopy feed, not OANDA — prices differ slightly from your TradingView chart.")
        sl = slice(start - pd.Timedelta(days=12), end)
        cols = ["bo", "bh", "bl", "bc", "v"]
        return PairData(pair, "forexsb", m5.loc[sl, cols], h1.loc[start - pd.Timedelta(days=60):end, cols],
                        m1.loc[sl, cols] if m1 is not None and len(m1) else None, False, notes)

    # ------------------------------------------------------------- public
    def get_many(self, pairs: list[str], source: str, start: pd.Timestamp, end: pd.Timestamp,
                 force: bool = False, keep: bool = True) -> dict[str, PairData]:
        with ThreadPoolExecutor(3) as ex:     # more buffers overlap in memory with little time gained
            res = list(ex.map(lambda p: self.get(p, source, start, end, force, keep), pairs))
        return dict(zip(pairs, res))

    def get(self, pair: str, source: str, start: pd.Timestamp, end: pd.Timestamp, force: bool = False,
            keep: bool = True, with_m1: bool = True) -> PairData:
        """keep=False: don't hold it in memory after this use. with_m1=False: charts don't need 1m bars."""
        key = (pair, source, start, end, with_m1)
        with self.lock:
            if not force and key in self._mem and time.time() - self._mem[key][0] < 10 * 60:
                return self._mem[key][1]
        if source == "oanda":
            pdata = self._oanda_load(pair, start, end, force, with_m1)
        elif source == "forexsb":
            pdata = self._fsb_load(pair, start, end, force)
        else:
            pdata = self._tv_load(pair, start, end, force)
        if keep:
            with self.lock:
                self._mem[key] = (time.time(), pdata)
                while len(self._mem) > 14:
                    self._mem.pop(next(iter(self._mem)))
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
            for tf in ("1", "5", "60"):
                f = self.cache / "fsb" / f"{pair}_{tf}.parquet"
                if f.exists():
                    idx = pd.read_parquet(f, columns=["bc"]).index
                    info[f"fsb_{tf}"] = [str(idx[0]), str(idx[-1]), len(idx)]
            packed = self.cache / f"{pair}.parquet"
            d = self.cache / "oanda" / pair
            if packed.exists():
                idx = pd.read_parquet(packed, columns=[]).index
                if len(idx):
                    d0 = dt.datetime.fromtimestamp(int(idx[0]), dt.timezone.utc)
                    d1 = dt.datetime.fromtimestamp(int(idx[-1]), dt.timezone.utc)
                    n_months = (d1.year - d0.year) * 12 + (d1.month - d0.month) + 1
                    info["oanda_months"] = [f"{d0.year:04d}-{d0.month:02d}", f"{d1.year:04d}-{d1.month:02d}", n_months]
            elif d.exists():
                files = sorted(d.glob("M1_*.parquet"))
                if files:
                    info["oanda_months"] = [files[0].stem[3:], files[-1].stem[3:], len(files)]
            out[pair] = info
        return out
