# Calibration: engine vs TradingView and the March 2026 trade review

Measured on 5 Nov 2026 on OANDA data with the rules as they stand.

## Indicators against TradingView's own plots

Read from the user's EMA + ST and MACD + ADX indicators on `OANDA:` charts (last 300 5m bars and 26 1H session checks, all six pairs).

| | Agreement |
|---|---|
| Supertrend flips | 126 of 126 |
| MACD histogram sign changes | 159 of 159 |
| EMA, Supertrend, histogram in sampled bars | Within 0.007 pip |
| 1H ADX at the session checks, green or red | 156 of 156. Largest value difference 0.81, otherwise at most 0.01 |

The code was also compared line by line with both Pine scripts. The indicators are not a source of difference.

## Trade review, March 2026

The user checked the March 2026 trades in the chart inspector. Each verdict was compared with the candles.

| Trade (BGN) | User's verdict | Finding | Change |
|---|---|---|---|
| EURJPY SELL 13.03 19:55:30 | Not valid: the -0.000 bar counts as green, 6 fading bars | Agreed | 0-bars next to a flip count as the opposite of the painted colour (`zero_mode: literal`). The same rule also fits AUDUSD 30.09, which `zero_is_red` was added for. |
| AUDUSD BUY 11.03 20:30:30 | Not valid: 1 light red bar, then 0.00000 | Agreed | Same rule |
| USDJPY SELL 27.03 13:30:30 | Not valid: 13:10 is the BOS and is under 0.8 pip | Agreed. The 13:20 close was 0.08 tick above the EMA and counted as a new cross | A close within `ema_on_line_ticks` of the EMA sits on the line |
| USDJPY SELL 30.03 18:35:30 | Should be TP | Mid low 159.388 passed the TP of 159.390. The ask low of 159.396 did not. With a constant spread it is a TP up to 1.2 pips and an SL from 1.3; OANDA's spread at entry was 1.5 | Fills default to the chart price with no spread, because the user fills at bid = ask on MT5, so this is a TP. Upcomers' own text says live market spreads, so `bid_ask` stays as a stress test. |
| USDCHF SELL 10.03 13:10:30 | Did it close above the SL? | No. Highest ask before the exit 0.77638, SL 0.77651. The exit marker was drawn above the candle, not at the price | Exit price line, dot at the exit price and exit price on the trade card |
| EURJPY BUY 25.03 11:55:30 | Why an EMA exit? | Correct: the entry candle closed 0.79 pip below the EMA, exit at the next open | None |

`ema_on_line_ticks` = 0.25 is set by two examples the user confirmed by zooming: USDJPY 27.03 (0.08 tick) did not pierce the EMA, USDJPY 01.04 18:15 (0.47 tick) did, so 18:20 is the BOS there. Whole-history results barely differ between 0, 0.25 and 0.5.

The EMA exit looks at the entry candle only: if it closes on the wrong side of the EMA, by any distance, the trade exits at the next open. The candle's colour and where it opened don't matter, and `ema_on_line_ticks` applies to the BOS only.

A London trade still open at the 15:00 check closes there if the US session is red. The 22:00 close applies only when the US session is green. On the whole history this is 288 trades, +$518 in total.
