"""Backtest runner: candidates -> filters -> 30s snapshots -> entries -> exits -> P&L."""
from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pandas as pd

from ..data.store import PairData
from . import indicators as ind
from .metrics import compute_metrics
from .strategy import (REASONS, PriceAccess, Prepared, build_candidates, build_events, gate, momentum_check,
                       news_times, pine_indicator_signals, prepare, session_table, simulate_exit)


def _iso(t: int | float | None) -> str | None:
    if t is None:
        return None
    return pd.Timestamp(int(t), unit="s", tz="UTC").isoformat()


class RateBook:
    """USD conversion of quote-currency P&L using the loaded USDJPY / USDCHF series."""

    def __init__(self, prepared: dict[str, Prepared]):
        self.series = {}
        for p in ("USDJPY", "USDCHF"):
            if p in prepared:
                self.series[p] = (prepared[p].t5, prepared[p].bid5[:, 3])

    def to_usd(self, pair: str, amount_quote: float, t: int, exit_px: float) -> float:
        q = pair[3:]
        if q == "USD":
            return amount_quote
        if pair[:3] == "USD":
            return amount_quote / exit_px
        conv = "USD" + q
        if conv in self.series:
            ts, cl = self.series[conv]
            i = max(int(np.searchsorted(ts, t, side="right")) - 1, 0)
            return amount_quote / cl[i]
        raise RuntimeError(f"No USD{q} series loaded to convert {pair} P&L")


