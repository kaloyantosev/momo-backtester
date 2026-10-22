"""Rule-level tests: each test pins down one interpretation of the written strategy rules."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.data.store import PairData  # noqa: E402
from server.engine import indicators as ind  # noqa: E402
from server.engine.strategy import (PriceAccess, build_candidates, effective_colors, find_bos, find_hooks,  # noqa: E402
                                    merge_params, prepare, simulate_exit)

EPS = 0.5e-5  # EURUSD: values below half a tick display as 0.00000


def hooks_of(h, mode="literal", hmin=2, hmax=5):  # most tests pin the 'literal' reading explicitly
    h = np.array(h, dtype=float)
    eff, adj = effective_colors(h, EPS, mode)
    return find_hooks(h, eff, hmin, hmax), eff, adj


# ------------------------------------------------------------------ MACD hook
def test_plain_bull_hook_counts_light_red_bars():
    hk, _, _ = hooks_of([-5e-5, -8e-5, -6e-5, -4e-5, -2e-5, 3e-5])
    assert hk[5] == (1, 3, True)  # 3 receding (light red) bars, then green


def test_too_many_receding_bars_invalid():
    hk, _, _ = hooks_of([-9e-5, -8e-5, -7e-5, -6e-5, -5e-5, -4e-5, -3e-5, 2e-5])
    d, k, ok = hk[7]
    assert d == 1 and k == 6 and not ok


def test_bear_hook():
    hk, _, _ = hooks_of([5e-5, 8e-5, 6e-5, 4e-5, -1e-5])
    assert hk[4] == (-1, 2, True)


def test_zero_bar_before_flip_shown_red_becomes_green_flip_moves_earlier():
    # bar 4 displays -0.00000 (red) right before the green bar -> treated as green, flip at 4
    hk, eff, adj = hooks_of([-9e-5, -7e-5, -5e-5, -3e-5, -1e-6, 4e-5])
    assert adj[4] and eff[4]
    assert 4 in hk and hk[4][0] == 1 and hk[4][1] == 3
    assert 5 not in hk


def test_zero_bar_shown_green_at_flip_counts_as_red_flip_moves_later():
    # bar 4 displays +0.00000 (green) -> treated as red, flip happens on bar 5
    hk, eff, adj = hooks_of([-9e-5, -7e-5, -5e-5, -3e-5, 1e-6, 4e-5])
    assert adj[4] and not eff[4]
    assert 4 not in hk and hk[5][0] == 1 and hk[5][1] == 4  # bars 1..4 receding


def test_raw_mode_uses_tradingview_colours():
    hk, _, _ = hooks_of([-9e-5, -7e-5, -5e-5, -3e-5, 1e-6, 4e-5], mode="raw")
    assert 4 in hk and hk[4][1] == 3


def test_zero_bar_right_after_flip_breaks_hook():
    # green flip at 4, bar 5 displays 0.00000 green -> red -> immediate reversal at 5
    hk, eff, _ = hooks_of([-9e-5, -7e-5, -5e-5, -3e-5, 2e-5, 1e-6, 3e-5])
    assert not eff[5]
    assert hk[5][0] == -1 and not hk[5][2]


# ------------------------------------------------------------------ helpers for bar-based tests
def make_pp(c, h=None, l=None, o=None, start="2026-01-05 08:00", P=None):
    c = np.array(c, dtype=float)
    o = c.copy() if o is None else np.array(o, dtype=float)
    h = np.maximum(o, c) + 0.00005 if h is None else np.array(h, dtype=float)
    l = np.minimum(o, c) - 0.00005 if l is None else np.array(l, dtype=float)
    idx = pd.date_range(start, periods=len(c), freq="5min", tz="UTC")
    m5 = pd.DataFrame({"bo": o, "bh": h, "bl": l, "bc": c}, index=idx)
    h1 = m5.resample("1h").agg({"bo": "first", "bh": "max", "bl": "min", "bc": "last"}).dropna()
    P = merge_params(P or {})
    return prepare(PairData("EURUSD", "test", m5, h1, None, False), P), P


def test_bos_only_on_crossing_candle_body_not_wick():
    pp, P = make_pp([1.1] * 60)
    pp.ema[:] = 1.1000
    # crossing candle: body top 0.9 pip above EMA -> valid
    pp.o[40], pp.c[40], pp.h[40], pp.l[40] = 1.09995, 1.10009, 1.10015, 1.09990
    # crossing candle: close 0.5 pip above, wick 3 pips above -> wick does not count -> invalid
    pp.o[45], pp.c[45], pp.h[45], pp.l[45] = 1.09995, 1.10005, 1.10030, 1.09990
    # next candle closes 2 pips above but did not cross the EMA itself -> no BOS (no rescue)
    pp.o[46], pp.c[46], pp.h[46], pp.l[46] = 1.10005, 1.10020, 1.10025, 1.10000
    # crossing candle with 1.5 pip body but 13 pip range -> invalid
    pp.o[50], pp.c[50], pp.h[50], pp.l[50] = 1.09990, 1.10015, 1.10100, 1.09970
    # bearish crossing candle: body bottom 1.0 pip below EMA -> valid sell BOS
    pp.o[55], pp.c[55], pp.h[55], pp.l[55] = 1.10005, 1.09990, 1.10010, 1.09985
    pp.c[54] = 1.10003
    bos = find_bos(pp, P)
    assert bos[40][0] == 1 and bos[40][3] and abs(bos[40][1] - 0.9) < 1e-6
    assert bos[45][0] == 1 and not bos[45][3] and "body" in bos[45][4]
    assert 46 not in bos
    assert not bos[50][3] and "range" in bos[50][4] and abs(bos[50][2] - 13.0) < 1e-6
    assert bos[55][0] == -1 and bos[55][3] and abs(bos[55][1] - 1.0) < 1e-6


def test_bos_indicator_mode_accepts_later_candle():
    pp, P = make_pp([1.1] * 60, P={"bos_mode": "indicator"})
    pp.ema[:] = 1.1000
    pp.c[45] = 1.10003                          # crosses the EMA by 0.3 pip only
    pp.c[46] = 1.10020                          # crosses EMA + 0.8 pip one candle later
    bos = find_bos(pp, P)
    assert 45 not in bos and bos[46][0] == 1 and bos[46][3]


def test_confluence_windows():
    pp, P = make_pp([1.1] * 60)
    flips = {20: 1}
    bos = {21: (1, 1.0, 3.0, True)}           # BOS one bar after the ST flip -> span 20..21
    for H, expect in ((23, True), (24, False), (19, True), (18, False)):
        hooks = {H: (1, 3, True)}
        got = build_candidates(pp, P, hooks, bos, flips)
        assert bool(got) == expect, (H, got)
        if got:
            assert got[0]["C"] == max(H, 21)
    # BOS before the ST flip is not allowed by default (indicator 1 allows up to 2 bars)
    assert not build_candidates(pp, P, {21: (1, 3, True)}, {19: (1, 1.0, 3.0, True)}, flips)
    P2 = dict(P, bos_before_st_max=2)
    assert build_candidates(pp, P2, {21: (1, 3, True)}, {19: (1, 1.0, 3.0, True)}, flips)
    # BOS two bars after the flip -> invalid
    assert not build_candidates(pp, P, {22: (1, 3, True)}, {22: (1, 1.0, 3.0, True)}, flips)


# ------------------------------------------------------------------ exits
def test_exit_tp_sl_gap_and_tie():
    n = 20
    c = np.full(n, 1.1000)
    pp, P = make_pp(c, h=c + 0.0001, l=c - 0.0001)
    pa = PriceAccess(pp, P)
    T = int(pp.t5[5]) + 30
    # TP hit on bar 8
    pp.bid5[8, 1] = 1.1020
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, 1.1000, 1.0990, 1.1016, None, "", "pessimistic")
    assert why == "tp" and px == 1.1016 and t == pp.t5[8]
    # SL and TP inside the same bar -> pessimistic SL
    pp.bid5[8, 2] = 1.0980
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, 1.1000, 1.0990, 1.1016, None, "", "pessimistic")
    assert why == "sl" and px == 1.0990
    # gap through the stop -> filled at the open, worse than the stop
    pp.bid5[7, 0], pp.bid5[7, 2] = 1.0970, 1.0965
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, 1.1000, 1.0990, 1.1016, None, "", "pessimistic")
    assert why == "sl" and px == 1.0970 and t == pp.t5[7]


def test_news_deadline_closes_at_bar_open():
    c = np.full(20, 1.1000)
    pp, P = make_pp(c, h=c + 0.0001, l=c - 0.0001)
    pa = PriceAccess(pp, P)
    T = int(pp.t5[5]) + 30
    dl = int(pp.t5[9])
    pp.bid5[9, 0] = 1.1003
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, 1.1000, 1.0990, 1.1016, dl, "news_close", "pessimistic")
    assert why == "news_close" and t == dl and px == 1.1003


def test_short_exits_on_ask_with_fixed_spread():
    c = np.full(20, 1.1000)
    pp, P = make_pp(c, h=c + 0.0001, l=c - 0.0001)
    pp.spread = 0.0001
    pa = PriceAccess(pp, P)
    T = int(pp.t5[5]) + 30
    pp.bid5[8, 1] = 1.10095          # bid high 1.10095 -> ask high 1.10105 >= SL 1.1010
    t, px, why, *_ = simulate_exit(pa, -1, 5, T, 1.1000, 1.1010, 1.0984, None, "", "pessimistic")
    assert why == "sl" and t == pp.t5[8]


def test_live_supertrend_matches_full_recalc_when_bar_completes():
    rng = np.random.default_rng(1)
    c = 1.1 + np.cumsum(rng.normal(0, 0.0002, 400))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + rng.uniform(0, 0.0002, 400)
    l = np.minimum(o, c) - rng.uniform(0, 0.0002, 400)
    st, d, ub, lb, a = ind.supertrend(h, l, c, 1.5, 3)
    for k in range(50, 399):
        v, di = ind.supertrend_live(h[k + 1], l[k + 1], c[k + 1], c[k], a[k], ub[k], lb[k], st[k], 1.5, 3)
        assert abs(v - st[k + 1]) < 1e-12 and di == d[k + 1]
    m, sg, hist, f, s = ind.macd(c, 12, 26, 9)
    for k in range(60, 399):
        assert abs(ind.macd_live(c[k + 1], f[k], s[k], sg[k], 12, 26, 9) - hist[k + 1]) < 1e-15


def test_no_lookahead_without_subminute_data():
    """Decision price at +30s must not depend on the forming bar's later prices."""
    c = np.full(40, 1.1000)
    pp, P = make_pp(c, h=c + 0.0001, l=c - 0.0001)
    pa = PriceAccess(pp, P)
    a = pa.snapshot(20, 30)
    pp.bid5[20, 3] = pp.c[20] = 1.0950       # the bar later collapses
    pp.bid5[20, 2] = pp.l[20] = 1.0940
    b = pa.snapshot(20, 30)
    assert a["px"] == b["px"] == pp.o[20] and a["ph"] == b["ph"] and a["pl"] == b["pl"]
    assert b["bid"] < a["bid"]                # only the modelled fill uses the bar's close


