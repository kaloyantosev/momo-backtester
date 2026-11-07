# OANDA data: what is stored and how it was checked

Measured on 5 Nov 2026. Everything sits in `data_cache/oanda/<PAIR>/`.

## What is stored

| File | Content | From |
|---|---|---|
| `M1_YYYY-MM.parquet` | 1-minute candles: mid, bid and ask OHLC, tick volume | Jan 2005 |
| `M5_…`, `H1_…` | 5-minute and 1-hour candles built from M1 | Jan 2005 |
| `S5_YYYY-MM.parquet` | 5-second candles: mid, bid and ask OHLC | May 2002 to the last finished month, usable from Jan 2005 |

OANDA returns candles from May 2002, but 2002–2004 holds only about 26 flat quotes a month, so those months are
not usable. About 7.5–8.1 million M1 candles per pair remain, and 354 million S5 candles in all. The folder takes
3.5 GB: prices are stored as whole ticks in delta encoding, against 11.2 GB as plain floats. All 7,074 files were
read back identical before being replaced. The running month's S5 candles are fetched when a test needs them, so a
test reaching today needs the token.

To refresh or resume, from this folder:

```bash
../../.venv/bin/python -m server.data.oanda_sync m1
```

```bash
../../.venv/bin/python -m server.data.oanda_sync s5 2005-01
```

Finished months are skipped. The dashboard also fetches the running month when a test reaches it.

## Why mid candles are stored separately

TradingView's `OANDA:` charts plot OANDA's own mid candles. Averaging bid and ask does not reproduce them, because
bid and ask don't peak at the same moment.

| 16 Sep – 27 Oct 2026, against TradingView 5m bars | Highs that differ | Largest difference |
|---|---|---|
| (bid high + ask high) / 2 | 54–62 % | 3.75 pips |
| OANDA's mid candle | 0.0–0.1 % | 3 pips, on a few bars |

## Checks on the whole history

| Check | Result |
|---|---|
| M5 and H1 built from M1 vs OANDA's own M5 and H1, all 12 price columns and volume, 2005–2026 (about 9.8 M M5 and 830 k H1 bars) | No differences, no missing or extra bars |
| Ask below bid, high below open/close, low above open/close, high below low, mid outside bid/ask, empty values, on all M1 candles | 0 |
| S5 download, 1,572 months | 0 failed, none missing |
| S5 candles out of order, off the 5-second grid, outside their month, crossed, malformed or empty | 0 |
| S5 rolled up to M1 vs the stored M1, all 12 price columns, 47 million minutes | No differences, no M1 minute without S5. 7 minutes have S5 but no M1, all at a market close or open |

## Against TradingView (16 Sep – 27 Oct 2026, same engine and settings)

| | Agreement |
|---|---|
| Supertrend flips | 3,795 of 3,796 |
| Valid MACD hooks | 1,629 of 1,633 |
| Valid EMA breaks | 3,562 of 3,567 |
| 1H ADX session checks | 360 of 360, with no difference in value |
| Trades | 36 of 36 identical |

TradingView's 5m opens and closes differ on about 1 % of bars, highs and lows on about 0.05 %.

## Known limits of the data

* **News starts in January 2008.** TradingView's calendar has no earlier events, so 2005–2007 runs without the news
  rules. The dashboard says so in the run notes.
* **"Medium" news is not the same list every year.** Distinct release times for US/EU/CH/AU/JP rise from 805 in 2008
  to 1,764 in 2021 and about 1,500 now. 2020–2022 includes daily COVID counts, and later years list items such as
  USDA pork sales. Trades closed for news rise from 8 % in 2008 to about 20 % now, so part of that change comes
  from the list and not the market. News timing itself is right: NFP sits at 12:30/13:30 UTC across DST in every
  year checked, and EURUSD moves 2–9 times more in the release minute than 30 minutes earlier.
