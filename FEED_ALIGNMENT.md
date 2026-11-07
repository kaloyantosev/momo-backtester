# Dukascopy (forexsb.com) vs TradingView OANDA bars

Measured on 28 Oct 2026 with the data in `data_cache/`.

## Source

* forexsb.com's data app downloads `https://data.forexsb.com/datafeed/data/dukascopy/<PAIR><PERIOD>.lb.gz`.
  These are Dukascopy bars as 28-byte records: time, OHLC, volume, spread. The latest ≤ 200,000 bars
  are included.
* Downloaded for EURJPY, EURUSD, AUDUSD, GBPUSD, USDJPY and USDCHF:

  | Timeframe | Covers |
  |---|---|
  | 1m | 15 Apr 2026 → 27 Oct 2026 |
  | 5m | 22 Feb 2024 → 27 Oct 2026 |
  | 1H (built from 30m, as the app does) | Oct 2010 → 27 Oct 2026 |

* Decoder: `server/data/forexsb.py`. Data source in the dashboard: "Dukascopy via forexsb.com".

## Raw bars (5m, 13 Sep – 27 Oct 2026, ~9,200 bars per pair)

* Timestamps line up exactly: both are UTC bar-open times. A ±1h shift makes the match far worse.
* TradingView/OANDA prices are consistently higher than Dukascopy:

  | Pair | Mean offset (pips) |
  |---|---|
  | EURUSD | +0.2 |
  | USDJPY | +0.35 |
  | GBPUSD | +0.5 |
  | AUDUSD | +0.55 |
  | USDCHF | +0.6 |
  | EURJPY | +0.75 |

* Around that offset, the median close difference is 0.1–0.5 pip, the 90th percentile 0.3–0.8 pip
  and the 99th percentile 2–8 pips.
* High–low ranges match: the mean difference is ≤ 0.1 pip.
* A constant offset does not change EMA distances, Supertrend, MACD or ADX. Only the noise around it
  matters.

## Signals and trades (same engine and settings, 16 Sep – 27 Oct 2026)

| | Agreement (same bar, same direction) |
|---|---|
| Supertrend flips | 72 % |
| Valid EMA BOS | 76 % |
| Valid MACD hooks | 71 % |
| 1H ADX session checks (green/red) | 88 % (317 / 360) |

| | TradingView / OANDA | Dukascopy |
|---|---|---|
| Trades | 36 | 44 |
| Identical entries | 31 (86 % of OANDA trades) | |
| Same exit reason (of identical) | 29 | |
| Win rate / profit factor | 39 % / 0.97 | 32 % / 0.70 |
| Net | −$7 | −$94 |

## What this means

* Dukascopy data reproduces most OANDA trades. The rules sit right at the edge of sub-pip differences
  (ATR-3 Supertrend, 0.8-pip BOS, histogram values near 0), so roughly 1 in 4 single signals and some
  trades differ.
* Use it to judge the strategy **statistically** over years, not to reproduce individual trades from
  your TradingView chart.
* If results change a lot between the two feeds, the edge depends on sub-pip noise, which is itself a
  warning sign.

## First long run (default settings, 8 Mar 2024 – 28 Oct 2026)

| Period | Trades | Win rate | Profit factor | Net |
|---|---|---|---|---|
| In-sample, 8 Mar 2024 – 11 Jan 2026 | 609 | 36.6 % | 0.86 | −$630 |
| Out-of-sample, 12 Jan – 28 Oct 2026 | 276 | 37.3 % | 0.89 | −$204 |

Some trades' 30-second MACD checks could not be decided from bar data: 100 in-sample and 16
out-of-sample. If all of those had been rejected:

| Period | Profit factor | Net |
|---|---|---|
| In-sample | 1.02 | +$81 |
| Out-of-sample | 0.91 | −$155 |

So the 30 s checks matter. Only 5-second data settles them, and the strict version leans on prices
after the 30 s mark.

## OANDA v20 data (added 5 Nov 2026)

The backtester now uses OANDA data only. See [DATA_QUALITY.md](DATA_QUALITY.md). This file is kept as the record of the Dukascopy comparison.
