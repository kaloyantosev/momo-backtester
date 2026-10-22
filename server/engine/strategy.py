"""EMA + Supertrend + MACD-hook + ADX-session strategy: setup detection and trade simulation.

Bar convention: arrays are indexed by 5m bar; t5[i] is the bar OPEN time (UTC epoch seconds).
All signal logic is evaluated on bar close, exactly like the Pine indicators on historical bars.
The entry happens at open(C+1) + entry_delay_sec, using sub-minute data when available.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data.store import PairData, pip_size, tick_size
from . import indicators as ind

DEFAULTS: dict = {
    # ---- general
    "pairs": ["EURJPY", "EURUSD", "AUDUSD", "GBPUSD", "USDJPY", "USDCHF"],
    "source": "tradingview",          # tradingview | oanda
    "price_basis": "bid",             # bid | mid  (signal price; TradingView plots bid)
    "initial_balance": 10000.0,
    "lots": 0.2,
    "max_open_total": 6,              # one position per pair is always enforced
    # ---- costs
    "spread_pips": {"EURJPY": 1.8, "EURUSD": 1.0, "AUDUSD": 1.2, "GBPUSD": 1.4, "USDJPY": 1.2, "USDCHF": 1.6},
    "commission_per_lot": 0.0,        # USD per 1.0 lot round turn
    # ---- sessions / ADX (hours in `tz`)
    "tz": "Europe/Sofia",             # Bulgarian time (always London + 2h; both switch DST on the same dates)
    "use_adx_filter": True,
    "s1_check_hour": 11, "s1_end_hour": 15,   # 11:00 BGN = 09:00 London
    "s2_check_hour": 15, "s2_end_hour": 21,   # 15:00 BGN = 13:00 London; 21:00 BGN = 19:00 London
    "adx_len": 10, "adx_min": 25.0, "adx_max": 40.0,
    "adx_bar": "completed",           # completed (no look-ahead) | check_bar (what 1H-chart labels show; look-ahead)
    # ---- news
    "use_news": True,
    "news_countries": ["US", "EU", "CH", "AU", "JP"],
    "news_min_importance": 0,         # -1 low, 0 medium (2 stars), 1 high (3 stars)
    "news_before_min": 10, "news_after_min": 10,
    "news_scope": "pair",             # pair (only news of the pair's currencies) | all
    "news_block_setup_bars": False,   # also reject if any setup bar overlaps a news window
    "close_before_news_min": 10,      # 0 = off
    # ---- EMA + Supertrend
    "ema_len": 20,
    "st_atr": 3, "st_factor": 1.5,
    "bos_buffer_pips": 0.8,
    "bos_max_range_pips": 12.0,
    "bos_mode": "cross_candle",       # cross_candle: only the candle whose close crosses the EMA, body >= buffer | indicator
    "bos_after_st_max": 1,            # BOS may come 0..N bars AFTER the ST flip
    "bos_before_st_max": 0,           # BOS may come 0..N bars BEFORE the ST flip (indicator 1 uses 2)
    # ---- MACD
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "hook_min": 2, "hook_max": 5,
    "zero_mode": "against",           # against: 0-bars near a flip never count as the new colour | literal: opposite of painted colour | raw | zero_is_red
    "zero_eps_ticks": 0.5,            # |hist| below this many ticks displays as 0 on TradingView
    "hook_confirm": True,             # bar after the hook bar must continue (checked at +delay)
    "hook_confirm_mode": "snapshot",  # snapshot (at +delay sec) | close
    "macd_window_bars": 3,            # EMA/ST must complete within the hook bar + (N-1)
    "macd_after_span_max": 2,         # hook may come at most N bars after the EMA/ST span
    "require_monotonic": True,        # every bar from the hook to the signal bar must be stronger than the one before
    "entry_still_check": True,        # at entry (+delay) the histogram must still be rising/falling
    # ---- entry / exits
    "entry_delay_sec": 30,
    "sl_mode": "avg_close",           # avg_close: avg(ST signal candle, ST entry candle as closed) | avg_live | signal_only
    "rr": 1.6,
    "min_sl_pips": 0.0, "max_sl_pips": 0.0,
    "close_at_session_end": False,
    "close_before_weekend": False, "weekend_close_hour": 23,  # Friday, in tz
    "intrabar_tie": "pessimistic",    # when SL & TP hit inside one bar without finer data
    "subminute_model": "interp",      # without S5 data: interp | open | close of the 1m bar
    "decision_price": "known",        # price used for the live Supertrend at +delay without S5 data (internal)
    # +30s MACD checks without 5-second data. The forming-bar histogram is monotonic in price, so the
    # check reduces to "was the price beyond a critical level at +30s". If the whole known price range
    # around +30s (the 1m bar, else the 5m bar) is on one side, the answer is certain.
    #   range_accept: certain -> apply, ambiguous -> accept (no look-ahead bias)
    #   range_reject: ambiguous -> reject (strict, but biased: uses later prices)
    #   bar_close   : use the bar's final histogram (look-ahead)
    #   open        : use the bar's open price only
    "momentum_no5s": "range_accept",
    "big_candle_pips": 9.0,           # signal candle AND the one before >= N pips (high-low) -> SL on entry-candle ST; 0 = off
    "be_enabled": False,              # break-even: move SL to entry once price covered be_trigger_pct % of the way to TP
    "be_trigger_pct": 50.0,
    "be_offset_pips": 0.0,
}

REASONS = {
    "session": "Outside an ADX-confirmed session window",
    "news": "Entry inside news blackout",
    "news_setup": "Setup bar inside news window",
    "gap": "No regular next bar (data gap / weekend)",
    "side": "Histogram crossed back before entry",
    "monotonic": "Momentum faded between hook and signal bar (a light bar)",
    "st_state": "Supertrend flipped back before entry",
    "hook_confirm": "Bar after hook did not continue (30s check)",
    "still": "Entry-bar histogram not above previous bar and hook bar (30s check)",
    "st_live": "Supertrend flipped against at entry",
    "sl_invalid": "SL on wrong side of entry",
    "sl_min": "SL distance below minimum",
    "sl_max": "SL distance above maximum",
    "busy": "Pair already in a position",
    "max_open": "Max open positions reached",
    "used": "Setup events already used by a trade",
    "no_data": "No data to simulate",
}


def merge_params(p: dict | None) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for k, v in (p or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k].update(v)
        elif k in out:
            out[k] = v
    return out


# --------------------------------------------------------------------------------------------- prep
@dataclass
class Prepared:
    pair: str
    pip: float
    tick: float
    has_ask: bool
    spread: float                      # fixed spread in price units (TradingView data)
    t5: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    bid5: np.ndarray                   # (n,4)
    ask5: np.ndarray | None
    ema: np.ndarray
    st: np.ndarray
    sdir: np.ndarray
    ub: np.ndarray
    lb: np.ndarray
    atr: np.ndarray
    hist: np.ndarray
    fast: np.ndarray
    slow: np.ndarray
    sig: np.ndarray
    macd: np.ndarray
    loc_hour: np.ndarray
    loc_date: np.ndarray               # datetime64[D] in tz
    loc_wday: np.ndarray
    lon_hour: np.ndarray               # London hour (indicator 1 hard-codes 09-19 London)
    t1: np.ndarray | None
    bid1: np.ndarray | None
    ask1: np.ndarray | None
    h1_t: np.ndarray
    h1_adx: np.ndarray
    notes: list = field(default_factory=list)


def _sig_cols(df: pd.DataFrame, basis: str, has_ask: bool) -> np.ndarray:
    b = df[["bo", "bh", "bl", "bc"]].to_numpy(float)
    if basis == "mid" and has_ask:
        a = df[["ao", "ah", "al", "ac"]].to_numpy(float)
        return (a + b) / 2
    return b


def prepare(pd_: PairData, P: dict) -> Prepared:
    m5 = pd_.m5
    has_ask = pd_.has_ask and "ao" in m5.columns
    sig = _sig_cols(m5, P["price_basis"], has_ask)
    o, h, l, c = sig[:, 0], sig[:, 1], sig[:, 2], sig[:, 3]
    e = ind.ema(c, int(P["ema_len"]))
    st, sd, ub, lb, a = ind.supertrend(h, l, c, float(P["st_factor"]), int(P["st_atr"]))
    m, sg, hist, f, s = ind.macd(c, int(P["macd_fast"]), int(P["macd_slow"]), int(P["macd_signal"]))
    idx_local = m5.index.tz_convert(P["tz"])
    h1 = pd_.h1
    hs = _sig_cols(h1, P["price_basis"], has_ask and "ao" in h1.columns)
    adx = ind.custom_adx(hs[:, 1], hs[:, 2], hs[:, 3], int(P["adx_len"]))
    t1 = bid1 = ask1 = None
    if pd_.m1 is not None and len(pd_.m1):
        t1 = (pd_.m1.index.asi8 // 10**9).astype(np.int64)
        bid1 = pd_.m1[["bo", "bh", "bl", "bc"]].to_numpy(float)
        ask1 = pd_.m1[["ao", "ah", "al", "ac"]].to_numpy(float) if has_ask and "ao" in pd_.m1.columns else None
    return Prepared(
        pair=pd_.pair, pip=pip_size(pd_.pair), tick=tick_size(pd_.pair), has_ask=has_ask,
        spread=float(P["spread_pips"].get(pd_.pair, 1.0)) * pip_size(pd_.pair),
        t5=(m5.index.asi8 // 10**9).astype(np.int64), o=o, h=h, l=l, c=c,
        bid5=m5[["bo", "bh", "bl", "bc"]].to_numpy(float),
        ask5=m5[["ao", "ah", "al", "ac"]].to_numpy(float) if has_ask else None,
        ema=e, st=st, sdir=sd, ub=ub, lb=lb, atr=a, hist=hist, fast=f, slow=s, sig=sg, macd=m,
        loc_hour=idx_local.hour.to_numpy(), loc_date=idx_local.tz_localize(None).normalize().to_numpy().astype("datetime64[D]"),
        loc_wday=idx_local.weekday.to_numpy(), lon_hour=m5.index.tz_convert("Europe/London").hour.to_numpy(),
        t1=t1, bid1=bid1, ask1=ask1,
        h1_t=(h1.index.asi8 // 10**9).astype(np.int64), h1_adx=adx, notes=list(pd_.notes))


# --------------------------------------------------------------------------------------------- events
def effective_colors(hist: np.ndarray, eps: float, mode: str):
    """Returns (green_effective, adjusted_mask). TradingView paints hist >= 0 green."""
    disp = hist >= 0
    adj = np.zeros(len(hist), dtype=bool)
    if mode == "zero_is_red":
        eff = hist >= eps if eps > 0 else hist > 0
        adj = eff != disp
        return eff, adj
    eff = disp.copy()
    if mode == "against":
        # a bar that displays 0 next to a colour change never counts as the NEW colour:
        # the flip must happen on a bar with a real (non-zero) value
        z = np.abs(hist) < eps if eps > 0 else hist == 0
        flips = np.nonzero(disp[1:] != disp[:-1])[0] + 1
        for f in flips:
            new = disp[f]
            for j in (f - 1, f, f + 1):
                if 0 <= j < len(hist) and z[j] and not adj[j] and eff[j] == new:
                    eff[j] = not new
                    adj[j] = True
        return eff, adj
    if mode == "literal":
        z = np.abs(hist) < eps if eps > 0 else hist == 0
        flips = np.nonzero(disp[1:] != disp[:-1])[0] + 1
        for f in flips:
            for j in (f - 1, f, f + 1):
                if 0 <= j < len(hist) and z[j] and not adj[j]:
                    eff[j] = not disp[j]
                    adj[j] = True
    return eff, adj


def find_hooks(hist: np.ndarray, eff: np.ndarray, hmin: int, hmax: int):
    """dict bar -> (direction, receding_count) for every effective colour flip (valid or not)."""
    out = {}
    n = len(hist)
    for f in range(2, n):
        if math.isnan(hist[f]) or math.isnan(hist[f - 1]):
            continue
        if eff[f] and not eff[f - 1]:
            d = 1
        elif (not eff[f]) and eff[f - 1]:
            d = -1
        else:
            continue
        k = 0
        j = f - 1
        while j >= 1 and not math.isnan(hist[j - 1]):
            if d == 1 and (not eff[j]) and hist[j] > hist[j - 1]:
                k += 1
            elif d == -1 and eff[j] and hist[j] < hist[j - 1]:
                k += 1
            else:
                break
            j -= 1
        momentum = (hist[f] > hist[f - 1]) if d == 1 else (hist[f] < hist[f - 1])
        out[f] = (d, k, bool(momentum and hmin <= k <= hmax))
    return out


def find_bos(pp: Prepared, P: dict):
    """dict bar -> (direction, pierce_pips, range_pips, valid, why_invalid)

    cross_candle (default): the BOS candle is the candle whose close crosses the EMA line
        (previous close on/below the EMA, this close above it - mirrored for sells). Only that candle
        is measured: its BODY top (bottom for sells) must be >= buffer beyond the EMA. If it fails,
        later candles do not rescue the break.
    indicator: ta.crossover(close, ema + buffer) like indicator 1 - may be a later candle.
    """
    buf = float(P["bos_buffer_pips"])
    maxr = float(P["bos_max_range_pips"])
    rng = np.round((pp.h - pp.l) / pp.pip, 6)
    body_top = np.round((np.maximum(pp.o, pp.c) - pp.ema) / pp.pip, 6)
    body_bot = np.round((pp.ema - np.minimum(pp.o, pp.c)) / pp.pip, 6)
    dist = np.round((pp.c - pp.ema) / pp.pip, 6)
    out = {}
    for i in range(1, len(pp.c)):
        if math.isnan(pp.ema[i]) or math.isnan(pp.ema[i - 1]):
            continue
        if P["bos_mode"] == "indicator":
            if dist[i] >= buf and dist[i - 1] < buf:
                d, pierce = 1, float(dist[i])
            elif dist[i] <= -buf and dist[i - 1] > -buf:
                d, pierce = -1, float(-dist[i])
            else:
                continue
        else:
            if pp.c[i - 1] <= pp.ema[i - 1] and pp.c[i] > pp.ema[i]:
                d, pierce = 1, float(body_top[i])
            elif pp.c[i - 1] >= pp.ema[i - 1] and pp.c[i] < pp.ema[i]:
                d, pierce = -1, float(body_bot[i])
            else:
                continue
        why = []
        if pierce < buf:
            why.append(f"body only {pierce:.2f}p beyond EMA")
        if rng[i] > maxr:
            why.append(f"range {rng[i]:.1f}p")
        out[i] = (d, pierce, float(rng[i]), not why, ", ".join(why))
    return out


def find_st_flips(pp: Prepared):
    out = {}
    d = pp.sdir
    for i in range(1, len(d)):
        if d[i - 1] == 1 and d[i] == -1:
            out[i] = 1
        elif d[i - 1] == -1 and d[i] == 1:
            out[i] = -1
    return out


# --------------------------------------------------------------------------------------------- price access
class PriceAccess:
    """Sub-bar prices: OANDA S5 (exact) -> 1m bars -> 5m bars (approximation)."""

    def __init__(self, pp: Prepared, P: dict, s5cache=None, oanda_client=None):
        self.pp, self.P, self.s5, self.client = pp, P, s5cache, oanda_client

    def s5_rows(self, t0: int, t1: int) -> np.ndarray | None:
        if self.s5 is None or self.client is None:
            return None
        if not self.s5.has_block(self.pp.pair, t0):
            try:
                blocks = [(self.pp.pair, b) for b in range(t0 // 300 * 300, t1, 300)]
                self.s5.ensure(self.client, blocks, workers=2)
            except Exception:
                return None
        return self.s5.get(self.pp.pair, t0, t1)

    def _ask_from_bid(self, x):
        return x + self.pp.spread

    def snapshot(self, k: int, delay: int) -> dict:
        """State of 5m bar k at open+delay.

        px / ph / pl : decision values (signal basis) built ONLY from prices known before that second
                       (no look-ahead): last price, and partial high/low of the forming bar.
        bid / ask    : estimated fill of a market order sent at that second.
        With S5 data both are exact. With 1m/5m bars the decision uses the last fully known price
        (the open of the current 1m / 5m bar) and the fill is modelled by `subminute_model`.
        """
        pp = self.pp
        t0 = int(pp.t5[k])
        T = t0 + delay
        mid = self.P["price_basis"] == "mid" and pp.has_ask
        sig = (lambda b_, a_: (b_ + a_) / 2) if mid else (lambda b_, a_: b_)
        o5 = pp.o[k]
        ask_o5 = pp.ask5[k, 0] if pp.ask5 is not None else pp.bid5[k, 0] + pp.spread
        rows = self.s5_rows(t0, t0 + 300)
        if rows is not None and len(rows):
            before = rows[rows[:, 0] < T]
            at = rows[rows[:, 0] >= T]
            if len(before):
                dbid, dask = before[-1, 4], before[-1, 8]
                hs = sig(before[:, 2], before[:, 6])
                ls = sig(before[:, 3], before[:, 7])
                ph, pl = max(o5, float(hs.max())), min(o5, float(ls.min()))
            else:
                dbid, dask = pp.bid5[k, 0], ask_o5
                ph = pl = o5
            px = sig(dbid, dask)
            ph, pl = max(ph, px), min(pl, px)
            if len(at) and at[0, 0] < T + 5:
                bid, ask = at[0, 1], at[0, 5]          # next tick after the order is sent
            else:
                bid, ask = dbid, dask
            return {"px": px, "ph": ph, "pl": pl, "bid": bid, "ask": ask, "tier": "S5", "lo": px, "hi": px}
        model = self.P["subminute_model"]
        if pp.t1 is not None:
            mstart = T // 60 * 60
            j = int(np.searchsorted(pp.t1, mstart))
            if j < len(pp.t1) and pp.t1[j] == mstart and pp.t1[0] <= t0:
                frac = (T - mstart) / 60.0
                bo, bh, bl, bc = pp.bid1[j]
                if pp.ask1 is not None:
                    ao, ah, al, ac = pp.ask1[j]
                else:
                    ao, ac = bo + pp.spread, bc + pp.spread
                # complete minutes of this 5m bar before the current minute are known
                j0 = int(np.searchsorted(pp.t1, t0))
                prev = slice(j0, j)
                if mid and pp.ask1 is not None:
                    hs = (pp.bid1[prev, 1] + pp.ask1[prev, 1]) / 2
                    ls = (pp.bid1[prev, 2] + pp.ask1[prev, 2]) / 2
                else:
                    hs, ls = pp.bid1[prev, 1], pp.bid1[prev, 2]
                px = sig(bo, ao)  # the current minute's open is the last price known for sure
                ph = max([o5, px] + list(hs))
                pl = min([o5, px] + list(ls))
                if model == "open":
                    bid, ask = bo, ao
                elif model == "close":
                    bid, ask = bc, ac
                else:
                    bid, ask = bo + (bc - bo) * frac, ao + (ac - ao) * frac
                if self.P["decision_price"] == "fill":
                    px = sig(bid, ask)
                    ph, pl = max(ph, px), min(pl, px)
                elif self.P["decision_price"] == "bar_close":
                    px, ph, pl = pp.c[k], pp.h[k], pp.l[k]
                mlo = (bl + pp.ask1[j][2]) / 2 if (mid and pp.ask1 is not None) else bl
                mhi = (bh + pp.ask1[j][1]) / 2 if (mid and pp.ask1 is not None) else bh
                return {"px": px, "ph": ph, "pl": pl, "bid": bid, "ask": ask, "tier": "1m", "lo": mlo, "hi": mhi}
        frac = delay / 300.0
        bo, bc = pp.bid5[k, 0], pp.bid5[k, 3]
        if pp.ask5 is not None:
            ao, ac = pp.ask5[k, 0], pp.ask5[k, 3]
        else:
            ao, ac = bo + pp.spread, bc + pp.spread
        if model == "open":
            bid, ask = bo, ao
        elif model == "close":
            bid, ask = bc, ac
        else:
            bid, ask = bo + (bc - bo) * frac, ao + (ac - ao) * frac
        if self.P["decision_price"] == "bar_close":
            return {"px": pp.c[k], "ph": pp.h[k], "pl": pp.l[k], "bid": bid, "ask": ask, "tier": "5m", "lo": pp.l[k], "hi": pp.h[k]}
        px = sig(bo, ao) if self.P["decision_price"] != "fill" else sig(bid, ask)
        return {"px": px, "ph": max(o5, px), "pl": min(o5, px), "bid": bid, "ask": ask, "tier": "5m",
                "lo": pp.l[k], "hi": pp.h[k]}


def momentum_check(pp: Prepared, P: dict, d: int, prev: int, snap: dict, refs: list[float], bar: int) -> tuple[str, float]:
    """Is the forming bar `bar`'s histogram beyond every value in `refs` at +delay?

    Returns (status, critical_price); status in pass | fail | ambiguous_pass | ambiguous_fail.
    """
    fa, sl_, sg = int(P["macd_fast"]), int(P["macd_slow"]), int(P["macd_signal"])
    crit = [ind.macd_critical_price(r, pp.fast[prev], pp.slow[prev], pp.sig[prev], fa, sl_, sg) for r in refs]
    need = max(crit) if d == 1 else min(crit)
    ok = (lambda p: p > need) if d == 1 else (lambda p: p < need)
    mode = P["momentum_no5s"]
    if snap["tier"] == "S5":
        return ("pass" if ok(snap["px"]) else "fail"), need
    if mode == "bar_close":
        h = pp.hist[bar]
        return ("pass" if all((h > r) if d == 1 else (h < r) for r in refs) else "fail"), need
    if mode == "open":
        return ("pass" if ok(snap["px"]) else "fail"), need
    a, b = ok(snap["lo"]), ok(snap["hi"])
    if a and b:
        return "pass", need
    if not a and not b:
        return "fail", need
    return ("ambiguous_fail" if mode == "range_reject" else "ambiguous_pass"), need


# --------------------------------------------------------------------------------------------- simulation
def _bars_after(pp: Prepared, t_from: int):
    """Yield (t, res_sec, bid_ohlc, ask_ohlc) starting with the 5m bar that contains t_from,
    using 1m bars where available, else 5m bars."""
    i5 = max(int(np.searchsorted(pp.t5, t_from, side="right")) - 1, 0)
    t1_start = pp.t1[0] if pp.t1 is not None else None
    t1_end = pp.t1[-1] if pp.t1 is not None else None
    while i5 < len(pp.t5):
        tb = int(pp.t5[i5])
        if pp.t1 is not None and t1_start <= tb <= t1_end:
            j = np.searchsorted(pp.t1, tb)
            jend = np.searchsorted(pp.t1, tb + 300)
            if jend > j:
                for jj in range(j, jend):
                    b = pp.bid1[jj]
                    a = pp.ask1[jj] if pp.ask1 is not None else b + pp.spread
                    yield int(pp.t1[jj]), 60, b, a
                i5 += 1
                continue
        b = pp.bid5[i5]
        a = pp.ask5[i5] if pp.ask5 is not None else b + pp.spread
        yield tb, 300, b, a
        i5 += 1




def simulate_exit(pa: PriceAccess, d: int, k_entry: int, T: int, entry: float, sl: float, tp: float,
                  deadline: int | None, deadline_reason: str, tie: str,
                  adjust_at: int | None = None, sl2: float | None = None, tp2: float | None = None,
                  be: dict | None = None):
    """Walk the price path after the entry. Longs exit on bid, shorts on ask.

    Finest data first: OANDA S5 for the entry bar and for any 1m bar where SL and TP are both
    touched; otherwise 1m bars, otherwise 5m bars. Without finer data the bar in which the entry
    happened is treated pessimistically (SL counts if touched, TP only if the bar closed beyond it).
    If adjust_at is given, SL/TP become sl2/tp2 for every bar starting at or after adjust_at
    (used to move the stop once the entry candle has closed).
    be = {"pct": %, "off": price offset}: once the exit-side price has covered pct % of the distance
    entry -> TP, the stop moves to entry +/- off (never loosened). The move takes effect from the next
    5s/1m/5m bar, because inside one bar the order of the high and the low is unknown.
    Returns (exit_t, exit_px, reason, mae_price, mfe_price, tiers); reason "be" = break-even stop.
    """
    L = [sl, tp]
    st = {"adjusted": False, "be": False}

    def tighter(a, b):
        return max(a, b) if d == 1 else min(a, b)

    def at(t):
        if adjust_at is not None and t >= adjust_at and not st["adjusted"]:
            st["adjusted"] = True
            L[1] = tp2
            L[0] = tighter(L[0], sl2) if st["be"] else sl2

    def trigger(hi, lo, cl, partial):
        if be is None or st["be"]:
            return
        lvl = entry + d * (be["pct"] / 100.0) * abs(L[1] - entry)
        if d == 1:
            reached = (cl if partial else hi) >= lvl
        else:
            reached = (cl if partial else lo) <= lvl
        if reached:
            st["be"] = True
            L[0] = tighter(L[0], entry + d * be["off"])

    def why_sl():
        return "be" if st["be"] else "sl"
    pp = pa.pp
    side = 0 if d == 1 else 1
    ex = {"worst": 0.0, "best": 0.0}
    tiers: set = set()

    def exc(hi, lo):
        if d == 1:
            ex["worst"] = min(ex["worst"], lo - entry)
            ex["best"] = max(ex["best"], hi - entry)
        else:
            ex["worst"] = min(ex["worst"], entry - hi)
            ex["best"] = max(ex["best"], entry - lo)

    def check(o, hi, lo, cl, partial):
        if d == 1:
            if not partial and o <= L[0]:
                return o, why_sl()
            if not partial and o >= L[1]:
                return o, "tp"
            hit_sl, hit_tp = lo <= L[0], (cl >= L[1]) if partial else (hi >= L[1])
        else:
            if not partial and o >= L[0]:
                return o, why_sl()
            if not partial and o <= L[1]:
                return o, "tp"
            hit_sl, hit_tp = hi >= L[0], (cl <= L[1]) if partial else (lo <= L[1])
        if hit_sl and hit_tp:
            return None, "tie"
        if hit_sl:
            return L[0], why_sl()
        if hit_tp:
            return L[1], "tp"
        return None

    def walk_s5(rows, t_min):
        for r in rows:
            t = int(r[0])
            if t < t_min:
                continue
            ohlc = r[1:5] if side == 0 else r[5:9]
            at(t)
            if deadline is not None and t >= deadline:
                return t, ohlc[0], deadline_reason
            res = check(*ohlc, partial=False)
            exc(ohlc[1], ohlc[2])
            if res is None:
                trigger(ohlc[1], ohlc[2], ohlc[3], False)
                continue
            if res[1] == "tie":
                return (t, L[0], why_sl()) if tie == "pessimistic" else (t, L[1], "tp")
            return t, res[0], res[1]
        return None

    if deadline is not None and deadline < T:
        deadline = T
    bar0 = int(pp.t5[k_entry])
    scan_from = bar0
    if pa.client is not None:
        rows = pa.s5_rows(bar0, bar0 + 300)
        if rows is not None and len(rows):
            tiers.add("S5")
            r = walk_s5(rows, T)
            if r:
                return (*r, ex["worst"], ex["best"], tiers)
            scan_from = bar0 + 300
    for tb, rs, b, a in _bars_after(pp, scan_from):
        if tb < scan_from or tb + rs <= T:
            continue
        ohlc = b if side == 0 else a
        at(tb)
        if deadline is not None and tb >= deadline:
            return tb, ohlc[0], deadline_reason, ex["worst"], ex["best"], tiers
        partial = tb < T
        tiers.add("1m" if rs == 60 else "5m")
        if deadline is not None and tb < deadline < tb + rs:
            res = check(*ohlc, partial=True)
            exc(ohlc[1], ohlc[2])
            if res is not None and res[1] in ("sl", "be", "tie"):
                return max(tb, T), L[0], why_sl(), ex["worst"], ex["best"], tiers
            frac = (deadline - tb) / rs
            return deadline, ohlc[0] + (ohlc[3] - ohlc[0]) * frac, deadline_reason, ex["worst"], ex["best"], tiers
        res = check(*ohlc, partial=partial)
        if res is None:
            exc(ohlc[1], ohlc[2])
            trigger(ohlc[1], ohlc[2], ohlc[3], partial)
            continue
        if res[1] == "tie":
            if pa.client is not None and rs == 60 and not partial:
                rows = pa.s5_rows(tb, tb + rs)
                if rows is not None and len(rows):
                    r = walk_s5(rows, tb)
                    if r:
                        tiers.add("S5")
                        return (*r, ex["worst"], ex["best"], tiers)
            exc(ohlc[1], ohlc[2])
            tiers.add("tie-" + tie)
            px, why = (L[0], why_sl()) if tie == "pessimistic" else (L[1], "tp")
            return max(tb, T), px, why, ex["worst"], ex["best"], tiers
        exc(ohlc[1], ohlc[2])
        return max(tb, T), res[0], res[1], ex["worst"], ex["best"], tiers
    last = pp.bid5[-1, 3] if side == 0 else (pp.ask5[-1, 3] if pp.ask5 is not None else pp.bid5[-1, 3] + pp.spread)
    return int(pp.t5[-1]) + 300, last, "end_of_data", ex["worst"], ex["best"], tiers


# --------------------------------------------------------------------------------------------- sessions & news
def _local_ts(date: np.datetime64, hour: int, tz: str) -> int:
    ts = pd.Timestamp(date) + pd.Timedelta(hours=hour)
    return int(ts.tz_localize(tz, ambiguous=False, nonexistent="shift_forward").timestamp())


def session_table(pp: Prepared, P: dict) -> dict:
    tab = {}
    lo, hi = float(P["adx_min"]), float(P["adx_max"])
    close_t = pp.h1_t + 3600
    for date in np.unique(pp.loc_date):
        for s in (1, 2):
            ct = _local_ts(date, int(P[f"s{s}_check_hour"]), P["tz"])
            if P["adx_bar"] == "check_bar":
                i = int(np.searchsorted(pp.h1_t, ct, side="right")) - 1
            else:
                i = int(np.searchsorted(close_t, ct, side="right")) - 1
            val = float(pp.h1_adx[i]) if i >= 0 else float("nan")
            bar_t = int(pp.h1_t[i]) if i >= 0 else None
            ok = (not math.isnan(val)) and lo <= val <= hi
            tab[(date, s)] = (val, ok, bar_t, ct)
    return tab


def gate(pp: Prepared, P: dict, tab: dict, k: int):
    hr = int(pp.loc_hour[k])
    date = pp.loc_date[k]
    in1 = int(P["s1_check_hour"]) <= hr < int(P["s1_end_hour"])
    in2 = int(P["s2_check_hour"]) <= hr < int(P["s2_end_hour"])
    if not P["use_adx_filter"]:
        return (in1 or in2), ("S1" if in1 else "S2" if in2 else None), None
    if in1 and tab.get((date, 1), (0, False))[1]:
        return True, "S1", tab[(date, 1)][0]
    if in2 and tab.get((date, 2), (0, False))[1]:
        return True, "S2", tab[(date, 2)][0]
    return False, ("S1" if in1 else "S2" if in2 else None), (tab.get((date, 1 if in1 else 2), (None,))[0]
                                                             if (in1 or in2) else None)


def news_times(events: pd.DataFrame, pair: str, P: dict) -> tuple[np.ndarray, list]:
    from ..data.calendar import COUNTRY_CCY
    if events is None or events.empty or not P["use_news"]:
        return np.array([], dtype=np.int64), []
    ev = events[(events["importance"] >= int(P["news_min_importance"])) &
                (events["country"].isin(P["news_countries"]))]
    if P["news_scope"] == "pair":
        ccys = {pair[:3], pair[3:]}
        ev = ev[ev["country"].map(COUNTRY_CCY).isin(ccys)]
    t = (ev["t"].astype("int64") // 10**9).to_numpy(np.int64)
    order = np.argsort(t, kind="stable")
    return t[order], [ev.iloc[i] for i in order]


# --------------------------------------------------------------------------------------------- candidates
def build_events(pp: Prepared, P: dict):
    eps = float(P["zero_eps_ticks"]) * pp.tick
    eff, adj = effective_colors(pp.hist, eps, P["zero_mode"])
    hooks = find_hooks(pp.hist, eff, int(P["hook_min"]), int(P["hook_max"]))
    bos = find_bos(pp, P)
    flips = find_st_flips(pp)
    return eff, adj, hooks, bos, flips


def build_candidates(pp: Prepared, P: dict, hooks, bos, flips):
    before, after = int(P["bos_before_st_max"]), int(P["bos_after_st_max"])
    win, aft = int(P["macd_window_bars"]), int(P["macd_after_span_max"])
    cands = {}
    for S, d in flips.items():
        for B in range(S - before, S + after + 1):
            b = bos.get(B)
            if not b or b[0] != d or not b[3]:
                continue
            E = max(S, B)
            for H in range(E - (win - 1), E + aft + 1):
                hk = hooks.get(H)
                if not hk or hk[0] != d or not hk[2]:
                    continue
                C = max(H, E)
                if C + 1 >= len(pp.t5):
                    continue
                key = (d, C)
                score = abs(H - E)
                if key not in cands or score < cands[key][0]:
                    cands[key] = (score, S, B, H)
    return [{"d": d, "C": C, "S": v[1], "B": v[2], "H": v[3]} for (d, C), v in sorted(cands.items(), key=lambda x: x[0][1])]


# --------------------------------------------------------------------------------------------- Pine replicas
def pine_indicator_signals(pp: Prepared, P: dict, tab: dict, ntimes: np.ndarray):
    """Exactly what the two TradingView indicators would mark on the 5m chart (for comparison)."""
    n = len(pp.c)
    buf = float(P["bos_buffer_pips"]) * pp.tick * 10
    up = ind.crossover(pp.c, pp.ema + buf)
    dn = ind.crossunder(pp.c, pp.ema - buf)
    bsu = bsd = 999
    i1 = []
    nb, na = int(P["news_before_min"]) * 60, int(P["news_after_min"]) * 60
    for i in range(1, n):
        bsu = 0 if up[i] else bsu + 1
        bsd = 0 if dn[i] else bsd + 1
        ch = pp.sdir[i] - pp.sdir[i - 1]
        if math.isnan(ch) or ch == 0:
            continue
        t = int(pp.t5[i])
        j = np.searchsorted(ntimes, t - na)
        danger = j < len(ntimes) and ntimes[j] <= t + nb
        london_ok = 9 <= pp.lon_hour[i] < 19
        if ch < 0 and bsu <= 2 and not danger and london_ok:
            i1.append((i, 1))
        if ch > 0 and bsd <= 2 and not danger and london_ok:
            i1.append((i, -1))
    # indicator 2
    hist = pp.hist
    decay = growth = 0
    saved_d = saved_g = 0
    prev_decay = prev_growth = 0
    hmin, hmax = int(P["hook_min"]), int(P["hook_max"])
    i2 = []
    for i in range(1, n):
        hcur, hprev = hist[i], hist[i - 1]
        if hcur > 0:
            decay = decay + 1 if hcur < hprev else 0
            growth = 0
        elif hcur < 0:
            growth = growth + 1 if hcur > hprev else 0
            decay = 0
        zu = hcur > 0 and hprev <= 0
        zd = hcur < 0 and hprev >= 0
        if zu:
            saved_g = prev_growth
        if zd:
            saved_d = prev_decay
        vb = zu and hmin <= saved_g <= hmax
        vs = zd and hmin <= saved_d <= hmax
        if zu or zd:
            decay = growth = 0
        prev_decay, prev_growth = decay, growth
        if vb or vs:
            ok, _, _ = gate(pp, {**P, "use_adx_filter": True}, tab, i)
            if ok:
                i2.append((i, 1 if vb else -1))
    return i1, i2