def test_against_mode_zero_bar_before_flip_stays_old_colour():
    # trade #3 shape: light red bars, a bar displaying -0.00000, then the first real green bar
    h = [-9e-5, -8e-5, -6.8e-5, -3.8e-5, -1.1e-6, 1.71e-5, 4.6e-6]
    hk, eff, adj = hooks_of(h, mode="against")
    assert not eff[4] and not adj[4]                # stays red
    assert 4 not in hk and hk[5] == (1, 4, True)    # hook on the first real green bar
    hk2, _, _ = hooks_of(h, mode="literal")
    assert 4 in hk2                                 # the other reading puts the hook on the 0-bar


def test_against_mode_zero_bar_painted_new_colour_moves_flip_later():
    hk, eff, adj = hooks_of([-9e-5, -7e-5, -5e-5, -3e-5, 1e-6, 4e-5], mode="against")
    assert adj[4] and not eff[4] and 4 not in hk and hk[5][0] == 1


def test_bos_big_body_small_break_is_invalid():
    """11-pip body that only gets 0.5 pip past the EMA line -> invalid; measured EMA line -> body edge."""
    pp, P = make_pp([1.1] * 60)
    pp.ema[:] = 1.1000
    pp.c[39] = 1.0990
    pp.o[40], pp.c[40], pp.h[40], pp.l[40] = 1.09945, 1.10005, 1.10010, 1.09940   # body 6 pips, 0.5 above EMA
    pp.c[49] = 1.0990
    pp.o[50], pp.c[50], pp.h[50], pp.l[50] = 1.09900, 1.10010, 1.10012, 1.09898   # body 11 pips, 1.0 above EMA
    bos = find_bos(pp, P)
    assert not bos[40][3] and abs(bos[40][1] - 0.5) < 1e-6
    assert bos[50][3] and abs(bos[50][1] - 1.0) < 1e-6


