"""Bar-by-bar replicas of the Pine Script built-ins / custom functions used by the two indicators.

Every function follows the Pine reference implementation (including na seeding and nz() quirks),
so values converge to TradingView's after the warm-up period.
"""
from __future__ import annotations

import math

import numpy as np

NaN = float("nan")


def _seeded_recursive(src: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """alpha * x + (1 - alpha) * prev, seeded with the SMA of the first n non-na values."""
    x = src.tolist()
    out = [NaN] * len(x)
    acc, cnt = 0.0, 0
    prev = NaN
    seeded = False
    beta = 1 - alpha
    for i, v in enumerate(x):
        if v != v:
            continue
        if seeded:
            prev = alpha * v + beta * prev
            out[i] = prev
        else:
            acc += v
            cnt += 1
            if cnt == n:
                prev = acc / n
                out[i] = prev
                seeded = True
    return np.array(out, dtype=float)


def ema(src: np.ndarray, n: int) -> np.ndarray:
    """ta.ema: alpha = 2/(n+1), seeded with the SMA of the first n non-na values (as the built-in plots)."""
    return _seeded_recursive(src, n, 2.0 / (n + 1))


def sma(src: np.ndarray, n: int) -> np.ndarray:
    """ta.sma: na until n values are available; na if any value in the window is na."""
    out = np.full(len(src), NaN)
    s = np.asarray(src, dtype=float)
    if len(s) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(s, n).sum(axis=1) / n  # a na in the window gives na
    return out


def rma(src: np.ndarray, n: int) -> np.ndarray:
    """ta.rma: alpha = 1/n, seeded with SMA of the first n values."""
    return _seeded_recursive(src, n, 1.0 / n)


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
    hl, ll, cl, al = h.tolist(), l.tolist(), c.tolist(), a.tolist()
    st, d, ub, lb = [NaN] * size, [NaN] * size, [NaN] * size, [NaN] * size
    prev_ub = prev_lb = NaN
    prev_st = NaN
    for i in range(size):
        src = (hl[i] + ll[i]) / 2
        upper = src + factor * al[i]
        lower = src - factor * al[i]
        p_lb = 0.0 if prev_lb != prev_lb else prev_lb
        p_ub = 0.0 if prev_ub != prev_ub else prev_ub
        pc = cl[i - 1] if i > 0 else NaN
        # lowerBand := lowerBand > prevLowerBand or close[1] < prevLowerBand ? lowerBand : prevLowerBand
        lower = lower if (lower > p_lb or (pc < p_lb)) else p_lb
        upper = upper if (upper < p_ub or (pc > p_ub)) else p_ub
        if i == 0 or al[i - 1] != al[i - 1]:
            di = 1
        elif prev_st == p_ub:
            di = -1 if cl[i] > upper else 1
        else:
            di = 1 if cl[i] < lower else -1
        s = lower if di == -1 else upper
        st[i], d[i], ub[i], lb[i] = s, di, upper, lower
        prev_ub, prev_lb, prev_st = upper, lower, s
    return np.array(st), np.array(d, dtype=float), np.array(ub), np.array(lb), a


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
    hl, ll, cl = h.tolist(), l.tolist(), c.tolist()
    dx = [0.0] * size
    str_ = sp = sm = 0.0
    pc = ph = pl = 0.0
    for i in range(size):
        hi, lo = hl[i], ll[i]
        tr = max(max(hi - lo, abs(hi - pc)), abs(lo - pc))
        up = hi - ph
        dn = pl - lo
        dmp = max(up, 0.0) if up > dn else 0.0
        dmm = max(dn, 0.0) if dn > up else 0.0
        str_ = str_ - str_ / n + tr
        sp = sp - sp / n + dmp
        sm = sm - sm / n + dmm
        dip = sp / str_ * 100 if str_ > 0 else 0.0
        dim = sm / str_ * 100 if str_ > 0 else 0.0
        dx[i] = abs(dip - dim) / (dip + dim) * 100 if (dip + dim) > 0 else 0.0
        pc, ph, pl = cl[i], hi, lo
    dx = np.array(dx)
    return sma(dx, n)


def crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    out = np.zeros(len(a), dtype=bool)
    out[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
    return out


def crossunder(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    out = np.zeros(len(a), dtype=bool)
    out[1:] = (a[1:] < b[1:]) & (a[:-1] >= b[:-1])
    return out
