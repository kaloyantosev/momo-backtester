"""Bar-by-bar replicas of the Pine Script built-ins / custom functions used by the two indicators.

Every function follows the Pine reference implementation (including na seeding and nz() quirks),
so values converge to TradingView's after the warm-up period.
"""
from __future__ import annotations

import math

import numpy as np

NaN = float("nan")


def ema(src: np.ndarray, n: int) -> np.ndarray:
    """ta.ema: alpha = 2/(n+1), seeded with the SMA of the first n non-na values (as the built-in plots)."""
    out = np.full(len(src), NaN)
    alpha = 2.0 / (n + 1)
    acc, cnt, prev = 0.0, 0, NaN
    for i, x in enumerate(src):
        if math.isnan(x):
            continue
        if math.isnan(prev):
            acc += x
            cnt += 1
            if cnt == n:
                prev = acc / n
                out[i] = prev
            continue
        prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def sma(src: np.ndarray, n: int) -> np.ndarray:
    """ta.sma: na until n values are available; na if any value in the window is na."""
    out = np.full(len(src), NaN)
    s = np.asarray(src, dtype=float)
    for i in range(n - 1, len(s)):
        w = s[i - n + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = w.sum() / n
    return out


def rma(src: np.ndarray, n: int) -> np.ndarray:
    """ta.rma: alpha = 1/n, seeded with SMA of the first n values."""
    out = np.full(len(src), NaN)
    alpha = 1.0 / n
    acc, cnt, prev = 0.0, 0, NaN
    for i, x in enumerate(src):
        if math.isnan(x):
            continue
        if math.isnan(prev):
            acc += x
            cnt += 1
            if cnt == n:
                prev = acc / n
                out[i] = prev
            continue
        prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """ta.tr(true) as used by ta.atr: high-low on the first bar."""
    tr = np.empty(len(h))
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum(np.maximum(h[1:] - l[1:], np.abs(h[1:] - pc)), np.abs(l[1:] - pc))
    return tr


def atr(h, l, c, n):
    return rma(true_range(h, l, c), n)


def supertrend(h: np.ndarray, l: np.ndarray, c: np.ndarray, factor: float, n: int):
    """ta.supertrend(factor, atrPeriod) -> (supertrend, direction, upperBand, lowerBand, atr)

    direction: -1 = up-trend (green line below price), +1 = down-trend.
    """
    a = atr(h, l, c, n)
    size = len(c)
    st = np.full(size, NaN)
    d = np.full(size, NaN)
    ub = np.full(size, NaN)
    lb = np.full(size, NaN)
    prev_ub = prev_lb = NaN
    prev_st = NaN
    for i in range(size):
        src = (h[i] + l[i]) / 2
        upper = src + factor * a[i]
        lower = src - factor * a[i]
        p_lb = 0.0 if math.isnan(prev_lb) else prev_lb
        p_ub = 0.0 if math.isnan(prev_ub) else prev_ub
        pc = c[i - 1] if i > 0 else NaN
        # lowerBand := lowerBand > prevLowerBand or close[1] < prevLowerBand ? lowerBand : prevLowerBand
        lower = lower if (lower > p_lb or (pc < p_lb)) else p_lb
        upper = upper if (upper < p_ub or (pc > p_ub)) else p_ub
        if i == 0 or math.isnan(a[i - 1]):
            di = 1
        elif prev_st == p_ub:
            di = -1 if c[i] > upper else 1
        else:
            di = 1 if c[i] < lower else -1
        s = lower if di == -1 else upper
        st[i], d[i], ub[i], lb[i] = s, di, upper, lower
        prev_ub, prev_lb, prev_st = upper, lower, s
    return st, d, ub, lb, a


def supertrend_live(h_part: float, l_part: float, c_now: float, prev_close: float, prev_atr: float,
                    prev_ub: float, prev_lb: float, prev_st: float, factor: float, n: int):
    """Supertrend value of a still-forming bar, given its partial high/low and current price."""
    tr = max(h_part - l_part, abs(h_part - prev_close), abs(l_part - prev_close))
    a = (prev_atr * (n - 1) + tr) / n
    src = (h_part + l_part) / 2
    upper = src + factor * a
    lower = src - factor * a
    lower = lower if (lower > prev_lb or prev_close < prev_lb) else prev_lb
    upper = upper if (upper < prev_ub or prev_close > prev_ub) else prev_ub
    if prev_st == prev_ub:
        di = -1 if c_now > upper else 1
    else:
        di = 1 if c_now < lower else -1
    return (lower if di == -1 else upper), di


def macd(src: np.ndarray, fast: int, slow: int, signal: int, osc_type: str = "EMA", sig_type: str = "EMA"):
    f = ema(src, fast) if osc_type == "EMA" else sma(src, fast)
    s = ema(src, slow) if osc_type == "EMA" else sma(src, slow)
    m = f - s
    sg = ema(m, signal) if sig_type == "EMA" else sma(m, signal)
    return m, sg, m - sg, f, s


def macd_live(price: float, prev_fast: float, prev_slow: float, prev_sig: float, fast: int, slow: int, signal: int):
    """Histogram of a forming bar (EMA/EMA only) using `price` as the bar's current close."""
    af, as_, ag = 2 / (fast + 1), 2 / (slow + 1), 2 / (signal + 1)
    f = af * price + (1 - af) * prev_fast
    s = as_ * price + (1 - as_) * prev_slow
    m = f - s
    sg = ag * m + (1 - ag) * prev_sig
    return m - sg


def macd_critical_price(ref: float, prev_fast: float, prev_slow: float, prev_sig: float,
                        fast: int, slow: int, signal: int) -> float:
    """Price at which the forming bar's histogram equals `ref`.

    The forming-bar histogram is linear and strictly increasing in the current price:
        h(p) = (1-ag) * (F - S - G - af*F + as*S + (af - as) * p)
    so for buys "histogram above ref" <=> price above this level (below it for sells).
    """
    af, as_, ag = 2 / (fast + 1), 2 / (slow + 1), 2 / (signal + 1)
    return (ref / (1 - ag) - (prev_fast - prev_slow - prev_sig) + af * prev_fast - as_ * prev_slow) / (af - as_)


def custom_adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    """calc_adx() from 'MACD + ADX Auto-Scale v2' (non-standard: SMA of DX, sums seeded from 0, nz(close[1]))."""
    size = len(c)
    dx = np.empty(size)
    str_ = sp = sm = 0.0
    for i in range(size):
        pc = c[i - 1] if i > 0 else 0.0
        ph = h[i - 1] if i > 0 else 0.0
        pl = l[i - 1] if i > 0 else 0.0
        tr = max(max(h[i] - l[i], abs(h[i] - pc)), abs(l[i] - pc))
        up = h[i] - ph
        dn = pl - l[i]
        dmp = max(up, 0.0) if up > dn else 0.0
        dmm = max(dn, 0.0) if dn > up else 0.0
        str_ = str_ - str_ / n + tr
        sp = sp - sp / n + dmp
        sm = sm - sm / n + dmm
        dip = sp / str_ * 100 if str_ > 0 else 0.0
        dim = sm / str_ * 100 if str_ > 0 else 0.0
        dx[i] = abs(dip - dim) / (dip + dim) * 100 if (dip + dim) > 0 else 0.0
    return sma(dx, n)


def crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    out = np.zeros(len(a), dtype=bool)
    out[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
    return out


def crossunder(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    out = np.zeros(len(a), dtype=bool)
    out[1:] = (a[1:] < b[1:]) & (a[:-1] >= b[:-1])
    return out