def test_stop_moves_when_entry_candle_closes():
    c = np.full(20, 1.1000)
    pp, P = make_pp(c, h=c + 0.0001, l=c - 0.0001)
    pa = PriceAccess(pp, P)
    T = int(pp.t5[5]) + 30
    adj = int(pp.t5[6])
    # entry candle dips to 1.0994: below the final SL (1.0995) but above the provisional SL (1.0990)
    pp.bid5[5, 2] = 1.0994
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, 1.1000, 1.0990, 1.1016, None, "", "pessimistic", adj, 1.0995, 1.1008)
    assert why == "tp" or why == "end_of_data"
    # after the candle closes the tighter stop is live
    pp.bid5[8, 2] = 1.0994
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, 1.1000, 1.0990, 1.1016, None, "", "pessimistic", adj, 1.0995, 1.1008)
    assert why == "sl" and px == 1.0995 and t == pp.t5[8]
    # TP of the adjusted level
    pp.bid5[8, 2] = 1.0999
    pp.bid5[9, 1] = 1.1009
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, 1.1000, 1.0990, 1.1016, None, "", "pessimistic", adj, 1.0995, 1.1008)
    assert why == "tp" and px == 1.1008 and t == pp.t5[9]


def test_macd_critical_price_is_exact():
    rng = np.random.default_rng(3)
    for _ in range(200):
        F, S, G = 1.1 + rng.normal(0, 1e-3, 3)
        ref = rng.normal(0, 5e-5)
        p = ind.macd_critical_price(ref, F, S, G, 12, 26, 9)
        assert abs(ind.macd_live(p, F, S, G, 12, 26, 9) - ref) < 1e-12
        assert ind.macd_live(p + 1e-6, F, S, G, 12, 26, 9) > ref > ind.macd_live(p - 1e-6, F, S, G, 12, 26, 9)