* **Weekend candles before 2014.** Up to 2012 about 1–2 % of M1 candles fall on the weekend, with stray quotes.
  They would appear on a TradingView chart too, so they are kept. 2013 has a few and from 2014 there are none.
* **Thin data before 2022, not only early on.** Flat M1 candles run at 2–30 % in 2005–2007, depending on the pair,
  against about 1 % today. Between 08:00 and 19:00 UTC, 5-second candles per hour (720 at most) run at about 580–710
  from 2022 and 320–620 in most earlier years. The thinnest is AUDUSD in 2019 at 184, where 87 % of minutes have a
  candle. 5-minute bars are at least 99 % present in every pair and year. A missing candle means OANDA recorded no
  quote, so nothing was lost in the download, but older highs and lows rest on fewer quotes.
* **Long gaps are rare.** Gaps of 30+ minutes between 08:00 and 19:00 UTC on weekdays: 0–8 per pair and year, none
  since 2022. The longest is 565 minutes (EURJPY, 2006).
* **Price spikes are kept.** M1 jumps of 50+ pips or ranges of 100+ pips happen 15–113 times per pair in 22 years.
  Most are real (2008, SNB 2015, Brexit, the Oct 2016 pound crash, Jan 2019), a few look like bad quotes (EURUSD
  2005-11-07 and 2008-04-13). Each is above the 12-pip setup limit, so it can only affect a trade already open.
* **Practice-account prices.** The candles come from the practice server. Mid matches TradingView, but live spreads
  have not been compared. Median EURUSD spread inside the trading window: 0.9–1.6 pips by year. GBPUSD was 4.0 in
  2005 and 1.6–2.0 since 2014.

## Engine checks on this data

| Check | Result |
|---|---|
| Look-ahead: delete everything after a cut-off and re-run, 5 cut-offs in 2026 | All 665 comparisons of closed trades unchanged, entries before each cut-off identical |
| Warm-up: 30 days vs 90 days vs a year before the start, 2024 | EMA, histogram, Supertrend and ADX identical |
| Independent walk through 5-second candles from entry to exit, Feb 2005 – Oct 2026 | 7,405 of 7,405 trades give the same exit reason, minute and price. On 653 forced exits the engine stamps the candle's open and the first quote comes seconds later, at the same price |
| P&L arithmetic (USD-quoted and USD-base pairs), SL/TP side, entry at +30 s, on the same trades | 0 errors |
| TP = 1.6 × SL, on the same trades | 1 trade off, from rounding on a 1.4-pip stop |
| Whole history with the network blocked | 0 network calls, 0 trades short of S5 data |
| Session times: 11:00 and 15:00 BGN, all years, across DST | Always 09:00 and 13:00 London, reading the candle that just closed |
| Faster indicator code vs the old code, 885 trades | Identical trades, rejections and event counts |

The whole-history walk found that exits outside the entry candle were priced from M1 candles, so a jump over a level
inside a minute was filled at the level. 726 of 6,750 trades had the wrong fill, by 0.1 pip on the median stop or
target, with extremes of 5.1 pips worse and 11 pips better. Any minute in which something happens now runs on its S5
candles, and the checks above are from after that fix.

Every open trade now closes at 22:00 BGN. If the market has already closed for a holiday, the trade exits at the next quote: this happened once in 7,405 trades, on 31 Dec 2015 (USDJPY, exit on 3 Jan 2016).

OANDA runs hold one calendar year in memory at a time. On the whole history, in both fill modes, trades, the rejection
funnel, event counts and every summary figure are identical to loading everything at once.

| All six pairs, measured on the running server | Time | Peak memory |
|---|---|---|
| One month | 0.4 s | 0.44 GB |
| 12 months | 2.2 s | 0.96 GB |
| 2005 to now | 42 s | 1.58 GB (7.41 GB before) |

Runs with the daily close switched off still load their whole range, because a trade can then stay open for weeks.

The indicators were checked against TradingView's own plots, see [CALIBRATION.md](CALIBRATION.md).