def run_backtest(pdatas: dict[str, PairData], events: pd.DataFrame, P: dict, start: pd.Timestamp,
                 end: pd.Timestamp, s5cache=None, oanda_client=None, rate_pdatas: dict | None = None) -> dict:
    t_start, t_end = int(start.timestamp()), int(end.timestamp())
    delay = int(P["entry_delay_sec"])
    units = float(P["lots"]) * 100000
    prepared = {p: prepare(d, P) for p, d in pdatas.items()}
    rate_prepared = dict(prepared)
    for p, d in (rate_pdatas or {}).items():
        if p not in rate_prepared:
            rate_prepared[p] = prepare(d, P)
    rates = RateBook(rate_prepared)
    use_s5 = s5cache is not None and oanda_client is not None

    notes, funnel, event_stats = [], Counter(), {}
    rejected, pine_marks, all_cands, adx_checks = [], {}, [], []
    ctx = {}
    for pair, pp in prepared.items():
        notes.extend(pp.notes)
        eff, adj, hooks, bos, flips = build_events(pp, P)
        tab = session_table(pp, P)
        ntimes, nrows = news_times(events, pair, P)
        pa = PriceAccess(pp, P, s5cache if use_s5 else None, oanda_client if use_s5 else None)
        ctx[pair] = {"pp": pp, "eff": eff, "adj": adj, "hooks": hooks, "bos": bos, "flips": flips,
                     "tab": tab, "ntimes": ntimes, "nrows": nrows, "pa": pa}
        in_test = (pp.t5 >= t_start) & (pp.t5 < t_end)
        rng = np.nonzero(in_test)[0]
        lo_i, hi_i = (int(rng[0]), int(rng[-1])) if len(rng) else (0, -1)
        within = lambda i: lo_i <= i <= hi_i  # noqa: E731
        event_stats[pair] = {
            "bars_5m": int(len(rng)),
            "st_flips": sum(1 for i in flips if within(i)),
            "bos_total": sum(1 for i in bos if within(i)),
            "bos_valid": sum(1 for i, b in bos.items() if within(i) and b[3]),
            "bos_buffer_rejected": sum(1 for i, b in bos.items() if within(i) and "body" in b[4]),
            "bos_range_rejected": sum(1 for i, b in bos.items() if within(i) and "range" in b[4]),
            "hooks_total": sum(1 for i in hooks if within(i)),
            "hooks_valid": sum(1 for i, h in hooks.items() if within(i) and h[2]),
            "hooks_zero_adjusted": int(sum(1 for i in hooks if within(i) and adj[max(i - 1, 0):i + 2].any())),
            "sessions_confirmed": sum(1 for (dte, s), v in tab.items() if v[1] and
                                      t_start <= v[3] < t_end),
            "sessions_checked": sum(1 for (dte, s), v in tab.items() if t_start <= v[3] < t_end
                                    and not math.isnan(v[0])),
        }
        for (dte, sess), (val, ok, bar_t, ct) in tab.items():
            if t_start <= ct < t_end:
                adx_checks.append({"date": str(dte), "pair": pair, "session": f"S{sess}", "check_time": _iso(ct),
                                   "adx": None if math.isnan(val) else round(val, 2), "green": bool(ok),
                                   "adx_bar": _iso(bar_t)})
        i1, i2 = pine_indicator_signals(pp, P, tab, ntimes)
        pine_marks[pair] = {"ind1": [(int(pp.t5[i]), d) for i, d in i1 if within(i)],
                            "ind2": [(int(pp.t5[i]), d) for i, d in i2 if within(i)]}
        for c in build_candidates(pp, P, hooks, bos, flips):
            k = c["C"] + 1
            T = int(pp.t5[k]) + delay
            if not (t_start <= T < t_end):
                continue
            c.update(pair=pair, k=k, T=T)
            all_cands.append(c)

    def reject(c, code, **extra):
        funnel[code] += 1
        pp = ctx[c["pair"]]["pp"]
        rejected.append({"pair": c["pair"], "dir": "BUY" if c["d"] == 1 else "SELL", "signal_bar": _iso(pp.t5[c["C"]]),
                         "entry_time": _iso(c["T"]), "reason": REASONS[code], "code": code,
                         "hook_bar": _iso(pp.t5[c["H"]]), "st_bar": _iso(pp.t5[c["S"]]),
                         "bos_bar": _iso(pp.t5[c["B"]]), **extra})

    # ------------------------------------------------ cheap (bar-close) filters
    survivors = []
    nb, na = int(P["news_before_min"]) * 60, int(P["news_after_min"]) * 60
    for c in all_cands:
        x = ctx[c["pair"]]
        pp, d = x["pp"], c["d"]
        C, k, H, S, B = c["C"], c["k"], c["H"], c["S"], c["B"]
        if pp.t5[k] - pp.t5[C] != 300:
            reject(c, "gap")
            continue
        ok, sess, adxv = gate(pp, P, x["tab"], k)
        c["session"], c["adx"] = sess, adxv
        if not ok:
            reject(c, "session", session=sess, adx=None if adxv is None or (isinstance(adxv, float) and math.isnan(adxv)) else round(adxv, 2))
            continue
        nt = x["ntimes"]
        j = np.searchsorted(nt, c["T"] - na)
        if j < len(nt) and nt[j] <= c["T"] + nb:
            ev = x["nrows"][j]
            reject(c, "news", news=f'{ev["country"]} {ev["title"]} @ {ev["t"]:%Y-%m-%d %H:%M}Z')
            continue
        if P["news_block_setup_bars"]:
            first = min(S, B, H)
            j = np.searchsorted(nt, int(pp.t5[first]) - na)
            if j < len(nt) and nt[j] <= int(pp.t5[C]) + 300 + nb:
                reject(c, "news_setup")
                continue
        eff = x["eff"]
        if d == 1 and not all(eff[H:C + 1]) or d == -1 and any(eff[H:C + 1]):
            reject(c, "side")
            continue
        if P["require_monotonic"]:
            hs = pp.hist[H:C + 1]
            if len(hs) > 1 and not (np.all(np.diff(hs) > 0) if d == 1 else np.all(np.diff(hs) < 0)):
                reject(c, "monotonic")
                continue
        if any(pp.sdir[S:C + 1] != -d):
            reject(c, "st_state")
            continue
        survivors.append(c)

    # ------------------------------------------------ prefetch exact sub-minute data (OANDA S5)
    if use_s5 and survivors:
        blocks = []
        for c in survivors:
            pp = ctx[c["pair"]]["pp"]
            blocks.append((c["pair"], int(pp.t5[c["k"]])))
            if c["H"] + 1 <= c["C"]:
                blocks.append((c["pair"], int(pp.t5[c["H"] + 1])))
        try:
            s5cache.ensure(oanda_client, blocks)
        except Exception as e:  # network trouble -> fall back to 1m approximation, but say so
            notes.append(f"S5 prefetch failed ({e}); 1m approximation used where S5 missing.")

    # ------------------------------------------------ sequential execution
    survivors.sort(key=lambda c: (c["T"], c["pair"]))
    trades = []
    busy_until: dict[str, int] = {}
    open_exits: list[int] = []
    used: set = set()
    for c in survivors:
        x = ctx[c["pair"]]
        pp, pa, d = x["pp"], x["pa"], c["d"]
        C, k, H, S, B, T = c["C"], c["k"], c["H"], c["S"], c["B"], c["T"]
        if busy_until.get(c["pair"], -1) > T:
            reject(c, "busy")
            continue
        open_exits = [e for e in open_exits if e > T]
        if len(open_exits) >= int(P["max_open_total"]):
            reject(c, "max_open")
            continue
        keys = {(c["pair"], "H", H), (c["pair"], "S", S), (c["pair"], "B", B)}
        if keys & used:
            reject(c, "used")
            continue
        sgn = 1 if d == 1 else -1
        # --- MACD momentum at +delay. The forming bar's histogram is linear in price, so each check is
        # "was the price beyond a critical level at +delay"; see strategy.momentum_check.
        snapE = pa.snapshot(k, delay)
        histE = ind.macd_live(snapE["px"], pp.fast[C], pp.slow[C], pp.sig[C],
                              int(P["macd_fast"]), int(P["macd_slow"]), int(P["macd_signal"]))
        hook_next, hook_status, hook_crit = None, "-", None
        if P["hook_confirm"]:
            # 2-bar rule: the bar after the hook bar must be stronger than the hook bar
            if H + 1 <= C and P["hook_confirm_mode"] == "close":
                hook_next = float(pp.hist[H + 1])
                ok = sgn * (hook_next - pp.hist[H]) > 0 and sgn * hook_next > 0
                hook_status = "pass" if ok else "fail"
            else:
                sH = snapE if H + 1 == k else pa.snapshot(H + 1, delay)
                hook_status, hook_crit = momentum_check(pp, P, d, H, sH, [float(pp.hist[H]), 0.0], H + 1)
                hook_next = ind.macd_live(sH["px"], pp.fast[H], pp.slow[H], pp.sig[H],
                                          int(P["macd_fast"]), int(P["macd_slow"]), int(P["macd_signal"]))
            if hook_status in ("fail", "ambiguous_fail"):
                reject(c, "hook_confirm", hist_hook=float(pp.hist[H]), hist_next=float(hook_next),
                       crit=hook_crit, check=hook_status)
                continue
        # entry bar (+delay) must be stronger than BOTH the signal bar and the hook bar - a weaker
        # (light green / light red) entry bar means momentum is fading -> setup invalid
        entry_status, entry_crit = "-", None
        if P["entry_still_check"]:
            entry_status, entry_crit = momentum_check(pp, P, d, C, snapE, [float(pp.hist[C]), float(pp.hist[H]), 0.0], k)
            if entry_status in ("fail", "ambiguous_fail"):
                reject(c, "still", hist_signal=float(pp.hist[C]), hist_hook=float(pp.hist[H]), hist_entry=float(histE),
                       crit=entry_crit, check=entry_status)
                continue
        st_live, dir_live = ind.supertrend_live(snapE["ph"], snapE["pl"], snapE["px"], pp.c[C], pp.atr[C],
                                                pp.ub[C], pp.lb[C], pp.st[C], float(P["st_factor"]), int(P["st_atr"]))
        if dir_live != -d:
            reject(c, "st_live", st_live=float(st_live))
            continue
        tick = pp.tick
        rnd = lambda v: round(round(v / tick) * tick, 6)  # noqa: E731
        # avg_close (default): SL = average of the Supertrend of the signal candle and of the entry
        # candle as the chart shows it. The entry candle's value is final only when it closes, so until
        # then the SL sits on the signal candle's Supertrend, then moves to the average (no look-ahead).
        # big-candle rule: signal candle AND the candle before it both >= N pips (high-low) -> the average
        # would stretch SL/TP too far, so the SL goes on the entry candle's Supertrend instead
        big_pips = float(P["big_candle_pips"])
        rng_sig = round((pp.h[C] - pp.l[C]) / pp.pip, 6)
        rng_pre = round((pp.h[C - 1] - pp.l[C - 1]) / pp.pip, 6)
        big = big_pips > 0 and rng_sig >= big_pips and rng_pre >= big_pips
        if P["sl_mode"] == "avg_live":
            sl = rnd(st_live) if big else rnd((pp.st[C] + st_live) / 2)
        else:
            sl = rnd(pp.st[C])
        entry = snapE["ask"] if d == 1 else snapE["bid"]
        risk = sgn * (entry - sl)
        risk_pips = risk / pp.pip
        if risk <= 0:
            reject(c, "sl_invalid", entry=entry, sl=sl)
            continue
        if risk_pips < float(P["min_sl_pips"]):
            reject(c, "sl_min", sl_pips=round(risk_pips, 2))
            continue
        if float(P["max_sl_pips"]) > 0 and risk_pips > float(P["max_sl_pips"]):
            reject(c, "sl_max", sl_pips=round(risk_pips, 2))
            continue
        tp = rnd(entry + sgn * float(P["rr"]) * risk)
        adjust_at = sl2 = tp2 = None
        sl_note = ""
        sl_rule = ("entry-candle Supertrend (big candles)" if big else
                   {"avg_close": "average signal/entry candle Supertrend", "avg_live": "average signal/live entry Supertrend",
                    "signal_only": "signal-candle Supertrend"}[P["sl_mode"]])
        if P["sl_mode"] != "avg_live" and (big or P["sl_mode"] == "avg_close"):
            if pp.sdir[k] == -d:
                cand = rnd(pp.st[k]) if big else rnd((pp.st[C] + pp.st[k]) / 2)
                r2 = sgn * (entry - cand)
                if r2 > 0:
                    adjust_at, sl2, tp2 = int(pp.t5[k]) + 300, cand, rnd(entry + sgn * float(P["rr"]) * r2)
                else:
                    sl_note = "entry-candle Supertrend passed the entry price; signal-candle SL kept"
            else:
                sl_note = "Supertrend flipped on the entry candle; signal-candle SL kept"
        # --- forced-exit deadline
        deadline, why, why_detail = None, "", ""
        nt = x["ntimes"]
        cb = int(P["close_before_news_min"]) * 60
        if cb > 0 and len(nt):
            j = np.searchsorted(nt, T + cb, side="right")
            if j < len(nt):
                deadline, why = int(nt[j]) - cb, "news_close"
                ev = x["nrows"][j]
                why_detail = f'{ev["country"]} {ev["title"]}'
        if P["close_at_session_end"] and c["session"]:
            end_h = int(P["s1_end_hour"] if c["session"] == "S1" else P["s2_end_hour"])
            se = _local_end(pp.loc_date[k], end_h, P["tz"])
            if se > T and (deadline is None or se < deadline):
                deadline, why = se, "session_end"
        if P["close_before_weekend"]:
            we = _weekend_deadline(T, P)
            if we and (deadline is None or we < deadline):
                deadline, why = we, "weekend_close"
        be = ({"pct": float(P["be_trigger_pct"]), "off": float(P["be_offset_pips"]) * pp.pip}
              if P["be_enabled"] and 0 < float(P["be_trigger_pct"]) <= 100 else None)
        ext, exp, reason, mae, mfe, tiers = simulate_exit(pa, d, k, T, entry, sl, tp, deadline, why,
                                                          P["intrabar_tie"], adjust_at, sl2, tp2, be)
        sl_init, tp_init = sl, tp
        if adjust_at is not None:
            if ext < adjust_at:
                sl_note = (f"closed before the entry candle closed (signal-candle SL was active; "
                           f"would have moved to {sl2})")
            else:
                sl, tp = sl2, tp2
                risk = sgn * (entry - sl)
                risk_pips = risk / pp.pip
        pnl_q = sgn * (exp - entry) * units
        pnl = rates.to_usd(c["pair"], pnl_q, ext, exp)
        risk_usd = rates.to_usd(c["pair"], risk * units, T, entry)
        pnl -= float(P["commission_per_lot"]) * float(P["lots"])
        spread_cost = rates.to_usd(c["pair"], (snapE["ask"] - snapE["bid"]) * units, T, entry)
        busy_until[c["pair"]] = ext
        open_exits.append(ext)
        used |= keys
        hk = x["hooks"][H]
        bb = x["bos"][B]
        trades.append({
            "id": len(trades) + 1, "pair": c["pair"], "dir": "BUY" if d == 1 else "SELL",
            "session": c["session"], "adx": None if c["adx"] is None else round(float(c["adx"]), 2),
            "hook_bar": _iso(pp.t5[H]), "st_bar": _iso(pp.t5[S]), "bos_bar": _iso(pp.t5[B]),
            "signal_bar": _iso(pp.t5[C]), "entry_time": _iso(T), "entry": round(entry, 6),
            "sl": sl, "tp": tp, "sl_pips": round(risk_pips, 2), "tp_pips": round(risk_pips * float(P["rr"]), 2),
            "sl_initial": sl_init, "tp_initial": tp_init, "sl_moved_at": _iso(adjust_at), "sl_note": sl_note,
            "sl_rule": sl_rule, "range_signal_pips": round(rng_sig, 1), "range_prev_pips": round(rng_pre, 1),
            "hook_check": hook_status, "hook_crit": None if hook_crit is None else round(hook_crit, 6),
            "entry_check": entry_status, "entry_crit": None if entry_crit is None else round(entry_crit, 6),
            "hist_entry_close": float(pp.hist[k]),
            "faded_after_entry": bool(sgn * (pp.hist[k] - pp.hist[C]) <= 0),
            "st_signal": round(float(pp.st[C]), 6), "st_entry_live": round(float(st_live), 6),
            "st_entry_final": round(float(pp.st[k]), 6),
            "spread_pips": round((snapE["ask"] - snapE["bid"]) / pp.pip, 2),
            "hook_bars": int(hk[1]), "hook_zero_adjusted": bool(x["adj"][max(H - 1, 0):H + 2].any()),
            "hist_before_hook": float(pp.hist[H - 1]), "hist_hook": float(pp.hist[H]),
            "hist_next_30s": None if hook_next is None else float(hook_next),
            "hist_signal": float(pp.hist[C]), "hist_entry_30s": float(histE),
            "bos_pierce_pips": round(bb[1], 2), "bos_range_pips": round(bb[2], 2),
            "exit_time": _iso(ext), "exit": round(float(exp), 6), "exit_reason": reason,
            "exit_note": why_detail if reason == "news_close" else "",
            "pnl": round(pnl, 2), "r": round(pnl / risk_usd, 3) if risk_usd > 0 else 0.0,
            "risk_usd": round(risk_usd, 2), "spread_cost": round(spread_cost, 2),
            "pips": round(sgn * (exp - entry) / pp.pip, 1),
            "mae_pips": round(mae / pp.pip, 1), "mfe_pips": round(mfe / pp.pip, 1),
            "duration_min": round((ext - T) / 60, 1), "precision": "+".join(sorted(tiers)) or "-",
            "entry_ts": T, "exit_ts": ext,
        })

    # ------------------------------------------------ equity (mark-to-market on 5m closes)
    mtm = _mtm_curve(trades, ctx, rates, units, P, t_start, t_end)
    metrics = compute_metrics(trades, mtm.pop("_full", None), float(P["initial_balance"]), start, end, P["tz"])
    total_cands = len(all_cands)
    funnel_list = [{"code": k, "reason": REASONS[k], "count": v} for k, v in funnel.most_common()]
    ind_cmp = {p: {"indicator1_signals": len(v["ind1"]), "indicator2_signals": len(v["ind2"])}
               for p, v in pine_marks.items()}
    return {
        "trades": trades, "metrics": metrics, "equity": mtm, "rejected": rejected[-5000:],
        "funnel": {"candidates": total_cands, "entered": len(trades), "rejections": funnel_list},
        "event_stats": event_stats, "indicator_signals": ind_cmp, "notes": notes,
        "adx_checks": sorted(adx_checks, key=lambda x: (x["check_time"], x["pair"])),
        "pine_marks": {p: {k: [[t, d] for t, d in v[k]] for k in v} for p, v in pine_marks.items()},
    }


