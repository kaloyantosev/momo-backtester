"""Economic calendar from TradingView (economic-calendar.tradingview.com).

importance: -1 = low, 0 = medium (2 stars), 1 = high (3 stars).
Past months are cached on disk permanently; the current/future months are re-fetched after 6h.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import httpx
import pandas as pd

from .tradingview import HTTP_HEADERS

URL = "https://economic-calendar.tradingview.com/events"
ALL_COUNTRIES = ["US", "EU", "CH", "AU", "JP", "GB", "CA", "NZ", "DE", "FR", "IT", "ES", "CN"]
COUNTRY_CCY = {"US": "USD", "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "CH": "CHF",
               "AU": "AUD", "JP": "JPY", "GB": "GBP", "CA": "CAD", "NZ": "NZD", "CN": "CNY"}


def _month_iter(start: dt.date, end: dt.date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def _fetch_month(y: int, m: int) -> list[dict]:
    a = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    b = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=dt.timezone.utc)
    out = []
    # two halves keep responses small
    mid = a + (b - a) / 2
    for s, e in ((a, mid), (mid, b)):
        params = {"from": s.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "to": e.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                  "countries": ",".join(ALL_COUNTRIES)}
        r = httpx.get(URL, params=params, headers=HTTP_HEADERS, timeout=60)
        r.raise_for_status()
        for ev in r.json().get("result", []):
            out.append({"id": ev.get("id"), "date": ev.get("date"), "country": ev.get("country"),
                        "currency": ev.get("currency"), "importance": ev.get("importance"),
                        "title": ev.get("title"), "category": ev.get("category")})
    # de-dup (event on the half boundary)
    seen, uniq = set(), []
    for ev in out:
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        uniq.append(ev)
    return uniq


def load_events(cache_dir: Path, start: dt.date, end: dt.date) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    rows = []
    for y, m in _month_iter(start, end):
        f = cache_dir / f"{y:04d}-{m:02d}.json"
        month_end = dt.date(y + (m == 12), m % 12 + 1, 1)
        fresh_needed = month_end > today - dt.timedelta(days=3)  # month not fully in the past
        if f.exists() and (not fresh_needed or time.time() - f.stat().st_mtime < 6 * 3600):
            evs = json.loads(f.read_text())
        else:
            evs = _fetch_month(y, m)
            f.write_text(json.dumps(evs))
        rows.extend(evs)
    df = pd.DataFrame(rows, columns=["id", "date", "country", "currency", "importance", "title", "category"])
    if df.empty:
        df["t"] = pd.Series(dtype="datetime64[ns, UTC]")
        return df
    df["t"] = pd.to_datetime(df["date"], utc=True)
    df = df.drop_duplicates("id").sort_values("t").reset_index(drop=True)
    return df
