"""OANDA code path (no network): pagination, 5-second snapshot at +30s, S5 exit resolution."""
import os
import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.data.oanda import OandaClient  # noqa: E402
from server.data.store import DataStore, PairData, resample  # noqa: E402
from server.engine.backtest import _day_close  # noqa: E402
from server.engine.strategy import PriceAccess, _sig_cols, merge_params, prepare, simulate_exit  # noqa: E402


def test_candles_pagination_no_gaps_no_dupes():
    t_start, t_end = 1_700_000_040, 1_700_000_040 + 60 * 12_000  # 12k minutes

    def handler(req: httpx.Request):
        q = req.url.params
        frm, cnt = float(q["from"]), int(q["count"])
        first = frm if q["includeFirst"] == "true" else frm + 60
        if q["includeFirst"] != "true":  # the live API counts the skipped candle: count - 1 come back
            cnt -= 1
        cs = []
        t = first
        while len(cs) < cnt and t < t_end + 600:
            px = {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"}
            cs.append({"time": f"{t:.9f}", "volume": 3, "complete": True, "mid": px, "bid": px, "ask": px})
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
    assert abs(PriceAccess(pp, P, FakeS5(rows), object()).snapshot(k, 30)["px"] - 1.10015) < 1e-12  # default: mid
    P["price_basis"] = "bid"
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


def test_s5_gives_the_real_fill_and_second_inside_a_1m_bar():
    pp, P, m1 = make_oanda_pp()
    k = 10
    t0 = int(pp.t5[k])
    T = t0 + 30
    j = int(np.searchsorted(pp.t1, t0 + 300 + 180))
    pp.bid1[j, 2] = 1.0970                     # the 1m bar only says the stop was touched
    tie_t = int(pp.t1[j])
    entry_rows = [s5row(t0 + 5 * i, (1.1000, 1.1001, 1.0999, 1.1000)) for i in range(60)]
    # price jumps over the stop between two 5-second candles
    gap_rows = [s5row(tie_t, (1.1000, 1.1001, 1.0999, 1.1000)), s5row(tie_t + 25, (1.0985, 1.0986, 1.0970, 1.0975))]
    pa = PriceAccess(pp, P, FakeS5(entry_rows + gap_rows), object())
    t, px, why, *_ = simulate_exit(pa, 1, k, T, 1.1001, 1.0991, 1.1016, None, "", "pessimistic")
    assert why == "sl" and t == tie_t + 25 and px == 1.0985


def test_s5_quiet_minute_with_an_event_keeps_walking():
    pp, P, m1 = make_oanda_pp()
    k = 10
    t0 = int(pp.t5[k])
    T = t0 + 30
    j = int(np.searchsorted(pp.t1, t0 + 300 + 180))
    pp.bid1[j, 1] = 1.1008                     # high reaches break-even, stop not touched
    be_t = int(pp.t1[j])
    pp.bid1[j + 1, 2] = 1.0999                 # next minute comes back to the moved stop
    rows = [s5row(t0 + 5 * i, (1.1000, 1.1001, 1.0999, 1.1000)) for i in range(60)]
    rows += [s5row(be_t, (1.1000, 1.1008, 1.1000, 1.1005)), s5row(be_t + 5, (1.1005, 1.1005, 1.1002, 1.1003))]
    rows += [s5row(be_t + 60, (1.1003, 1.1003, 1.1000, 1.1000))]
    pa = PriceAccess(pp, P, FakeS5(rows), object())
    t, px, why, *_ = simulate_exit(pa, 1, k, T, 1.1001, 1.0991, 1.1016, None, "", "pessimistic",
                                   be={"pct": 40, "off": 0.0})
    assert why == "be" and t == be_t + 60 and px == 1.1001


def test_mid_fills_ignore_the_spread_and_add_the_fixed_one():
    pp, P, _ = make_oanda_pp()
    P = {**P, "fill_basis": "mid"}
    pp.spread = 0.0
    k = 10
    t0 = int(pp.t5[k])
    T = t0 + 30
    # columns: t, bid OHLC, ask OHLC, mid OHLC. Bid dips through the stop, mid doesn't, until the second candle.
    def row(t, mid_lo):
        m = [1.1000, 1.1001, mid_lo, 1.1000]
        return [t, *[v - 0.0005 for v in m], *[v + 0.0005 for v in m], *m]
    rows = [row(t0 + 5 * i, 1.0999) for i in range(60)] + [row(t0 + 300, 1.0990)]
    pp.bid1[int(np.searchsorted(pp.t1, t0 + 300)), 2] = 1.0985
    pa = PriceAccess(pp, P, FakeS5(rows), object())
    assert pa.snapshot(k, 30)["ask"] == 1.1000
    t, px, why, *_ = simulate_exit(pa, 1, k, T, 1.1000, 1.0996, 1.1006, None, "", "pessimistic")
    assert why == "sl" and t == t0 + 300 and px == 1.0996
    pa_real = PriceAccess(pp, {**P, "fill_basis": "bid_ask"}, FakeS5(rows), object())
    t2, *_ = simulate_exit(pa_real, 1, k, T, 1.1000, 1.0996, 1.1006, None, "", "pessimistic")
    assert t2 < t0 + 300


def test_all_trades_close_at_22_bulgarian_time_in_winter_and_summer():
    P = merge_params({})
    def utc(s):
        return int(pd.Timestamp(s, tz="UTC").timestamp())
    assert _day_close(np.datetime64("2026-01-14"), utc("2026-01-14 12:00"), P) == utc("2026-01-14 20:00")
    assert _day_close(np.datetime64("2026-07-14"), utc("2026-07-14 12:00"), P) == utc("2026-07-14 19:00")
    assert _day_close(np.datetime64("2026-07-14"), utc("2026-07-14 19:30"), P) == utc("2026-07-15 19:00")


def test_signals_use_oandas_own_mid_candle():
    # the high of (bid + ask) / 2 is not OANDA's mid high: bid and ask don't peak at the same moment
    idx = pd.date_range("2026-01-05 09:00", periods=3, freq="5min", tz="UTC")
    df = pd.DataFrame({"mo": [1.1] * 3, "mh": [1.3] * 3, "ml": [1.0] * 3, "mc": [1.2] * 3,
                       "bo": [1.09] * 3, "bh": [1.29] * 3, "bl": [0.99] * 3, "bc": [1.19] * 3,
                       "ao": [1.11] * 3, "ah": [1.5] * 3, "al": [1.01] * 3, "ac": [1.21] * 3}, index=idx)
    assert _sig_cols(df, "mid", True)[0, 1] == 1.3
    assert abs(_sig_cols(df.drop(columns=["mo", "mh", "ml", "mc"]), "mid", True)[0, 1] - 1.395) < 1e-12
    assert _sig_cols(df, "bid", True)[0, 1] == 1.29


def test_month_saved_before_it_ended_is_downloaded_again(tmp_path):
    calls = []

    class Client:
        def candles(self, pair, g, a, b):
            calls.append(a)
            i = pd.date_range(a, periods=10, freq="1min")
            cols = ["mo", "mh", "ml", "mc", "bo", "bh", "bl", "bc", "ao", "ah", "al", "ac"]
            return pd.DataFrame({**{c: 1.0 for c in cols}, "v": 1}, index=i)

    store = DataStore(tmp_path)
    store._oanda_month(Client(), "EURUSD", 2024, 3, False)
    f = tmp_path / "oanda" / "EURUSD" / "M1_2024-03.parquet"
    assert (tmp_path / "oanda" / "EURUSD" / "M5_2024-03.parquet").exists() and f.exists() and len(calls) == 1
    store._oanda_month(Client(), "EURUSD", 2024, 3, False)     # written after the month ended: final
    assert len(calls) == 1
    t = pd.Timestamp("2024-03-20", tz="UTC").timestamp()
    os.utime(f, (t, t))                                        # written while the month was still running
    store._oanda_month(Client(), "EURUSD", 2024, 3, False)
    assert len(calls) == 2


def test_compact_price_files_read_back_exactly(tmp_path):
    from server.data.store import read_prices, write_prices
    idx = pd.date_range("2026-03-02", periods=4, freq="5s", tz="UTC")
    for pair, base in (("EURUSD", 1.16171), ("USDJPY", 159.877)):
        df = pd.DataFrame({"bo": base + np.arange(4) * (0.00001 if pair == "EURUSD" else 0.001), "ao": base}, index=idx).round(5)
        f = tmp_path / f"{pair}.parquet"
        write_prices(df, f, pair)
        assert pd.read_parquet(f)["bo"].dtype.kind == "i"
        assert read_prices(f, pair).equals(df)
    # values that aren't whole ticks stay floats instead of being rounded
    df = pd.DataFrame({"bo": [1.161715, 1.1617], "v": [3, 4]}, index=idx[:2])
    write_prices(df, tmp_path / "x.parquet", "EURUSD")
    assert read_prices(tmp_path / "x.parquet", "EURUSD").equals(df)
