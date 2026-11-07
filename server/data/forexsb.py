"""Historical bars from forexsb.com's free data app (compiled from Dukascopy ticks).

The data app (https://data.forexsb.com/data-app) downloads
    https://data.forexsb.com/datafeed/data/dukascopy/<SYMBOL><PERIOD>.lb.gz
PERIOD is 1, 5, 15 or 30 (minutes); longer timeframes are built from 30m bars, as the app does.
Each file holds the latest <= 200,000 bars, as little-endian int32 records of 28 bytes:
    minutes since 2000-01-01 UTC, open, high, low, close (x priceScale), volume, spread (points)
(older files may use 24-byte records without the spread). Prices are Dukascopy bid.
"""
from __future__ import annotations

import gzip
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

URL = "https://data.forexsb.com/datafeed/data/dukascopy/{symbol}{period}.lb.gz"
HEADERS = {"User-Agent": "Mozilla/5.0", "X-Client": "data-app"}
EPOCH_2000 = 946684800  # 2000-01-01T00:00:00Z


def price_scale(pair: str) -> int:
    return 1000 if pair.endswith("JPY") else 100000


def decode(raw: bytes, pair: str) -> pd.DataFrame:
    """Decode one .lb file (gzip or already inflated) into a UTC-indexed OHLC frame."""
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    rec = 28 if len(raw) % 28 == 0 and _monotonic(raw, 28) else 24
    if len(raw) % rec:
        raise ValueError(f"{pair}: file size {len(raw)} is not a multiple of {rec}-byte records")
    a = np.frombuffer(raw, dtype="<i4").reshape(-1, rec // 4)
    sc = price_scale(pair)
    df = pd.DataFrame({"bo": a[:, 1] / sc, "bh": a[:, 2] / sc, "bl": a[:, 3] / sc, "bc": a[:, 4] / sc,
                       "v": a[:, 5].astype(float)},
                      index=pd.to_datetime(a[:, 0].astype("int64") * 60 + EPOCH_2000, unit="s", utc=True))
    df["spread_pts"] = a[:, 6] if rec == 28 else np.nan
    df.index.name = "t"
    return df[~df.index.duplicated(keep="last")].sort_index()


def _monotonic(raw: bytes, rec: int) -> bool:
    t = np.frombuffer(raw, dtype="<i4")[:: rec // 4]
    return bool(len(t) == 0 or (np.diff(t) >= 0).all())


def download(pairs: list[str], periods: list[int], raw_dir: Path, log=print) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=HEADERS, timeout=120) as c:
        for p in pairs:
            for per in periods:
                r = c.get(URL.format(symbol=p, period=per))
                r.raise_for_status()
                (raw_dir / f"{p}{per}.lb.gz").write_bytes(r.content)
                log(f"{p} {per}m: {len(r.content) / 1e6:.1f} MB")
                time.sleep(1)


def build_cache(raw_dir: Path, out_dir: Path, pairs: list[str]) -> dict:
    """raw .lb.gz -> parquet: <pair>_1 / _5 (as downloaded) and _60 (built from 30m bars)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {}
    for p in pairs:
        rep = {}
        for per in (1, 5, 30):
            f = raw_dir / f"{p}{per}.lb.gz"
            if not f.exists():
                continue
            df = decode(f.read_bytes(), p)
            if per == 30:
                h1 = df.resample("1h", label="left", closed="left").agg(
                    {"bo": "first", "bh": "max", "bl": "min", "bc": "last", "v": "sum", "spread_pts": "min"}
                ).dropna(subset=["bo"])
                h1.to_parquet(out_dir / f"{p}_60.parquet")
                rep["60"] = [str(h1.index[0]), str(h1.index[-1]), len(h1)]
            else:
                df.to_parquet(out_dir / f"{p}_{per}.parquet")
                rep[str(per)] = [str(df.index[0]), str(df.index[-1]), len(df)]
        report[p] = rep
    return report
