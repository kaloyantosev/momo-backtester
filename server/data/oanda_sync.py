"""Bulk download of the OANDA history. Safe to stop and re-run: finished months are skipped.

    ../../.venv/bin/python -m server.data.oanda_sync m1          # M1, plus M5 and H1 built from it
    ../../.venv/bin/python -m server.data.oanda_sync s5          # S5, newest month first
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import oanda as oanda_mod
from .store import PAIRS, DataStore, write_prices

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data_cache"
ENV_FILE = ROOT / ".env"
FIRST_MONTH = (2002, 5)    # first candle OANDA returns for all six pairs

_clients: dict[str, oanda_mod.OandaClient] = {}
_lock = threading.Lock()


def client() -> oanda_mod.OandaClient:
    """Re-reads .env, so replacing the token in the dashboard doesn't stop a running download."""
    env = dict(line.strip().split("=", 1) for line in ENV_FILE.read_text().splitlines() if "=" in line)
    tok = env.get("OANDA_TOKEN", "")
    with _lock:
        if tok not in _clients:
            _clients[tok] = oanda_mod.OandaClient(tok, env.get("OANDA_ENV", "practice"))
        return _clients[tok]


def months(first: tuple[int, int], complete_only: bool) -> list[tuple[int, int]]:
    now = dt.datetime.now(dt.timezone.utc)
    last = (now.year, now.month)
    out = []
    y, m = first
    while (y, m) <= last:
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out[:-1] if complete_only else out


def _s5_month(pair: str, y: int, m: int) -> None:
    f = CACHE / "oanda" / pair / f"S5_{y:04d}-{m:02d}.parquet"
    if f.exists():
        return
    a = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    b = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=dt.timezone.utc)
    df = client().candles(pair, "S5", a, b).drop(columns="v")
    write_prices(df, f, pair)


def run(kind: str, first: tuple[int, int], workers: int) -> int:
    store = DataStore(CACHE)
    if kind == "m1":
        jobs = [(p, y, m) for p in PAIRS for y, m in months(first, False)]
        work = lambda j: store._oanda_month(client(), j[0], j[1], j[2], False)  # noqa: E731
    else:
        jobs = [(p, y, m) for y, m in reversed(months(first, True)) for p in PAIRS]
        work = lambda j: _s5_month(*j)  # noqa: E731
    status_file = CACHE / "oanda" / f"sync_{kind}.json"
    done, failed, t0 = [0], [], time.time()

    def one(j):
        for attempt in range(5):
            try:
                work(j)
                break
            except Exception as e:
                if attempt == 4:
                    failed.append([*j, str(e)[:200]])
                    print(f"FAILED {j}: {e}", flush=True)
                time.sleep(30 * (attempt + 1))
        with _lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(jobs):
                rate = done[0] / max(time.time() - t0, 1)
                status_file.write_text(json.dumps({"done": done[0], "total": len(jobs), "failed": failed,
                                                   "last": list(j), "eta_min": round((len(jobs) - done[0]) / rate / 60)}))
                print(f"{kind} {done[0]}/{len(jobs)} last {j} {len(failed)} failed, ~{(len(jobs) - done[0]) / rate / 60:.0f} min left", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, jobs))
    print(f"{kind} finished in {(time.time() - t0) / 60:.1f} min, {len(failed)} failed", flush=True)
    return len(failed)


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "m1"
    first = tuple(int(x) for x in sys.argv[2].split("-")) if len(sys.argv) > 2 else FIRST_MONTH
    sys.exit(1 if run(kind, first, int(sys.argv[3]) if len(sys.argv) > 3 else 12) else 0)