def test_30s_check_decided_from_price_range():
    from server.engine.strategy import momentum_check
    c = 1.1 + np.cumsum(np.r_[np.full(60, -0.00005), np.full(20, 0.0001)])
    pp, P = make_pp(c)
    C = 75
    crit = ind.macd_critical_price(pp.hist[C], pp.fast[C], pp.slow[C], pp.sig[C], 12, 26, 9)
    snap = {"tier": "1m", "px": crit + 1e-4, "lo": crit + 1e-5, "hi": crit + 3e-4}
    assert momentum_check(pp, P, 1, C, snap, [pp.hist[C]], C + 1)[0] == "pass"          # whole minute above
    snap = {"tier": "1m", "px": crit - 1e-4, "lo": crit - 3e-4, "hi": crit - 1e-5}
    assert momentum_check(pp, P, 1, C, snap, [pp.hist[C]], C + 1)[0] == "fail"          # whole minute below
    snap = {"tier": "1m", "px": crit, "lo": crit - 1e-4, "hi": crit + 1e-4}
    assert momentum_check(pp, P, 1, C, snap, [pp.hist[C]], C + 1)[0] == "ambiguous_pass"
    P2 = dict(P, momentum_no5s="range_reject")
    assert momentum_check(pp, P2, 1, C, snap, [pp.hist[C]], C + 1)[0] == "ambiguous_fail"
    snap = {"tier": "S5", "px": crit - 1e-6, "lo": 0, "hi": 9}
    assert momentum_check(pp, P, 1, C, snap, [pp.hist[C]], C + 1)[0] == "fail"          # 5s data is exact


def test_break_even():
    c = np.full(20, 1.1000)
    pp, P = make_pp(c, h=c + 0.0001, l=c - 0.0001)
    pa = PriceAccess(pp, P)
    T = int(pp.t5[5]) + 30
    be = {"pct": 50.0, "off": 0.0}
    E, SL, TP = 1.0998, 1.0988, 1.1014          # TP 16 pips away -> trigger at +8 pips = 1.1006
    # bar 7 reaches the trigger, bar 9 comes back to entry -> break-even exit at entry
    pp.bid5[7, 1] = 1.1006
    pp.bid5[9, 2] = 1.0997
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, E, SL, TP, None, "", "pessimistic", be=be)
    assert why == "be" and px == E and t == pp.t5[9]
    # 7.9 pips is not enough -> no break-even, later stopped at the original SL
    pp.bid5[7, 1] = 1.10059
    pp.bid5[9, 2] = 1.0987
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, E, SL, TP, None, "", "pessimistic", be=be)
    assert why == "sl" and px == SL
    # break-even is never loosened when the stop is adjusted at the entry candle's close
    pp.bid5[7, 1] = 1.1001
    pp.bid5[5, 3] = 1.1007                    # entry candle closes beyond the trigger
    pp.bid5[9, 2] = 1.0997
    t, px, why, *_ = simulate_exit(pa, 1, 5, T, E, SL, TP, None, "", "pessimistic", int(pp.t5[6]), 1.0993, 1.1006, be)
    assert why == "be" and px == E and t == pp.t5[9]
