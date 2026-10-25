# MOMO backtester — EMA + Supertrend + MACD hook + ADX sessions

A local server with a browser dashboard that backtests the strategy on the six OANDA pairs
(EURJPY, EURUSD, AUDUSD, GBPUSD, USDJPY, USDCHF). Prices come from TradingView's data feed
(`OANDA:<PAIR>` symbols) and news comes from TradingView's economic calendar. An optional OANDA API
token adds years of history and 5-second precision.

## How to Run Locally

### Windows
Double click `run_win.bat` or run in PowerShell / Command Prompt:
```cmd
run_win.bat
```

### Linux / Mac
```bash
./run.sh
```

Then open http://127.0.0.1:8000.

Run tests:
```bash
pytest tests
```

---

## Upload to GitHub & Deploy Online

> **Note:** Because this project has a Python backend (FastAPI, calculation engine, live websocket/API connections), it cannot run on **GitHub Pages** (which only serves static HTML). To host it online for free, push this repository to GitHub and connect it to a free cloud hosting service like **Render** or **Railway**.

### Step 1: Push to GitHub
1. Create a new empty repository on [GitHub](https://github.com/new) (e.g. `momo-backtester`).
2. Run the following commands in this directory:
```bash
git add .
git commit -m "Initial commit of MOMO Backtester"
git branch -M main
git remote add origin https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git
git push -u origin main
```

### Step 2: Deploy Online (e.g. on Render - Free)
1. Sign in to [Render.com](https://render.com) (free).
2. Click **New +** -> **Web Service**.
3. Connect your GitHub repository.
4. Render will automatically detect the settings from `render.yaml` / `Procfile`:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn server.main:app --host 0.0.0.0 --port $PORT`
5. Click **Create Web Service**. Within 2 minutes, your backtester is live at a public URL (e.g. `https://momo-backtester.onrender.com`)!

*(Alternative: You can also deploy with 1-click via Docker using the included `Dockerfile` on Railway, Fly.io, or any VPS).*


## Data sources and their limits

| Source | Needs | History | Precision |
|---|---|---|---|
| **TradingView** (default) | nothing | anonymous: 1m ≈ 1 week, 5m ≈ 4 weeks, 1H ≈ 1.5–2 years. A `sessionid` cookie from a paid plan raises this. | 1m where available, otherwise 5m. There is no ask price, so a fixed spread is added (Costs). |
| **OANDA v20** | free practice-account API token (Data connections) | years of 1m bid/ask | real bid/ask. 5-second candles for the +30 s entry and for any 1m bar where SL and TP are both touched. |
| TradingView economic calendar | nothing | full history | importance −1/0/1 = low/medium/high |

OANDA is the price provider behind TradingView's OANDA symbols. Use it for any serious in-sample or
out-of-sample test, because four weeks of 5m data is far too little.

**Deeper TradingView history.** Paste your TradingView `sessionid` cookie under *Data connections*
(never your password). Then use *Check what my session can see* and *Download history*. The
downloader pages backwards like scrolling the chart left, until the chosen date or your plan's limit.

Every download is cached in `data_cache/`. Repeated TradingView downloads are merged, so the local
history keeps growing if you run the tool regularly.

## How the written rules were turned into code

These are all adjustable in the sidebar. The defaults follow your text. Where your text and the
indicators disagree, your text wins.

* **ADX sessions (Bulgarian time, BGN = London + 2h).** At 11:00 and 15:00 BGN the ADX(10) of the
  **last completed** 1H candle is read, so the 11:00 check uses the 10:00–11:00 candle. All times in
  the dashboard are BGN. ADX is computed with the indicator's own
  non-standard `calc_adx`. The pair is green when 25 ≤ ADX ≤ 40. Green at 11:00 opens 11:00–15:00 BGN;
  green at 15:00 opens 15:00–21:00 BGN (21:00 BGN = 19:00 London).
* **EMA BOS.** Only the candle whose close crosses EMA(20) is measured. Its **body** top (bottom
  for sells) must be ≥ 0.8 pip beyond the EMA; wicks don't count. If that candle fails, a later candle
  can't turn it into a BOS. The BOS candle's high–low (wick to wick) must be ≤ 12 pips. Indicator 1's
  looser definition (close crosses EMA ± 0.8 pip, possibly a later candle) is an option.
* **Supertrend(3, 1.5).** The BOS must come on the flip candle or the next one. Indicator 1 instead
  accepts the cross up to 2 bars *before* the flip.
* **MACD hook.** The histogram changes colour after 2–5 receding (light) bars. The minimum of 2 comes
  from indicator 2.
  * **Zero rule.** A "0 bar" is one that *displays* as 0 at the chart's precision. On JPY pairs that
    is 0.000 (|hist| < 0.0005; from 0.0005 it shows 0.001 and is a real bar). On the other pairs it is
    0.00000. A 0 bar right before, on, or right after the colour change never counts as the new
    colour. So the hook
    bar must have a real value, and a 0-bar right after the hook breaks it. Another option reads it
    as the opposite of the colour TradingView paints.
  * **2-bar rule.** The hook bar and the bar after it must both rise (buys) or both fall (sells). The
    second bar is measured 30 s after it opens.
  * **No fading momentum.** Every bar from the hook to the signal bar must be stronger than the one
    before it. A light green or light red bar in between rejects the setup.
* **Confluence window.**
  * If the hook comes first, the ST flip and the BOS must both be complete within the hook bar + 2. A
    4th bar or later is discarded.
  * If EMA/ST come first, the hook must come at most 2 bars after the later of the two.
  * The histogram must stay on its new side, and Supertrend must keep its direction until entry.
* **Entry.** A market order 30 s after the next candle opens, only if that bar's histogram,
  recomputed with the price at that second, is above **both** the previous bar and the hook bar
  (below for sells). A light green / light red entry bar invalidates the setup. Buys fill at the
  ask, sells at the bid.
* **Blocked entries.** No entry within ±10 min of medium/high news (TradingView calendar) for the
  countries US, EU, CH, AU, JP. By default only the pair's own currencies count, as in indicator 1.
  GB is not in your list, so GBPUSD is only filtered by US news.
* **Big candles.** If the signal candle and the candle before it are both ≥ 9 pips (high–low), the
  SL goes on the **entry candle's** Supertrend instead of the average. It is set when the entry
  candle closes.
* **Break-even** (optional, on the Overview tab). Once price has covered X % of the way to TP, the SL
  moves to entry (+ optional offset). It takes effect from the next bar.
* **SL.** The middle (average) of the Supertrend value of the signal candle and of the entry
  candle, as drawn on the chart. If the line is flat, the SL sits on the line. The entry candle's
  value is only final when that candle closes. Until then the SL sits on the signal candle's
  Supertrend; at the close it moves to the average, and TP is re-set to 1.6 × the new distance.
  No slippage is applied.
* **TP.** 1.6 × the SL distance. The position is 0.2 lots. One position per pair.
* **News close.** Open trades are closed at market 10 min before relevant news.

## How the 30-second MACD checks are decided

The forming bar's histogram is a linear, increasing function of the current price. So "is the bar at
+30 s stronger than the previous bar and the hook bar" is the same question as "was the price above
(buys) / below (sells) a critical level at +30 s". The trade details show that level.

* With OANDA 5-second data the check is exact.
* With bar data, the price at +30 s is somewhere inside the 1m bar (or the 5m bar if there is no 1m
  data). If that whole range is on one side of the level, the answer is certain.
* If the range crosses the level, the check is undecidable. By default those setups are accepted,
  and the dashboard shows how the results change if they had all failed.

## No look-ahead and pessimistic defaults

* Signals use closed candles only.
* With 5-second data the fill is the next tick after +30 s. Without it, the fill is interpolated
  inside the bar. TradingView refuses second-level bars to anonymous users
  (`seconds_not_entitled`).
* The option "bar's final value" judges the 30 s checks by the candle as it finally closed (what you
  see on the chart afterwards). That peeks ahead, and the dashboard flags it.
* SL and TP touched in the same bar without finer data: SL first. A gap through a level fills at the
  gap price.
* Equity is marked to market on every 5m close. Drawdown and Sharpe use that curve, not just closed
  trades.
* Swaps and financing are not modelled. JPY and CHF P&L is converted to USD at the exit time.

## When your TradingView indicators print a signal

* **Indicator 1** prints when:
  * Supertrend flips, and
  * the close crossed EMA ± 0.8 pip on the same bar or up to 2 bars before the flip, and
  * the bar opens between 09:00 and 19:00 London, and
  * the bar is not within ±10 min of the news times typed into its inputs.

  It has no 12-pip filter, no ADX and no MACD.
* **Indicator 2** prints when the histogram crosses 0 after 2–5 receding bars inside a confirmed
  session.
  * Its defaults are Europe/Sofia 11/15/22, which is London 09:00 / **13:00** / 20:00. Your rules
    say 09:00 / 15:00 / 19:00, so change the indicator inputs to match.
  * It has no 2-bar or 30 s check, no zero rule and no EMA/ST confluence.
  * On a 1H chart the label shows the ADX of the check-hour candle, which finishes an hour later.

The dashboard reproduces both indicators bar for bar ("Ind-1/Ind-2 signals"), so you can see how many
of their marks would really qualify as trades.

## Layout

```
server/data/tradingview.py   TradingView websocket client (bars, built-in studies)
server/data/oanda.py         OANDA v20 client (M1 / S5, bid+ask)
server/data/calendar.py      TradingView economic calendar
server/data/store.py         download + cache + resampling
server/engine/indicators.py  Pine-exact EMA/RMA/ATR/Supertrend/MACD/custom ADX (+ live forming-bar versions)
server/engine/strategy.py    events, confluence, sessions, news, sub-minute prices, exit simulation
server/engine/backtest.py    the run loop, P&L, mark-to-market equity, audit trail
server/engine/metrics.py     performance metrics
web/                         dashboard (TradingView Lightweight Charts)
tests/                       rule-level tests
```