def _local_end(date, hour, tz):
    ts = pd.Timestamp(date) + pd.Timedelta(hours=hour)
    return int(ts.tz_localize(tz, ambiguous=False, nonexistent="shift_forward").timestamp())


def _weekend_deadline(T: int, P: dict) -> int | None:
    loc = pd.Timestamp(T, unit="s", tz="UTC").tz_convert(P["tz"])
    days = (4 - loc.weekday()) % 7
    fri = (loc.normalize() + pd.Timedelta(days=days)).tz_localize(None) + pd.Timedelta(hours=int(P["weekend_close_hour"]))
    ts = int(fri.tz_localize(P["tz"], ambiguous=False, nonexistent="shift_forward").timestamp())
    return ts if ts > T else ts + 7 * 86400


def _mtm_curve(trades, ctx, rates, units, P, t_start, t_end):
    """Equity marked to market at every 5m close (realized + open P&L)."""
    grids = [ctx[p]["pp"].t5 for p in ctx]
    grid = np.unique(np.concatenate(grids)) if grids else np.array([], dtype=np.int64)
    grid = grid[(grid >= t_start) & (grid < t_end)]
    bal0 = float(P["initial_balance"])
    if not len(grid):
        return {"t": [], "equity": [], "closed": [], "dd": []}
    tclose = grid + 300
    realized = np.zeros(len(grid))
    unreal = np.zeros(len(grid))
    for tr in trades:
        i = np.searchsorted(tclose, tr["exit_ts"], side="left")
        if i < len(grid):
            realized[i:] += tr["pnl"]
        pp = ctx[tr["pair"]]["pp"]
        sgn = 1 if tr["dir"] == "BUY" else -1
        a = np.searchsorted(tclose, tr["entry_ts"], side="right")
        b = np.searchsorted(tclose, tr["exit_ts"], side="left")
        for gi in range(a, min(b, len(grid))):
            j = np.searchsorted(pp.t5, grid[gi], side="right") - 1
            if j < 0:
                continue
            px = pp.bid5[j, 3] if sgn == 1 else (pp.ask5[j, 3] if pp.ask5 is not None else pp.bid5[j, 3] + pp.spread)
            unreal[gi] += rates.to_usd(tr["pair"], sgn * (px - tr["entry"]) * units, int(grid[gi]), px)
    eq = bal0 + realized + unreal
    closed = bal0 + realized
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100
    # thin out for the browser: keep every point when small, otherwise 1 per 30 min + all trade events
    step = 1 if len(grid) < 20000 else 6
    keep = np.zeros(len(grid), dtype=bool)
    keep[::step] = True
    keep[-1] = True
    for tr in trades:
        for ts in (tr["entry_ts"], tr["exit_ts"]):
            i = min(np.searchsorted(tclose, ts), len(grid) - 1)
            keep[i] = True
    idx = np.nonzero(keep)[0]
    return {"t": (tclose[idx]).tolist(), "equity": np.round(eq[idx], 2).tolist(),
            "closed": np.round(closed[idx], 2).tolist(), "dd": np.round(dd[idx], 3).tolist(),
            "_full": {"t": tclose.tolist(), "equity": eq.tolist()}}
