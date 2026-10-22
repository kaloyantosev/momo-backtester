"""Performance metrics. Everything is computed from the actual trade list / MTM equity, no smoothing."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _streaks(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _group(trades: list[dict], key) -> list[dict]:
    groups: dict = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    out = []
    for k, ts in sorted(groups.items(), key=lambda x: str(x[0])):
        p = np.array([t["pnl"] for t in ts])
        gp, gl = p[p > 0].sum(), p[p < 0].sum()
        out.append({"key": k, "trades": len(ts), "wins": int((p > 0).sum()), "losses": int((p < 0).sum()),
                    "win_rate": round(float((p > 0).mean() * 100), 2) if len(p) else 0.0,
                    "net": round(float(p.sum()), 2), "pf": round(float(gp / -gl), 2) if gl < 0 else None,
                    "avg_r": round(float(np.mean([t["r"] for t in ts])), 3)})
    return out


def _f(x, nd=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def compute_metrics(trades: list[dict], full: dict | None, bal0: float, start: pd.Timestamp, end: pd.Timestamp,
                    tz: str = "Europe/Sofia") -> dict:
    trades = sorted(trades, key=lambda t: t["exit_ts"])
    n = len(trades)
    pnl = np.array([t["pnl"] for t in trades], dtype=float)
    rs = np.array([t["r"] for t in trades], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gp, gl = float(wins.sum()), float(losses.sum())
    net = float(pnl.sum())
    days = max((end - start).total_seconds() / 86400, 1e-9)

    # closed-trade equity
    eq_closed = bal0 + np.cumsum(pnl) if n else np.array([bal0])
    peak_c = np.maximum.accumulate(np.concatenate([[bal0], eq_closed]))
    dd_c = np.concatenate([[bal0], eq_closed]) - peak_c
    max_dd_closed = float(-dd_c.min()) if len(dd_c) else 0.0
    max_dd_closed_pct = float((-(dd_c / peak_c)).max() * 100) if len(dd_c) else 0.0

    # mark-to-market equity
    sharpe = sortino = None
    max_dd = max_dd_pct = 0.0
    dd_days = 0.0
    final = bal0 + net
    daily_rows = []
    if full and full["t"]:
        t = np.array(full["t"], dtype=np.int64)
        e = np.array(full["equity"], dtype=float)
        peak = np.maximum.accumulate(np.maximum(e, bal0))
        dd = e - peak
        max_dd = float(-dd.min())
        max_dd_pct = float((-(dd / peak)).max() * 100)
        # longest time under water (peak -> recovery or end)
        under = dd < -1e-9
        longest, start_i = 0, None
        for i, u in enumerate(under):
            if u and start_i is None:
                start_i = i
            if (not u or i == len(under) - 1) and start_i is not None:
                longest = max(longest, t[i] - t[start_i])
                start_i = None
        dd_days = longest / 86400
        s = pd.Series(e, index=pd.to_datetime(t, unit="s", utc=True))
        daily = s.resample("1D").last().ffill()
        daily = daily[daily.index.dayofweek < 5]
        prev = daily.shift(1).fillna(bal0)
        rets = (daily - prev) / prev
        if len(rets) > 2 and rets.std() > 0:
            sharpe = float(rets.mean() / rets.std() * math.sqrt(252))
            downside = rets[rets < 0]
            dstd = math.sqrt((downside ** 2).sum() / len(rets)) if len(downside) else 0
            sortino = float(rets.mean() / dstd * math.sqrt(252)) if dstd > 0 else None
        daily_rows = [{"d": d.strftime("%Y-%m-%d"), "ret": round(float(r) * 100, 4)} for d, r in rets.items()]
    cagr = (final / bal0) ** (365.25 / days) - 1 if final > 0 else -1.0

    # streaks
    flags_w = [p > 0 for p in pnl]
    flags_l = [p < 0 for p in pnl]
    durations = np.array([t["duration_min"] for t in trades], dtype=float)
    # exposure: union of in-market intervals / test length
    iv = sorted((t["entry_ts"], t["exit_ts"]) for t in trades)
    covered, cur_s, cur_e = 0, None, None
    for a, b in iv:
        if cur_e is None or a > cur_e:
            if cur_e is not None:
                covered += cur_e - cur_s
            cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    if cur_e is not None:
        covered += cur_e - cur_s

    # monthly table
    monthly = []
    if n:
        df = pd.DataFrame({"t": pd.to_datetime([t["exit_ts"] for t in trades], unit="s", utc=True), "pnl": pnl})
        g = df.groupby(df["t"].dt.strftime("%Y-%m"))["pnl"]
        bal = bal0
        for m, v in g.sum().items():
            monthly.append({"month": m, "net": round(float(v), 2), "pct": round(float(v / bal * 100), 2),
                            "trades": int(g.count()[m])})
            bal += v
    hist_bins = np.arange(-2.0, 2.2, 0.2)
    rhist = np.histogram(np.clip(rs, -1.99, 1.99), bins=hist_bins)[0].tolist() if n else []

    m = {
        "initial_balance": bal0, "final_balance": _f(final), "net_profit": _f(net),
        "net_profit_pct": _f(net / bal0 * 100), "gross_profit": _f(gp), "gross_loss": _f(gl),
        "profit_factor": _f(gp / -gl) if gl < 0 else None,
        "total_trades": n, "wins": int(len(wins)), "losses": int(len(losses)),
        "breakeven": int(n - len(wins) - len(losses)),
        "win_rate": _f(len(wins) / n * 100) if n else None,
        "avg_win": _f(wins.mean()) if len(wins) else None, "avg_loss": _f(losses.mean()) if len(losses) else None,
        "payoff_ratio": _f(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        "expectancy": _f(net / n) if n else None, "avg_r": _f(rs.mean(), 3) if n else None,
        "sqn": _f(math.sqrt(n) * rs.mean() / rs.std(ddof=1)) if n > 1 and rs.std(ddof=1) > 0 else None,
        "largest_win": _f(pnl.max()) if n else None, "largest_loss": _f(pnl.min()) if n else None,
        "max_consec_wins": _streaks(flags_w), "max_consec_losses": _streaks(flags_l),
        "max_dd": _f(max_dd), "max_dd_pct": _f(max_dd_pct),
        "max_dd_closed": _f(max_dd_closed), "max_dd_closed_pct": _f(max_dd_closed_pct),
        "max_dd_duration_days": _f(dd_days, 1),
        "recovery_factor": _f(net / max_dd) if max_dd > 0 else None,
        "sharpe": _f(sharpe), "sortino": _f(sortino), "cagr_pct": _f(cagr * 100),
        "calmar": _f(cagr * 100 / max_dd_pct) if max_dd_pct > 0 else None,
        "avg_duration_min": _f(durations.mean(), 1) if n else None,
        "median_duration_min": _f(float(np.median(durations)), 1) if n else None,
        "trades_per_week": _f(n / (days / 7)), "exposure_pct": _f(covered / (days * 86400) * 100),
        "total_spread_cost": _f(sum(t.get("spread_cost", 0) for t in trades)),
        "longs": int(sum(1 for t in trades if t["dir"] == "BUY")),
        "shorts": int(sum(1 for t in trades if t["dir"] == "SELL")),
        "test_days": _f(days, 1),
    }
    return {
        "summary": m,
        "by_pair": _group(trades, lambda t: t["pair"]),
        "by_direction": _group(trades, lambda t: t["dir"]),
        "by_session": _group(trades, lambda t: t["session"] or "-"),
        "by_exit": _group(trades, lambda t: t["exit_reason"]),
        "by_hour": _group(trades, lambda t: pd.Timestamp(t["entry_ts"], unit="s", tz="UTC")
                          .tz_convert(tz).strftime("%H:00")),
        "monthly": monthly,
        "r_hist": {"bins": [round(float(b), 1) for b in hist_bins[:-1]], "counts": rhist},
        "daily_returns": daily_rows,
    }
