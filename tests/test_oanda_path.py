"""OANDA code path (no network): pagination, 5-second snapshot at +30s, S5 exit resolution."""
import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.data.oanda import OandaClient  # noqa: E402
from server.data.store import PairData, resample  # noqa: E402
from server.engine.strategy import PriceAccess, merge_params, prepare, simulate_exit  # noqa: E402


def test_candles_pagination_no_gaps_no_dupes():
    t_start, t_end = 1_700_000_040, 1_700_000_040 + 60 * 12_000  # 12k minutes

    def handler(req: httpx.Request):
        q = req.url.params
        frm, cnt = float(q["from"]), int(q["count"])
        first = frm if q["includeFirst"] == "true" else frm + 60
        cs = []
        t = first
        while len(cs) < cnt and t < t_end + 600:
            px = {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"}
            cs.append({"time": f"{t:.9f}", "volume": 3, "complete": True, "bid": px, "ask": px})
            t += 60
        return httpx.Response(200, json={"candles": cs})

    c = OandaClient("x")
    c._local.client = httpx.Client(transport=httpx.MockTransport(handler))
    df = c.candles("EURUSD", "M1", pd.Timestamp(t_start, unit="s", tz="UTC").to_pydatetime(),
                   pd.Timestamp(t_end, unit="s", tz="UTC").to_pydatetime())
    assert len(df) == 12_000 and df.index.is_unique and df.index.is_monotonic_increasing
    assert int(df.index[0].timestamp()) == t_start and int(df.index[-1].timestamp()) == t_end - 60


class FakeS5:
    def __init__(self, rows):
        self.rows = np.array(rows, dtype=float)

    def has_block(self, pair, t):
        return True

    def ensure(self, *a, **k):
        pass

    def get(self, pair, t0, t1):
        r = self.rows
        return r[(r[:, 0] >= t0) & (r[:, 0] < t1)]


def make_oanda_pp(n=30, px=1.1000, spread=0.0001):
    idx = pd.date_range("2026-01-05 08:00", periods=n * 5, freq="1min", tz="UTC")
    m1 = pd.DataFrame({"bo": px, "bh": px + 0.0001, "bl": px - 0.0001, "bc": px,
                       "ao": px + spread, "ah": px + 0.0001 + spread, "al": px - 0.0001 + spread, "ac": px + spread,
                       "v": 1}, index=idx)
    m5 = resample(m1, "5min")
    h1 = resample(m1, "1h")
    P = merge_params({"source": "oanda"})
    return prepare(PairData("EURUSD", "oanda", m5, h1, m1, True), P), P, m1


def s5row(t, b, spread=0.0001):
    o, h, l, c = b
    return [t, o, h, l, c, o + spread, h + spread, l + spread, c + spread]


def test_snapshot_at_30_seconds_decision_vs_fill():
    pp, P, _ = make_oanda_pp()
    k = 10
    t0 = int(pp.t5[k])
    rows = [s5row(t0 + 5 * i, (1.1000, 1.1002 + (0.0003 if i == 3 else 0), 1.0998, 1.1001)) for i in range(6)]
    rows.append(s5row(t0 + 30, (1.10075, 1.1009, 1.1007, 1.1008)))
    pa = PriceAccess(pp, P, FakeS5(rows), object())
    s = pa.snapshot(k, 30)
    assert s["tier"] == "S5"
    # fill = first tick at/after +30s; decision = last price before +30s (no look-ahead)
    assert abs(s["bid"] - 1.10075) < 1e-12 and abs(s["ask"] - 1.10085) < 1e-12
    assert abs(s["px"] - 1.1001) < 1e-12
    assert abs(s["ph"] - 1.1005) < 1e-12          # partial high of the 5s candles before +30s
    assert abs(s["pl"] - 1.0998) < 1e-12


def test_s5_resolves_exit_order_inside_entry_bar():
    pp, P, _ = make_oanda_pp()
    k = 10
    t0 = int(pp.t5[k])
    T = t0 + 30
    rows = [s5row(t0 + 5 * i, (1.1000, 1.1001, 1.0999, 1.1000)) for i in range(6)]
    # after entry: price spikes up to TP at +45s, later dumps through SL
    rows.append(s5row(t0 + 40, (1.1000, 1.1005, 1.0999, 1.1004)))
    rows.append(s5row(t0 + 45, (1.1004, 1.1017, 1.1003, 1.1016)))
    rows.append(s5row(t0 + 50, (1.1010, 1.1011, 1.0980, 1.0985)))
    pa = PriceAccess(pp, P, FakeS5(rows), object())
    t, px, why, *_ = simulate_exit(pa, 1, k, T, 1.1001, 1.0991, 1.1016, None, "", "pessimistic")
    assert why == "tp" and t == t0 + 45 and px == 1.1016


def test_s5_breaks_a_1m_tie():
    pp, P, m1 = make_oanda_pp()
    k = 10
    t0 = int(pp.t5[k])
    T = t0 + 30
    # 1m bar 3 minutes after the entry bar touches both levels
    j = int(np.searchsorted(pp.t1, t0 + 300 + 180))
    pp.bid1[j, 1], pp.bid1[j, 2] = 1.1030, 1.0970
    tie_t = int(pp.t1[j])
    entry_rows = [s5row(t0 + 5 * i, (1.1000, 1.1001, 1.0999, 1.1000)) for i in range(60)]
    tie_rows = [s5row(tie_t, (1.1000, 1.1001, 1.0999, 1.1000)), s5row(tie_t + 5, (1.1000, 1.1020, 1.1000, 1.1018)),
                s5row(tie_t + 10, (1.1018, 1.1018, 1.0970, 1.0975))]
    pa = PriceAccess(pp, P, FakeS5(entry_rows + tie_rows), object())
    t, px, why, *_ = simulate_exit(pa, 1, k, T, 1.1001, 1.0991, 1.1016, None, "", "pessimistic")
    assert why == "tp" and t == tie_t + 5
