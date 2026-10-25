/* MOMO backtester dashboard */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const LS = { get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
             set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} } };
let TZ = "Europe/Sofia";                       // display timezone = session timezone setting
const TZN = { "Europe/Sofia": "BGN", "Europe/London": "London", "America/New_York": "New York" };
const tzName = () => TZN[TZ] || TZ;
function setTZ(tz) { TZ = tz || "Europe/Sofia"; $$(".tzname").forEach(e => e.textContent = tzName()); }
const state = { cfg: null, params: {}, data: null, sel: null, charts: {}, sort: {} };
const RUN_COLORS = ["#2962ff", "#ff9800", "#ab47bc", "#26c6da"];
const COUNTRIES = ["US", "EU", "CH", "AU", "JP", "GB", "CA", "NZ", "DE", "FR", "IT", "ES", "CN"];

const GROUPS = [
  { title: "Instruments & data", fields: [
    { key: "source", label: "Data source", type: "select", options: [["tradingview", "TradingView (OANDA:… symbols)"], ["oanda", "OANDA v20 API (needs token)"]],
      help: "TradingView anonymous history is short (5m ≈ 4 weeks, 1m ≈ 1 week). OANDA gives years of 1m bid/ask + 5s candles." },
    { key: "pairs", label: "Pairs", type: "pairs" },
    { key: "price_basis", label: "Signal price", type: "select", options: [["bid", "Bid (what TradingView plots)"], ["mid", "Mid (OANDA only)"]] },
    { key: "initial_balance", label: "Initial balance (USD)", type: "number", step: 100 },
    { key: "lots", label: "Lots per trade (1 lot = 100k units)", type: "number", step: 0.01 },
    { key: "max_open_total", label: "Max open positions across pairs", type: "number", step: 1, help: "One position per pair is always enforced." },
  ]},
  { title: "Costs", fields: [
    { key: "spread_pips", label: "Fixed spread in pips (TradingView data has no ask; OANDA uses the real bid/ask)", type: "spreads" },
    { key: "commission_per_lot", label: "Commission USD per lot, round turn", type: "number", step: 0.5 },
  ]},
  { title: "Sessions & ADX (1H)", fields: [
    { key: "tz", label: "Timezone for session hours and all displayed times", type: "select", options: [["Europe/Sofia", "Bulgaria (BGN, Europe/Sofia)"], ["Europe/London", "London"], ["America/New_York", "New York"]],
      help: "BGN is always London + 2h. The session hours below are read in this timezone, so if you switch it, shift the hours too." },
    { key: "use_adx_filter", label: "Require ADX check (green) for the session", type: "check" },
    { key: "s1_check_hour", label: "Session 1 check hour", type: "number", step: 1, half: true },
    { key: "s1_end_hour", label: "Session 1 end hour", type: "number", step: 1, half: true },
    { key: "s2_check_hour", label: "Session 2 check hour", type: "number", step: 1, half: true },
    { key: "s2_end_hour", label: "Session 2 end hour", type: "number", step: 1, half: true },
    { key: "adx_len", label: "ADX length", type: "number", step: 1 },
    { key: "adx_min", label: "ADX min (green from)", type: "number", step: 0.5, half: true },
    { key: "adx_max", label: "ADX max (green to)", type: "number", step: 0.5, half: true },
    { key: "adx_bar", label: "ADX value read at the check", type: "select",
      options: [["completed", "Last completed 1H candle (no look-ahead)"], ["check_bar", "Check-hour candle (as 1H-chart labels show — LOOK-AHEAD)"]] },
  ]},
  { title: "News filter (TradingView calendar)", fields: [
    { key: "use_news", label: "Use news blackout", type: "check" },
    { key: "news_countries", label: "Countries", type: "countries" },
    { key: "news_min_importance", label: "Minimum importance", type: "select", options: [["0", "Medium + High (2-3 stars)"], ["1", "High only (3 stars)"], ["-1", "All (incl. low)"]], num: true },
    { key: "news_before_min", label: "No entries: min before", type: "number", step: 1, half: true },
    { key: "news_after_min", label: "No entries: min after", type: "number", step: 1, half: true },
    { key: "news_scope", label: "Which news blocks a pair", type: "select", options: [["pair", "Only news of the pair's two currencies"], ["all", "Any listed news blocks every pair"]] },
    { key: "news_block_setup_bars", label: "Also reject if a setup bar is inside a news window", type: "check" },
    { key: "close_before_news_min", label: "Close open trade N min before news (0 = off)", type: "number", step: 1 },
  ]},
  { title: "EMA + Supertrend (5m)", fields: [
    { key: "ema_len", label: "EMA length", type: "number", step: 1, half: true },
    { key: "bos_buffer_pips", label: "BOS body beyond EMA ≥ (pips)", type: "number", step: 0.1, half: true },
    { key: "st_atr", label: "Supertrend ATR", type: "number", step: 1, half: true },
    { key: "st_factor", label: "Supertrend factor", type: "number", step: 0.1, half: true },
    { key: "bos_max_range_pips", label: "BOS candle max range, wick to wick (pips)", type: "number", step: 0.5 },
    { key: "bos_mode", label: "BOS definition", type: "select", options: [["cross_candle", "Only the candle that closes across the EMA; its body must clear EMA by the buffer"], ["indicator", "Close crosses EMA±buffer, may be a later candle (indicator 1)"]] },
    { key: "bos_after_st_max", label: "BOS up to N bars AFTER ST flip", type: "number", step: 1, half: true },
    { key: "bos_before_st_max", label: "BOS up to N bars BEFORE ST flip", type: "number", step: 1, half: true, help: "Your rule: same or next candle → after=1, before=0. Indicator 1 uses before=2, after=0." },
  ]},
  { title: "MACD hook (5m)", fields: [
    { key: "macd_fast", label: "Fast", type: "number", step: 1, third: true },
    { key: "macd_slow", label: "Slow", type: "number", step: 1, third: true },
    { key: "macd_signal", label: "Signal", type: "number", step: 1, third: true },
    { key: "hook_min", label: "Min receding bars", type: "number", step: 1, half: true },
    { key: "hook_max", label: "Max receding bars", type: "number", step: 1, half: true },
    { key: "zero_mode", label: "Bars that display 0", type: "select", options: [["against", "Next to a flip → never counts as the new colour (flip needs a real bar)"], ["literal", "Next to a flip → opposite of the colour TradingView paints"], ["raw", "Use TradingView colours as painted"], ["zero_is_red", "Always red"]] },
    { key: "zero_eps_ticks", label: "‘Displays as 0’ threshold (ticks)", type: "number", step: 0.1, help: "0.5 = the value rounds to 0 at the chart's precision: JPY pairs |v| < 0.0005 → 0.000 (0.0005+ shows 0.001 = valid bar); other pairs |v| < 0.000005 → 0.00000." },
    { key: "hook_confirm", label: "Bar after the hook must continue (2-bar rule)", type: "check" },
    { key: "hook_confirm_mode", label: "2-bar rule measured", type: "select", options: [["snapshot", "At +entry-delay seconds into that bar"], ["close", "At that bar's close"]] },
    { key: "macd_window_bars", label: "MACD first: others within N bars (incl. hook bar)", type: "number", step: 1 },
    { key: "macd_after_span_max", label: "EMA/ST first: hook within N bars after span", type: "number", step: 1 },
    { key: "require_monotonic", label: "No fading bar between hook and signal bar (every bar stronger than the one before)", type: "check" },
    { key: "entry_still_check", label: "Entry bar (+30s) histogram must exceed the previous bar AND the hook bar", type: "check" },
    { key: "momentum_no5s", label: "+30s MACD checks without 5-second data", type: "select", options: [
        ["range_accept", "Decide from the 1m/5m price range; undecidable → accept (no look-ahead)"],
        ["range_reject", "Decide from the price range; undecidable → reject (strict, flatters results)"],
        ["bar_close", "Use the bar's final value (LOOK-AHEAD)"], ["open", "Use the bar's open price only"]],
      help: "The forming bar's histogram is beyond the previous bar exactly when price is beyond a critical level. If the whole 1m bar (or 5m bar) around +30s is on one side of it, the answer is certain." },
  ]},
  { title: "Entry & exits", fields: [
    { key: "entry_delay_sec", label: "Entry delay after candle open (sec)", type: "number", step: 5 },
    { key: "sl_mode", label: "Stop loss", type: "select", options: [["avg_close", "Average of Supertrend on signal candle & entry candle (as on the chart)"], ["avg_live", "Average of ST signal candle & live ST of entry candle at +30s"], ["signal_only", "Supertrend of the signal candle"]],
      help: "The entry candle's Supertrend is final only when it closes: until then the SL sits on the signal candle's Supertrend, then moves to the average and TP is re-set to the ratio." },
    { key: "big_candle_pips", label: "Big candles: signal + previous candle both ≥ N pips → SL on entry-candle Supertrend (0 = off)", type: "number", step: 0.5 },
    { key: "rr", label: "Take profit = SL distance ×", type: "number", step: 0.1 },
    { key: "min_sl_pips", label: "Min SL (pips, 0 = off)", type: "number", step: 0.5, half: true },
    { key: "max_sl_pips", label: "Max SL (pips, 0 = off)", type: "number", step: 0.5, half: true },
    { key: "close_at_session_end", label: "Close open trades at session end", type: "check" },
    { key: "close_before_weekend", label: "Close open trades before the weekend", type: "check" },
    { key: "weekend_close_hour", label: "Friday close hour (session tz)", type: "number", step: 1 },
    { key: "intrabar_tie", label: "SL & TP in the same bar without finer data", type: "select", options: [["pessimistic", "Assume SL first (pessimistic)"], ["optimistic", "Assume TP first"]] },
    { key: "subminute_model", label: "Fill price at +delay without 5s data", type: "select", options: [["interp", "Interpolate open→close of the 1m/5m bar"], ["open", "Bar open"], ["close", "Bar close"]] },
  ]},
];

// ------------------------------------------------------------------ formatting
const chk = t => {
  const s = [t.hook_check, t.entry_check];
  if (s.some(x => String(x).startsWith("ambiguous"))) return `<span class="tag" style="color:var(--warn)" title="bar data cannot decide the +30s check">undecided</span>`;
  return `<span class="tag" style="color:var(--good)">${t.precision.includes("S5") ? "exact" : "certain"}</span>`;
};
const fmtT = (v, sec = false) => {
  if (v == null) return "–";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  const p = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: sec ? "2-digit" : undefined, hour12: false }).formatToParts(d);
  const g = t => p.find(x => x.type === t)?.value;
  return `${g("year")}-${g("month")}-${g("day")} ${g("hour")}:${g("minute")}${sec ? ":" + g("second") : ""}`;
};
const money = v => v == null ? "–" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (v, d = 2) => v == null ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 2) => v == null ? "–" : num(v, d) + "%";
const cls = v => v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const isoDate = d => {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
};

// ------------------------------------------------------------------ params UI
function buildParams() {
  const host = $("#paramGroups");
  host.innerHTML = "";
  const collapsed = LS.get("momo.collapsed", {});
  GROUPS.forEach(g => {
    const box = document.createElement("div");
    box.className = "group" + (collapsed[g.title] ? " collapsed" : "");
    box.innerHTML = `<h3>${g.title}</h3><div class="gbody"></div>`;
    $("h3", box).onclick = () => { box.classList.toggle("collapsed"); collapsed[g.title] = box.classList.contains("collapsed"); LS.set("momo.collapsed", collapsed); };
    const body = $(".gbody", box);
    let row = null, rowCount = 0;
    g.fields.forEach(f => {
      const el = fieldEl(f);
      if (f.half || f.third) {
        const n = f.third ? 3 : 2;
        if (!row || rowCount >= n) { row = document.createElement("div"); row.className = "row2"; row.style.gridTemplateColumns = `repeat(${n}, 1fr)`; body.appendChild(row); rowCount = 0; }
        row.appendChild(el); rowCount++;
      } else { row = null; body.appendChild(el); }
    });
    host.appendChild(box);
  });
}

function fieldEl(f) {
  const P = state.params;
  const wrap = document.createElement("div");
  wrap.className = "field";
  const help = f.help ? `<div class="help">${f.help}</div>` : "";
  if (f.type === "check") {
    wrap.innerHTML = `<label class="chk"><input type="checkbox"> ${f.label}</label>${help}`;
    const i = $("input", wrap); i.checked = !!P[f.key]; i.onchange = () => setP(f.key, i.checked);
  } else if (f.type === "select") {
    wrap.innerHTML = `<label>${f.label}<select>${f.options.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select></label>${help}`;
    const s = $("select", wrap); s.value = String(P[f.key]); s.onchange = () => setP(f.key, f.num ? Number(s.value) : s.value);
  } else if (f.type === "number") {
    wrap.innerHTML = `<label>${f.label}<input type="number" step="${f.step || 1}"></label>${help}`;
    const i = $("input", wrap); i.value = P[f.key]; i.onchange = () => setP(f.key, i.value === "" ? 0 : Number(i.value));
  } else if (f.type === "pairs") {
    wrap.innerHTML = `<label>${f.label}</label><div class="pairs">${state.cfg.pairs.map(p => `<label class="chk"><input type="checkbox" value="${p}">${p}</label>`).join("")}</div>`;
    $$("input", wrap).forEach(i => { i.checked = P.pairs.includes(i.value); i.onchange = () => setP("pairs", $$("input", wrap).filter(x => x.checked).map(x => x.value)); });
  } else if (f.type === "countries") {
    wrap.innerHTML = `<label>${f.label}</label><div class="pairs">${COUNTRIES.map(p => `<label class="chk"><input type="checkbox" value="${p}">${p}</label>`).join("")}</div>`;
    $$("input", wrap).forEach(i => { i.checked = P.news_countries.includes(i.value); i.onchange = () => setP("news_countries", $$("input", wrap).filter(x => x.checked).map(x => x.value)); });
  } else if (f.type === "spreads") {
    wrap.innerHTML = `<label>${f.label}</label><div class="spreads">${state.cfg.pairs.map(p => `<label>${p}<input type="number" step="0.1" data-p="${p}"></label>`).join("")}</div>`;
    $$("input", wrap).forEach(i => { i.value = P.spread_pips[i.dataset.p]; i.onchange = () => { P.spread_pips[i.dataset.p] = Number(i.value); saveParams(); }; });
  }
  return wrap;
}
function setP(k, v) { state.params[k] = v; saveParams(); if (k === "source") { updateBadge(); setDateBounds(); } if (k === "tz") setTZ(v); }
function syncBe() {
  $("#beOn").checked = !!state.params.be_enabled; $("#bePct").value = state.params.be_trigger_pct; $("#beOff").value = state.params.be_offset_pips;
}
function setDateBounds() {
  ["#isStart", "#isEnd", "#oosStart", "#oosEnd"].forEach(s => {
    $(s).removeAttribute("min");
    $(s).removeAttribute("max");
  });
}
function saveParams() { LS.set("momo.params", state.params); }
function updateBadge() {
  const s = state.params.source;
  const ok = s === "tradingview" || state.cfg.sources.oanda;
  $("#srcBadge").textContent = s === "oanda" ? `OANDA ${state.cfg.oanda_env}${ok ? "" : " — no token!"}` : `TradingView${state.cfg.tv_login ? " (logged in)" : " (anonymous)"}`;
  $("#srcBadge").style.color = ok ? "" : "var(--bad)";
}

// ------------------------------------------------------------------ periods
function periodsFromUI() {
  const ps = [{ name: "In-sample", start: $("#isStart").value, end: $("#isEnd").value }];
  if ($("#oosOn").checked) ps.push({ name: "Out-of-sample", start: $("#oosStart").value, end: $("#oosEnd").value });
  if (ps[0].start && ps[0].end) LS.set("momo.periods", { is: [ps[0].start, ps[0].end], oos: [$("#oosStart").value, $("#oosEnd").value], on: $("#oosOn").checked });
  return ps;
}
function setPeriods(a, b, split) {
  const t0 = a.getTime(), t1 = b.getTime();
  const m = new Date(t0 + (t1 - t0) * split);
  $("#isStart").value = isoDate(a); $("#isEnd").value = isoDate(m);
  const m2 = new Date(m.getTime() + 86400000);
  $("#oosStart").value = isoDate(m2); $("#oosEnd").value = isoDate(b);
  periodsFromUI();
}
function quickPeriod(q) {
  const today = new Date();
  if (q === "tvmax") {
    const fourWeeksAgo = new Date(today.getTime() - 28 * 86400000);
    $("#isStart").value = isoDate(fourWeeksAgo);
    $("#isEnd").value = isoDate(today);
    $("#oosOn").checked = false;
    $("#oosRow").style.display = "none";
    periodsFromUI();
  } else {
    const months = q === "12m" ? 12 : 24;
    const a = new Date(today); a.setMonth(a.getMonth() - months);
    setPeriods(a, today, 0.7);
  }
}

// ------------------------------------------------------------------ API
async function api(path, body) {
  // Vercel reserves /api/* paths, so call the prefix-less aliases the server also exposes.
  path = path.replace(/^\/api\//, "/");
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  let j;
  try {
    j = await r.json();
  } catch {
    const text = await r.text().catch(() => "");
    j = { detail: text.slice(0, 200) || `HTTP ${r.status} ${r.statusText || "Error"}` };
  }
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status} ${r.statusText || "Error"}`);
  return j;
}
function busy(on, msg) { $("#busy").hidden = !on; if (msg) $("#busyMsg").textContent = msg; }

async function runBacktest() {
  const periods = periodsFromUI();
  for (const p of periods) if (!p.start || !p.end) return alert("Set all period dates.");
  busy(true, state.params.source === "oanda" ? "Loading OANDA data (first run of a period downloads 1m history — can take a few minutes)…" : "Loading TradingView data & running…");
  try {
    const data = await api("/api/backtest", { params: state.params, periods, refresh: $("#refresh").checked });
    $("#refresh").checked = false;
    state.data = data;
    state.sel = null;
    setTZ(data.params.tz);
    renderAll();
    state.cfg = await api("/api/config");
  } catch (e) {
    $("#warnings").innerHTML = `<div class="warn err"><b>Run failed:</b> ${esc(e.message)}</div>`;
    showTab("overview");
  } finally { busy(false); }
}

// ------------------------------------------------------------------ rendering
function runs() { return state.data ? state.data.runs : []; }
function renderAll() {
  $("#emptyState").hidden = true;
  $("#overviewBody").hidden = false;
  renderWarnings();
  renderKpis();
  renderEquity();
  renderMetricsTable();
  fillRunSelects();
  renderTrades();
  renderBreakdown();
  renderRejections();
  renderRules();
  const pairs = state.data.params.pairs;
  $("#chPair").innerHTML = pairs.map(p => `<option>${p}</option>`).join("");
  $("#trPair").innerHTML = `<option value="">All pairs</option>` + pairs.map(p => `<option>${p}</option>`).join("");
  $("#rjPair").innerHTML = `<option value="">All pairs</option>` + pairs.map(p => `<option>${p}</option>`).join("");
}

function renderWarnings() {
  const w = new Set();
  runs().forEach(r => {
    const res = r.result;
    const grouped = {};
    res.notes.forEach(n => { const m = n.match(/^([A-Z]{6}): (.*)$/); if (m) (grouped[m[2]] = grouped[m[2]] || []).push(m[1]); else w.add(n); });
    Object.entries(grouped).forEach(([txt, ps]) => w.add(`${ps.length === state.data.params.pairs.length ? "All pairs" : ps.join(", ")}: ${txt}`));
    const n = res.metrics.summary.total_trades;
    if (n < 30) w.add(`${r.name}: only ${n} trades — far too few for reliable statistics (aim for 100+).`);
    Object.entries(res.coverage).forEach(([p, c]) => {
      if (!c[0]) w.add(`${r.name}: no ${p} data in ${r.start} → ${r.end}.`);
      else if (new Date(c[0]) - new Date(r.start + "T00:00:00Z") > 2 * 86400000) w.add(`${r.name}: ${p} data only from ${c[0].slice(0, 16)} (requested ${r.start}).`);
    });
    const tiers = {};
    res.trades.forEach(t => { tiers[t.precision] = (tiers[t.precision] || 0) + 1; });
    const coarse = res.trades.filter(t => !t.precision.includes("S5") && !t.precision.includes("1m")).length;
    if (coarse) w.add(`${r.name}: ${coarse}/${res.trades.length} trades had no 1m/5s data — their +30s MACD checks were decided from the 5m bar's price range, the fill price is interpolated inside the 5m bar and the SL/TP order is resolved pessimistically on 5m bars.`);
    const ties = res.trades.filter(t => t.precision.includes("tie")).length;
    if (ties) w.add(`${r.name}: ${ties} trades hit SL and TP inside one bar without finer data → resolved as ${state.data.params.intrabar_tie}.`);
  });
  if (state.data.params.adx_bar === "check_bar") w.add("ADX is read from the check-hour candle — this uses up to 1 hour of FUTURE data (look-ahead). Results are optimistic.");
  if (state.data.params.momentum_no5s === "bar_close") w.add("+30s MACD checks use the candle's final value — LOOK-AHEAD, results are flattered.");
  if (state.data.params.momentum_no5s === "range_reject") w.add("Undecidable +30s MACD checks are rejected — this uses prices after the +30s mark and tends to flatter results.");
  runs().forEach(r => {
    const res = r.result, amb = res.ambiguous_trades || 0;
    if (amb) {
      const st = res.strict_summary;
      w.add(`${r.name}: ${amb}/${res.trades.length} trades had a +30s MACD check that bar data cannot decide (the price range of that minute/bar crosses the critical level) — they were accepted.` +
        (st ? ` If they had all failed: ${st.total_trades} trades, win rate ${pct(st.win_rate)}, PF ${num(st.profit_factor)}, net ${money(st.net_profit)}. Only 5-second data (OANDA) settles this.` : ""));
    }
  });
  if (state.data.params.intrabar_tie === "optimistic") w.add("Intrabar ties resolved optimistically (TP first) — results are biased upward.");
  if (state.data.params.source === "tradingview") w.add("TradingView data has no ask price: a fixed spread per pair is added (Costs). OANDA source uses the real bid/ask.");
  $("#warnings").innerHTML = w.size ? `<div class="warn"><b>Read before trusting the numbers</b><ul>${[...w].map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
}

const KPI = [
  ["Net profit", "net_profit", money], ["Net %", "net_profit_pct", v => pct(v)], ["Trades", "total_trades", v => v],
  ["Win rate", "win_rate", v => pct(v)], ["Profit factor", "profit_factor", v => num(v)], ["Max DD", "max_dd_pct", v => pct(v)],
  ["Sharpe (daily)", "sharpe", v => num(v)], ["Avg R", "avg_r", v => num(v, 3)], ["Expectancy / trade", "expectancy", money],
  ["Avg win / avg loss", null, s => `${money(s.avg_win)} / ${money(s.avg_loss)}`],
  ["Max win / loss streak", null, s => `${s.max_consec_wins} / ${s.max_consec_losses}`], ["Trades / week", "trades_per_week", v => num(v, 1)],
];
function renderKpis() {
  const R = runs();
  $("#kpis").innerHTML = KPI.map(([label, key, f]) => {
    const vals = R.map((r, i) => {
      const s = r.result.metrics.summary;
      const v = key ? s[key] : null;
      const txt = key ? f(v) : f(s);
      const signed = ["net_profit", "net_profit_pct", "avg_r", "expectancy", "sharpe"].includes(key);
      return `<div class="kv"><i style="background:${RUN_COLORS[i]}"></i><span class="${signed ? cls(v) : ""}">${txt}</span></div>`;
    }).join("");
    return `<div class="kpi"><div class="k">${label}</div><div class="v">${vals}</div></div>`;
  }).join("");
}

function lwTimeOpts() {
  const fmt = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const fmtD = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, month: "short", day: "2-digit" });
  const fmtH = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  return {
    localization: { timeFormatter: t => fmt.format(new Date(t * 1000)) },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2a303b",
      tickMarkFormatter: (t, type) => (type <= 2 ? fmtD : fmtH).format(new Date(t * 1000)) },
  };
}
function baseChart(el, extra = {}) {
  const o = lwTimeOpts();
  return LightweightCharts.createChart(el, Object.assign({
    autoSize: true, layout: { background: { color: "#161a21" }, textColor: "#8a93a3", fontSize: 11 },
    grid: { vertLines: { color: "#1e232c" }, horzLines: { color: "#1e232c" } },
    rightPriceScale: { borderColor: "#2a303b" }, crosshair: { mode: 0 },
  }, o, extra));
}
function uniqSeries(ts, vs) {
  const out = []; let last = -Infinity;
  for (let i = 0; i < ts.length; i++) { if (ts[i] > last && vs[i] != null) { out.push({ time: ts[i], value: vs[i] }); last = ts[i]; } }
  return out;
}

function renderEquity() {
  ["eq", "dd"].forEach(k => { if (state.charts[k]) { state.charts[k].remove(); state.charts[k] = null; } });
  const eq = baseChart($("#eqChart")), dd = baseChart($("#ddChart"));
  state.charts.eq = eq; state.charts.dd = dd;
  $("#eqLegend").innerHTML = runs().map((r, i) => `<i style="background:${RUN_COLORS[i]}"></i>${esc(r.name)} (${r.start} → ${r.end})`).join("") + `<i style="background:#8a93a3"></i>closed-trade equity`;
  runs().forEach((r, i) => {
    const e = r.result.equity;
    eq.addLineSeries({ color: RUN_COLORS[i], lineWidth: 2, priceLineVisible: false }).setData(uniqSeries(e.t, e.equity));
    eq.addLineSeries({ color: "#8a93a3", lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false }).setData(uniqSeries(e.t, e.closed));
    dd.addAreaSeries({ lineColor: RUN_COLORS[i], topColor: "rgba(239,83,80,0.05)", bottomColor: "rgba(239,83,80,0.35)", lineWidth: 1, priceLineVisible: false, invertFilledArea: true })
      .setData(uniqSeries(e.t, e.dd));
  });
  eq.timeScale().fitContent(); dd.timeScale().fitContent();
}

const METRIC_ROWS = [
  ["Initial balance", "initial_balance", money], ["Final balance", "final_balance", money], ["Net profit", "net_profit", money],
  ["Net profit %", "net_profit_pct", pct], ["CAGR %", "cagr_pct", pct], ["Gross profit", "gross_profit", money], ["Gross loss", "gross_loss", money],
  ["Profit factor", "profit_factor", num], ["Total trades", "total_trades", v => v], ["Longs / shorts", null, s => `${s.longs} / ${s.shorts}`],
  ["Wins", "wins", v => v], ["Losses", "losses", v => v], ["Breakeven", "breakeven", v => v], ["Win rate", "win_rate", pct],
  ["Average win", "avg_win", money], ["Average loss", "avg_loss", money], ["Payoff ratio (avg win / avg loss)", "payoff_ratio", num],
  ["Expectancy per trade", "expectancy", money], ["Average R", "avg_r", v => num(v, 3)], ["SQN", "sqn", num],
  ["Largest win", "largest_win", money], ["Largest loss", "largest_loss", money],
  ["Max consecutive wins", "max_consec_wins", v => v], ["Max consecutive losses", "max_consec_losses", v => v],
  ["Max drawdown (mark-to-market)", null, s => `${money(s.max_dd)} (${pct(s.max_dd_pct)})`],
  ["Max drawdown (closed trades)", null, s => `${money(s.max_dd_closed)} (${pct(s.max_dd_closed_pct)})`],
  ["Longest drawdown (days)", "max_dd_duration_days", v => num(v, 1)], ["Recovery factor", "recovery_factor", num],
  ["Sharpe (daily, ×√252)", "sharpe", num], ["Sortino", "sortino", num], ["Calmar (CAGR / maxDD%)", "calmar", num],
  ["Avg trade duration (min)", "avg_duration_min", v => num(v, 1)], ["Median trade duration (min)", "median_duration_min", v => num(v, 1)],
  ["Trades per week", "trades_per_week", v => num(v, 1)], ["Time in market", "exposure_pct", pct],
  ["Spread paid (approx.)", "total_spread_cost", money], ["Test length (days)", "test_days", v => num(v, 1)],
];
function renderMetricsTable() {
  const R = runs();
  const head = `<tr><th class="l">Metric</th>${R.map((r, i) => `<th style="color:${RUN_COLORS[i]}">${esc(r.name)}<br><span class="hint">${r.start} → ${r.end}</span></th>`).join("")}</tr>`;
  const body = METRIC_ROWS.map(([lab, key, f]) => `<tr><td>${lab}</td>${R.map(r => { const s = r.result.metrics.summary; const v = key ? s[key] : null; return `<td class="${key && ["net_profit", "net_profit_pct", "expectancy", "avg_r", "cagr_pct"].includes(key) ? cls(v) : ""}">${key ? f(v) : f(s)}</td>`; }).join("")}</tr>`).join("");
  $("#metricsTable").innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

function fillRunSelects() {
  const opts = runs().map((r, i) => `<option value="${i}">${esc(r.name)} (${r.start} → ${r.end})</option>`).join("");
  $("#trRun").innerHTML = `<option value="all">All periods</option>` + opts;
  $("#bdRun").innerHTML = opts; $("#rjRun").innerHTML = opts;
  const exits = new Set(); runs().forEach(r => r.result.trades.forEach(t => exits.add(t.exit_reason)));
  $("#trExit").innerHTML = `<option value="">All exits</option>` + [...exits].map(x => `<option>${x}</option>`).join("");
}

// ------------------------------------------------------------------ trades (visual charts + key metrics)
const TRADES_PAGE = 10;
state.tradeCharts = [];

function tradeRows() {
  const sel = $("#trRun").value;
  let rows = [];
  runs().forEach((r, i) => { if (sel === "all" || String(i) === sel) r.result.trades.forEach(t => rows.push({ ...t, _run: r.name, _ri: i })); });
  const p = $("#trPair").value, d = $("#trDir").value, x = $("#trExit").value;
  rows = rows.filter(t => (!p || t.pair === p) && (!d || t.dir === d) && (!x || t.exit_reason === x));
  rows.sort((a, b) => a.entry_ts - b.entry_ts);
  return rows;
}

function disposeTradeCharts() {
  (state.tradeCharts || []).forEach(c => { try { c.remove(); } catch {} });
  state.tradeCharts = [];
  if (state.tcObserver) {
    state.tcObserver.disconnect();
    state.tcObserver = null;
  }
}

function tradeCardHtml(t, idx) {
  return `
  <div class="trade-card" id="tc-${idx}" data-idx="${idx}">
    <div class="tc-chart-col">
      <div class="pxwrap">
        <div class="tc-px"><div class="tc-loading">Loading chart…</div></div>
        <div class="legend-ov"></div>
      </div>
      <div class="tc-macd"></div>
    </div>
    <div class="tc-details-col">
      <div class="tc-header">
        <span class="tc-pair">${t.pair}</span>
        <span class="tc-dir ${t.dir}">${t.dir}</span>
      </div>
      <div class="tc-grid">
        <div class="tc-item full">
          <label>Entry time</label>
          <span>${fmtT(t.entry_time, true)}</span>
        </div>
        <div class="tc-item full">
          <label>Exit time</label>
          <span>${fmtT(t.exit_time, true)}</span>
        </div>
        <div class="tc-item">
          <label>Entry price</label>
          <span>${t.entry}</span>
        </div>
        <div class="tc-item">
          <label>SL pips</label>
          <span>${num(t.sl_pips, 1)} p</span>
        </div>
        <div class="tc-item">
          <label>Stop Loss (SL)</label>
          <span>${t.sl}</span>
        </div>
        <div class="tc-item">
          <label>Take Profit (TP)</label>
          <span>${t.tp}</span>
        </div>
        <div class="tc-item full">
          <label>Reason</label>
          <span><span class="tag ${t.exit_reason}">${t.exit_reason.toUpperCase()}</span></span>
        </div>
        <div class="tc-item highlight">
          <label>Pips</label>
          <span class="${cls(t.pips)}">${t.pips > 0 ? "+" : ""}${num(t.pips, 1)}</span>
        </div>
        <div class="tc-item highlight">
          <label>P&L</label>
          <span class="${cls(t.pnl)}">${money(t.pnl)}</span>
        </div>
      </div>
    </div>
  </div>`;
}

function renderTrades() {
  disposeTradeCharts();
  const rows = tradeRows();
  state.tradeRowsCache = rows;
  state.tcShown = 0;
  const tot = rows.reduce((a, t) => a + t.pnl, 0);
  $("#tradesSummary").innerHTML = `${rows.length} trades · Total P&L: <span class="${cls(tot)}">${money(tot)}</span>`;
  $("#tradesWrap").innerHTML = rows.length ? "" : `<div class="hint">No trades match the selected filters.</div>`;
  if (!rows.length) return;

  state.tcObserver = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      state.tcObserver.unobserve(e.target);
      loadSingleTradeChart(e.target);
    });
  }, { rootMargin: "400px 0px" });

  moreTrades();
}

function moreTrades() {
  const rows = state.tradeRowsCache || [];
  const from = state.tcShown, to = Math.min(rows.length, from + TRADES_PAGE);
  const oldBtn = $("#tradesMore");
  if (oldBtn) oldBtn.remove();

  for (let i = from; i < to; i++) {
    const t = rows[i];
    const div = document.createElement("div");
    div.innerHTML = tradeCardHtml(t, i);
    const card = div.firstElementChild;
    $("#tradesWrap").appendChild(card);
    if (state.tcObserver) state.tcObserver.observe(card);
  }
  state.tcShown = to;

  if (to < rows.length) {
    const btn = document.createElement("button");
    btn.id = "tradesMore";
    btn.className = "mini";
    btn.style.margin = "10px 0 20px";
    btn.textContent = `Show more trades (${rows.length - to} left)`;
    btn.onclick = moreTrades;
    $("#tradesWrap").appendChild(btn);
  }
}

async function loadSingleTradeChart(card) {
  const idx = +card.dataset.idx;
  const rows = state.tradeRowsCache || [];
  const t = rows[idx];
  if (!t || !card.isConnected) return;
  const pxEl = $(".tc-px", card);
  const mcEl = $(".tc-macd", card);
  const legendEl = $(".legend-ov", card);

  const durBars = Math.ceil((t.duration_min || 15) / 5);
  const after = Math.min(180, Math.max(35, durBars + 25));
  const before = 45;
  const [s, e] = periodBounds();
  const P = state.data ? state.data.params : state.params;

  try {
    const c = await api("/api/chart", { params: P, pair: t.pair, center: t.entry_time, start: s, end: e, before, after });
    pxEl.innerHTML = "";
    const res = plotChart(c, t.pair, t, { px: pxEl, macd: mcEl, legend: legendEl }, { show: new Set(["trade", "hook", "bos", "st"]), onlyTrade: true, fit: true });
    state.tradeCharts.push(res.px, res.mc);
  } catch (err) {
    pxEl.innerHTML = `<div class="tc-loading hint">Failed to load chart: ${esc(err.message)}</div>`;
  }
}

function exportCsv() {
  const rows = state.tradeRowsCache || [];
  if (!rows.length) return;
  const keys = Object.keys(rows[0]).filter(k => !k.startsWith("_") || k === "_run");
  const csv = [keys.join(",")].concat(rows.map(r => keys.map(k => JSON.stringify(r[k] ?? "")).join(","))).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "trades.csv"; a.click();
}

// ------------------------------------------------------------------ chart inspector
function periodBounds() {
  const R = runs();
  if (!R.length) return [$("#isStart").value, $("#oosOn").checked ? $("#oosEnd").value : $("#isEnd").value];
  return [R.map(r => r.start).sort()[0], R.map(r => r.end).sort().slice(-1)[0]];
}

function openChart(pair, centerIso, trade) {
  showTab("chart");
  $("#chPair").value = pair;
  const d = new Date(centerIso);
  const loc = fmtT(d.getTime() / 1000).replace(" ", "T");
  $("#chCenter").value = loc;
  loadChart(trade);
}

async function loadChart(trade) {
  const pair = $("#chPair").value;
  if (!pair || !$("#chCenter").value) return;
  const [d, tm] = $("#chCenter").value.split("T");
  const guess = new Date(`${d}T${tm}:00Z`);
  const off = new Date(guess.toLocaleString("en-US", { timeZone: TZ })) - new Date(guess.toLocaleString("en-US", { timeZone: "UTC" }));
  const center = new Date(guess.getTime() - off).toISOString();
  const [s, e] = periodBounds();
  busy(true, "Loading chart…");
  try {
    const c = await api("/api/chart", { params: state.data ? state.data.params : state.params, pair, center, start: s, end: e });
    drawChart(c, pair, trade || state.sel);
  } catch (err) { alert(err.message); } finally { busy(false); }
}

function drawChart(c, pair, trade) {
  ["px", "macd"].forEach(k => { if (state.charts[k]) { state.charts[k].remove(); state.charts[k] = null; } });
  const show = new Set($$(".mk").filter(x => x.checked).map(x => x.value));
  const r = plotChart(c, pair, trade, { px: $("#pxChart"), macd: $("#macdChart"), legend: $("#pxLegend") }, { show });
  state.charts.px = r.px; state.charts.macd = r.mc;
  const newsWin = (state.data ? state.data.params : state.params);
  $("#chInfo").textContent = `${pair} 5m · times ${tzName()} · grey = news blackout (${newsWin.news_before_min}/${newsWin.news_after_min} min) · green tint = ADX-confirmed session · H<n> = valid hook (n receding bars) · B = valid EMA BOS, B✗ = BOS candle too large (pips) · N = news (list below)`;
  $("#chNews").innerHTML = c.news.length ? c.news.map(n => `${fmtT(n.t)} — ${esc(n.title)} ${n.imp > 0 ? "(high)" : "(medium)"}`).join("<br>") : "No relevant news in this window.";
}

function plotChart(c, pair, trade, el, opt = {}) {
  const show = opt.show || new Set(["trade", "hook", "bos", "st"]);
  const px = baseChart(el.px, { rightPriceScale: { borderColor: "#2a303b", scaleMargins: { top: 0.08, bottom: 0.08 } } });
  const mc = baseChart(el.macd);
  const fmt = { type: "price", precision: c.digits, minMove: Math.pow(10, -c.digits) };

  const bg = px.addHistogramSeries({ priceScaleId: "bg", priceLineVisible: false, lastValueVisible: false });
  px.priceScale("bg").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
  const newsWin = (state.data ? state.data.params : state.params);
  const nb = (newsWin.news_before_min || 10) * 60, na = (newsWin.news_after_min || 10) * 60;
  bg.setData(c.t.map((t, i) => {
    const inNews = (c.news || []).some(n => t + 300 > n.t - nb && t <= n.t + na);
    return { time: t, value: 1, color: inNews ? "rgba(140,140,140,0.28)" : (c.gate && c.gate[i]) ? "rgba(38,166,154,0.07)" : "rgba(0,0,0,0)" };
  }));

  const candles = px.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false, priceFormat: fmt });
  candles.setData(c.t.map((t, i) => ({ time: t, open: c.o[i], high: c.h[i], low: c.l[i], close: c.c[i] })));
  px.addLineSeries({ color: "#2962ff", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, priceFormat: fmt })
    .setData(c.t.map((t, i) => c.ema[i] == null ? { time: t } : { time: t, value: c.ema[i] }));

  let run = null;
  const flush = () => { if (run && run.pts.length) px.addLineSeries({ color: run.up ? "#4caf50" : "#f23645", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: fmt }).setData(run.pts); };
  c.t.forEach((t, i) => {
    if (c.st[i] == null || c.dir[i] == null) { flush(); run = null; return; }
    const up = c.dir[i] < 0;
    if (!run || run.up !== up) { flush(); run = { up, pts: [] }; }
    run.pts.push({ time: t, value: c.st[i] });
  });
  flush();

  const marks = [];
  (c.marks || []).forEach(m => {
    const t = c.t[m.i];
    if (m.kind === "hook" && show.has("hook") && m.ok) marks.push({ time: t, position: m.d > 0 ? "belowBar" : "aboveBar", color: "#ffeb3b", shape: "circle", text: `H${m.k}` });
    if (m.kind === "bos" && show.has("bos")) marks.push({ time: t, position: m.d > 0 ? "belowBar" : "aboveBar", color: m.ok ? "#42a5f5" : "#5c6b80", shape: "square", text: m.ok ? `B${m.pierce.toFixed(1)}` : `B✗${m.pierce.toFixed(1)}` });
    if (m.kind === "st" && show.has("st")) marks.push({ time: t, position: m.d > 0 ? "belowBar" : "aboveBar", color: m.d > 0 ? "#26a69a" : "#ef5350", shape: m.d > 0 ? "arrowUp" : "arrowDown", text: "ST" });
    if (m.kind === "ind1" && show.has("ind1")) marks.push({ time: t, position: m.d > 0 ? "belowBar" : "aboveBar", color: "#ce93d8", shape: m.d > 0 ? "arrowUp" : "arrowDown", text: "I1" });
    if (m.kind === "ind2" && show.has("ind2")) marks.push({ time: t, position: m.d > 0 ? "belowBar" : "aboveBar", color: "#80deea", shape: m.d > 0 ? "arrowUp" : "arrowDown", text: "I2" });
  });
  (c.news || []).forEach(n => {
    const t = Math.floor(n.t / 300) * 300;
    if (t >= c.t[0] && t <= c.t[c.t.length - 1]) marks.push({ time: t, position: "aboveBar", color: "#b0b7c3", shape: "square", text: `N` });
  });

  if (show.has("trade")) {
    const list = opt.onlyTrade && trade ? [trade] : [].concat(...runs().map(r => r.result.trades.filter(x => x.pair === pair)));
    list.forEach(x => {
      const te = Math.floor(x.entry_ts / 300) * 300, tx = Math.floor(x.exit_ts / 300) * 300;
      if (te >= c.t[0] && te <= c.t[c.t.length - 1]) marks.push({ time: te, position: x.dir === "BUY" ? "belowBar" : "aboveBar", color: "#fff", shape: x.dir === "BUY" ? "arrowUp" : "arrowDown", text: `${x.dir} ${x.entry}` });
      if (tx >= c.t[0] && tx <= c.t[c.t.length - 1]) marks.push({ time: tx, position: "aboveBar", color: x.pnl >= 0 ? "#26a69a" : "#ef5350", shape: "circle", text: `${x.exit_reason.toUpperCase()} ${money(x.pnl)}` });
    });
  }
  marks.sort((a, b) => a.time - b.time);
  candles.setMarkers(marks);

  if (trade) {
    candles.createPriceLine({ price: trade.entry, color: "#ffffff", lineWidth: 1, lineStyle: 2, title: "Entry" });
    candles.createPriceLine({ price: trade.sl, color: "#ef5350", lineWidth: 1, lineStyle: 0, title: "SL" });
    candles.createPriceLine({ price: trade.tp, color: "#26a69a", lineWidth: 1, lineStyle: 0, title: "TP" });
  }

  const hs = mc.addHistogramSeries({ priceFormat: { type: "price", precision: c.digits, minMove: Math.pow(10, -c.digits) }, priceLineVisible: false });
  hs.setData(c.t.map((t, i) => c.hist[i] == null ? { time: t } : { time: t, value: c.hist[i], color: c.hist_color[i] }));
  mc.addLineSeries({ color: "#2962ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(c.t.map((t, i) => c.macd[i] == null ? { time: t } : { time: t, value: c.macd[i] }));
  mc.addLineSeries({ color: "#ff6d00", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(c.t.map((t, i) => c.signal[i] == null ? { time: t } : { time: t, value: c.signal[i] }));

  let syncing = false;
  const sync = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange(r => { if (syncing || !r) return; syncing = true; b.timeScale().setVisibleLogicalRange(r); syncing = false; });
  sync(px, mc); sync(mc, px);

  if (el.legend) {
    const byT = new Map(c.t.map((t, i) => [t, i]));
    let xs = false;
    const legend = i => {
      if (i == null) { el.legend.innerHTML = ""; return; }
      const d = c.digits, f = v => v == null ? "–" : v.toFixed(d), pip = c.pip;
      const above = c.c[i] > c.ema[i];
      const edge = above ? Math.max(c.o[i], c.c[i]) - c.ema[i] : c.ema[i] - Math.min(c.o[i], c.c[i]);
      const hv = c.hist[i], shown = hv == null ? "–" : hv.toFixed(d);
      el.legend.innerHTML = `<b>${fmtT(c.t[i])}</b> O ${f(c.o[i])} H ${f(c.h[i])} L ${f(c.l[i])} C ${f(c.c[i])} · range ${((c.h[i] - c.l[i]) / pip).toFixed(1)}p
        <br><span style="color:#2962ff">EMA ${f(c.ema[i])}</span> · ${above ? "above" : "below"} · <span style="color:${c.dir[i] < 0 ? "#4caf50" : "#f23645"}">ST ${f(c.st[i])}</span> · <span style="color:${c.hist_color[i]}">Hist ${shown}</span>`;
    };
    const link = (a, b, bSeries, val) => a.subscribeCrosshairMove(p => {
      if (xs) return;
      xs = true;
      if (!p || p.time == null || !byT.has(p.time)) { b.clearCrosshairPosition(); legend(null); }
      else { const i = byT.get(p.time); const v = val(i); if (v != null) b.setCrosshairPosition(v, p.time, bSeries); legend(i); }
      xs = false;
    });
    link(px, mc, hs, i => c.hist[i]);
    link(mc, px, candles, i => c.c[i]);
  }

  if (opt.fit) {
    px.timeScale().fitContent();
  } else {
    const mid = Math.floor(c.t.length / 2);
    px.timeScale().setVisibleLogicalRange({ from: mid - 50, to: mid + 50 });
  }

  return { px, mc };
}

// ------------------------------------------------------------------ breakdown
function tableFrom(rows, cols) {
  if (!rows || !rows.length) return `<div class="hint">No data.</div>`;
  return `<table><thead><tr>${cols.map(c => `<th class="${c[3] || ""}">${c[0]}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${cols.map(c => `<td class="${c[3] || ""}">${c[2] ? c[2](r[c[1]], r) : esc(r[c[1]])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
const GCOLS = [["", "key"], ["Trades", "trades"], ["Wins", "wins"], ["Losses", "losses"], ["Win %", "win_rate", v => num(v, 1)],
  ["Net", "net", v => `<span class="${cls(v)}">${money(v)}</span>`], ["PF", "pf", v => num(v)], ["Avg R", "avg_r", v => num(v, 3)]];
function renderBreakdown() {
  const r = runs()[+$("#bdRun").value || 0];
  if (!r) return;
  const m = r.result.metrics;
  $("#bdPair").innerHTML = tableFrom(m.by_pair, GCOLS);
  $("#bdSession").innerHTML = tableFrom(m.by_session, GCOLS);
  $("#bdDir").innerHTML = tableFrom(m.by_direction, GCOLS);
  $("#bdExit").innerHTML = tableFrom(m.by_exit, GCOLS);
  $("#bdHour").innerHTML = tableFrom(m.by_hour, GCOLS);
  $("#bdMonthly").innerHTML = tableFrom(m.monthly, [["Month", "month"], ["Trades", "trades"], ["Net", "net", v => `<span class="${cls(v)}">${money(v)}</span>`], ["% of balance", "pct", v => `<span class="${cls(v)}">${pct(v)}</span>`]]);
  if (state.charts.rh) state.charts.rh.destroy();
  state.charts.rh = new Chart($("#rHist"), {
    type: "bar",
    data: { labels: m.r_hist.bins.map(b => `${b.toFixed(1)}`), datasets: [{ data: m.r_hist.counts, backgroundColor: m.r_hist.bins.map(b => b < 0 ? "#ef5350" : "#26a69a") }] },
    options: { plugins: { legend: { display: false } }, scales: { x: { title: { display: true, text: "R multiple (bin start)", color: "#8a93a3" }, ticks: { color: "#8a93a3" }, grid: { color: "#1e232c" } }, y: { ticks: { color: "#8a93a3" }, grid: { color: "#1e232c" } } } },
  });
}

// ------------------------------------------------------------------ rejections
function renderRejections() {
  const r = runs()[+$("#rjRun").value || 0];
  if (!r) return;
  const f = r.result.funnel;
  const rows = [{ k: "Setups where EMA-BOS + ST flip + valid MACD hook lined up", v: f.candidates }]
    .concat(f.rejections.map(x => ({ k: "− " + x.reason, v: x.count }))).concat([{ k: "= Trades entered", v: f.entered }]);
  $("#funnel").innerHTML = tableFrom(rows, [["Step", "k"], ["Count", "v"]]) +
    `<p class="hint">Rejections are counted at the first failing check, in this order: data gap → ADX session → news → histogram stayed on side → Supertrend still in direction → pair busy → max positions → 2-bar (30s) MACD rule → still rising at entry → live Supertrend → SL sanity.</p>`;
  const es = Object.entries(r.result.event_stats).map(([p, s]) => ({ pair: p, ...s, ...r.result.indicator_signals[p] }));
  $("#evStats").innerHTML = tableFrom(es, [["Pair", "pair"], ["5m bars", "bars_5m"], ["ST flips", "st_flips"], ["EMA crosses", "bos_total"], ["Valid BOS", "bos_valid"], ["Body < buffer", "bos_buffer_rejected"], ["Range too big", "bos_range_rejected"],
    ["Hooks", "hooks_total"], ["Valid hooks", "hooks_valid"], ["Sessions green / checked", "sessions_confirmed", (v, x) => `${v} / ${x.sessions_checked}`],
    ["Ind-1 signals", "indicator1_signals"], ["Ind-2 signals", "indicator2_signals"]]) +
    `<p class="hint">Ind-1/Ind-2 signals = what your TradingView indicators would mark (replicated bar-for-bar). They are not trades: indicator 1 accepts the EMA cross up to 2 bars <i>before</i> the ST flip, has no 12-pip filter; indicator 2 has no 2-bar/30s confirmation and no EMA/ST confluence.</p>`;
  const pf = $("#rjPair").value;
  const piv = {};
  (r.result.adx_checks || []).filter(x => !pf || x.pair === pf).forEach(x => {
    const key = x.date + " " + x.pair;
    piv[key] = piv[key] || { date: x.date, pair: x.pair };
    piv[key][x.session] = x;
  });
  const cell = x => x ? `<td class="${x.green ? "g" : "r"}">${x.adx == null ? "–" : num(x.adx)} ${x.green ? "✓" : "✗"}</td><td class="hint">${fmtT(x.adx_bar).slice(11)}</td>` : "<td>–</td><td></td>";
  const prow = Object.values(piv);
  $("#adxWrap").innerHTML = `<table><thead><tr><th>Date</th><th>Pair</th><th>Check 1 ADX</th><th>1H candle</th><th>Check 2 ADX</th><th>1H candle</th></tr></thead><tbody>${prow.map(x => `<tr><td>${x.date}</td><td>${x.pair}</td>${cell(x.S1)}${cell(x.S2)}</tr>`).join("")}</tbody></table>`;
  const reasons = [...new Set(r.result.rejected.map(x => x.reason))];
  const cur = $("#rjReason").value;
  $("#rjReason").innerHTML = `<option value="">All reasons</option>` + reasons.map(x => `<option ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join("");
  const reason = $("#rjReason").value, pair = $("#rjPair").value;
  const list = r.result.rejected.filter(x => (!reason || x.reason === reason) && (!pair || x.pair === pair));
  const shown = list.slice(0, 1500);
  $("#rjWrap").innerHTML = tableFrom(shown, [["Pair", "pair"], ["Dir", "dir"], ["ST flip", "st_bar", v => fmtT(v)], ["BOS", "bos_bar", v => fmtT(v)], ["Hook", "hook_bar", v => fmtT(v)],
    ["Signal bar", "signal_bar", v => fmtT(v)], ["Would enter", "entry_time", v => fmtT(v, true)], ["Reason", "reason", null, "l"],
    ["Detail", "code", (v, x) => esc(x.news || (x.adx != null ? `ADX ${x.adx}` : x.session ? `${x.session} not confirmed` : "") || (x.hist_next != null ? `next ${x.hist_next.toExponential(2)} vs hook ${x.hist_hook.toExponential(2)}` : "") || (x.hist_entry != null ? `entry ${x.hist_entry.toExponential(2)} vs prev ${x.hist_signal.toExponential(2)} / hook ${x.hist_hook.toExponential(2)}` : "") || (x.sl_pips != null ? `${x.sl_pips} pips` : "")), "l"]])
    + (list.length > shown.length ? `<div class="hint">Showing 1500 of ${list.length}.</div>` : "");
  $$("#rjWrap tbody tr").forEach((tr, i) => tr.onclick = () => openChart(shown[i].pair, shown[i].signal_bar));
}

// ------------------------------------------------------------------ rules
function renderRules() {
  const P = state.data ? state.data.params : state.params;
  const imp = { "-1": "low, medium and high", "0": "medium and high (2–3 stars)", "1": "high (3 stars)" }[String(P.news_min_importance)];
  $("#rulesBody").innerHTML = `
<h2>Strategy exactly as implemented (current parameters)</h2>
<h3>1. Session selection — ADX on 1H (${P.tz})</h3>
<ul>
<li>At <b>${P.s1_check_hour}:00</b> and <b>${P.s2_check_hour}:00</b> each pair's ADX(${P.adx_len}) is read from ${P.adx_bar === "completed" ? "the <b>last completed</b> 1H candle (e.g. the ${P.s1_check_hour - 1}:00–${P.s1_check_hour}:00 candle at the ${P.s1_check_hour}:00 check)" : "<b>the check-hour candle itself</b> — this matches the labels on the 1H chart but uses up to an hour of future data"}.</li>
<li>ADX is the indicator's own <code>calc_adx</code>: Wilder-style running sums seeded from 0, then an SMA(${P.adx_len}) of DX (not TradingView's standard ADX).</li>
<li>Green when ${P.adx_min} ≤ ADX ≤ ${P.adx_max}. Green at the first check → the pair may be traded ${P.s1_check_hour}:00–${P.s1_end_hour}:00; green at the second → ${P.s2_check_hour}:00–${P.s2_end_hour}:00. Each check is independent and resets daily. ${P.use_adx_filter ? "" : "<b>(ADX filter currently OFF)</b>"}</li>
</ul>
<h3>2. Signals on the 5m chart (all evaluated on candle close)</h3>
<ul>
<li><b>Supertrend(${P.st_atr}, ${P.st_factor})</b> flip = direction changes (Pine <code>ta.supertrend</code>).</li>
<li><b>EMA(${P.ema_len}) break of structure</b>: ${P.bos_mode === "indicator" ? `close crosses the EMA ± ${P.bos_buffer_pips} pip line (can be a candle after the one that crossed the EMA itself)` : `measured <b>only on the candle whose close crosses the EMA</b>: its body top (bottom for sells) must be ≥ <b>${P.bos_buffer_pips} pips</b> beyond the EMA — wicks don't count, and if that candle fails, later candles cannot make it a BOS`}. The BOS candle's high–low (wicks) must be ≤ <b>${P.bos_max_range_pips} pips</b>.</li>
<li>The BOS candle must be the ST-flip candle or up to <b>${P.bos_after_st_max}</b> candle(s) after it${P.bos_before_st_max > 0 ? `, or up to ${P.bos_before_st_max} before it` : ""}.</li>
<li><b>MACD(${P.macd_fast}, ${P.macd_slow}, ${P.macd_signal}) hook</b>: the histogram changes colour (red→green for buys, green→red for sells) after <b>${P.hook_min}–${P.hook_max}</b> consecutive receding bars (light red / light green, as TradingView colours them).</li>
<li><b>Zero bars</b>: bars whose value displays as 0 at the symbol's precision (|hist| &lt; ${P.zero_eps_ticks} tick) ${P.zero_mode === "against" ? "and sit one bar before, on, or right after a colour change <b>never count as the new colour</b> — the hook bar must have a real value, and a 0-bar right after the hook breaks it" : P.zero_mode === "literal" ? "and sit one bar before, on, or right after a colour change are read as the <b>opposite</b> of the colour TradingView paints" : P.zero_mode === "raw" ? "keep TradingView's colour" : "are always read as red"}.</li>
<li><b>2-bar rule</b>${P.hook_confirm ? "" : " (OFF)"}: the hook bar and the bar after it must both rise (buys) / fall (sells); the second bar is measured ${P.hook_confirm_mode === "snapshot" ? `<b>${P.entry_delay_sec}s after it opens</b>` : "at its close"}.</li>
<li><b>Confluence window</b>: if the hook comes first, ST flip and BOS must both be complete by hook bar + ${P.macd_window_bars - 1} (a ${P.macd_window_bars}-bar window; 4th bar or later = discarded). If EMA/ST come first, the hook must be at most ${P.macd_after_span_max} bars after the later of the two. The bar where the last condition completes is the <b>signal bar</b>.</li>
<li>The histogram must stay on the new side from the hook to the entry${P.require_monotonic ? ", and <b>every bar from the hook to the signal bar must be stronger than the one before</b> (no light/fading bar)" : ""}; Supertrend must still point in the trade direction.</li>
<li><b>+30s checks</b> (2-bar rule and entry bar): the forming bar's histogram is stronger than a reference exactly when the price is beyond a critical level, so the check is "was the price beyond that level 30 s after the open". With 5-second data (OANDA) this is exact. With bar data it is decided from the price range of the 1m bar (or 5m bar) that contains the 30 s mark: all of it on one side = certain; otherwise undecidable → ${P.momentum_no5s === "range_reject" ? "rejected" : P.momentum_no5s === "range_accept" ? "accepted (counted in the warnings)" : P.momentum_no5s}.</li>
</ul>
<h3>3. Entry</h3>
<ul>
<li>Market order <b>${P.entry_delay_sec}s after the next candle opens</b>${P.entry_still_check ? ", only if the entry bar's histogram (recomputed with the price at that second) is higher than <b>both</b> the previous bar and the hook bar (lower for sells) — a light green / light red entry bar invalidates the setup" : ""}. Buys fill at the ask, sells at the bid (no slippage).</li>
<li>The entry moment must be inside an ADX-confirmed session and outside [news − ${P.news_before_min} min, news + ${P.news_after_min} min].</li>
<li>One position per pair, at most ${P.max_open_total} open in total. Size ${P.lots} lots.</li>
</ul>
<h3>4. Exits</h3>
<ul>
<li>${P.big_candle_pips > 0 ? `<b>Big candles</b>: if the signal candle and the candle before it are both ≥ ${P.big_candle_pips} pips (high–low), the SL goes on the <b>entry candle's Supertrend</b> instead of the average (set when the entry candle closes; until then on the signal candle's Supertrend).` : "Big-candle SL rule off."}</li>
<li><b>SL</b> = ${P.sl_mode === "avg_close" ? "the middle (average) of the Supertrend value of the signal candle and of the entry candle, as drawn on the chart — if the line is flat, the SL is on the line. The entry candle's value is only final when it closes, so during the entry candle the SL sits on the signal candle's Supertrend, then moves to the average" : P.sl_mode === "avg_live" ? "average of the Supertrend value of the signal candle and the live Supertrend value of the entry candle at the moment of entry" : "Supertrend value of the signal candle"}. <b>TP</b> = entry ± ${P.rr} × the distance from entry to that SL.</li>
<li>Longs are stopped/targeted on the bid, shorts on the ask. A gap through a level fills at the gap price.</li>
<li>${P.be_enabled ? `<b>Break-even</b>: once price has covered ${P.be_trigger_pct}% of the distance from entry to TP, the SL moves to entry${P.be_offset_pips ? ` ${P.be_offset_pips > 0 ? "+" : ""}${P.be_offset_pips} pips` : ""}. It applies from the next bar (5s / 1m / 5m), because inside one bar the order of high and low is unknown. With commission, a break-even exit is a small loss.` : "Break-even off."}</li>
<li>${P.close_before_news_min > 0 ? `Open trades are closed at market <b>${P.close_before_news_min} min before</b> any relevant news.` : "No pre-news close."} ${P.close_at_session_end ? "Trades are closed at session end." : ""} ${P.close_before_weekend ? `Trades are closed Friday ${P.weekend_close_hour}:00.` : ""}</li>
</ul>
<h3>5. News</h3>
<ul><li>TradingView economic calendar, importance ${imp}, all categories, countries ${P.news_countries.join(", ")}; ${P.news_scope === "pair" ? "a pair is only blocked by news of its own two currencies (as indicator 1 does)" : "any listed news blocks every pair"}. Note: GBP news is not in your list, so GBPUSD is only filtered by US news.</li></ul>

<h2>When do your TradingView indicators print a signal — and why that is not a trade</h2>
<table>
<tr><th>Indicator 1 (EMA + ST) prints ▲/▼ when…</th><th>Difference to your written rules</th></tr>
<tr><td>Supertrend flips AND <code>close</code> crossed EMA ± 0.8 pip on the same bar or up to <b>2 bars before</b> the flip, bar open time 09:00–19:00 London (11:00–21:00 BGN), not within ±10 min of the news times typed into the inputs.</td>
<td>Your rule wants the BOS on the flip bar or the <b>next</b> bar (after, not before). No 12-pip candle filter in the indicator. News check uses the bar <i>open</i> time only and only the manually entered times. No ADX, no MACD.</td></tr>
<tr><th>Indicator 2 (MACD + ADX) prints ▲/▼ when…</th><th>Difference</th></tr>
<tr><td>Histogram crosses 0 after <b>2–5</b> receding bars and the bar is inside a confirmed session (ADX 25–40 at the check).</td>
<td>Its defaults are 11:00 / 15:00 / 22:00 BGN; the backtester uses ${P.s1_check_hour}:00 / ${P.s2_check_hour}:00 / ${P.s2_end_hour}:00 ${tzName()} — make the indicator's session-end input match. No 2-bar / 30-second confirmation, no zero-value rule, no EMA/ST confluence. On a 1H chart the label shows the ADX of the check-hour candle (finished an hour later), while on lower timeframes it is the previous completed candle.</td></tr>
</table>
<h2>Data & precision</h2>
<ul>
<li>Prices: ${P.source === "oanda" ? "OANDA v20 1m bid/ask, 5-second candles for the 30s entry moment and for any 1m bar where SL and TP are both touched." : "TradingView OANDA:… bars (1m where available, else 5m). No ask price → fixed spread from Costs is added. The +30s price is interpolated inside the 1m bar (or 5m bar if no 1m)."}</li>
<li>Indicators are recomputed bar-by-bar with Pine's exact formulas (verified against independent implementations; use <i>Data connections → Verify</i> with a TradingView login to compare with TradingView's own values).</li>
<li>No look-ahead: all signals use closed candles; the only forming-candle values are the live MACD/Supertrend at the entry second, computed from prices up to that second.</li>
<li>P&L in USD: JPY/CHF quoted pairs are converted with USDJPY/USDCHF at exit time. No swap/financing is modelled.</li>
</ul>`;
}

// ------------------------------------------------------------------ tabs, connections, init
function showTab(name) {
  $$(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
}
async function saveConn() {
  const body = {};
  if ($("#tvSid").value) body.tv_sessionid = $("#tvSid").value;
  if ($("#tvSidSign").value) body.tv_sessionid_sign = $("#tvSidSign").value;
  if ($("#oaTok").value) body.oanda_token = $("#oaTok").value;
  body.oanda_env = $("#oaEnv").value;
  $("#connStatus").textContent = "Testing…";
  try {
    const st = await api("/api/settings", body);
    $("#connStatus").textContent = Object.entries(st).map(([k, v]) => `${k}: ${v}`).join(" · ") || "Saved.";
    ["#tvSid", "#tvSidSign", "#oaTok"].forEach(s => $(s).value = "");
    state.cfg = await api("/api/config"); updateBadge();
  } catch (e) { $("#connStatus").textContent = "Error: " + e.message; }
}
async function verifyTv() {
  $("#connStatus").textContent = "Running TradingView's built-in EMA / Supertrend / MACD on OANDA:EURUSD 5m…";
  try {
    const r = await api("/api/verify_tv", { pair: (state.params.pairs[0] || "EURUSD"), params: state.params });
    $("#connStatus").innerHTML = Object.entries(r).map(([k, v]) => `<b>${k}</b>: ${v.error ? v.error : `${v.bars_compared} bars, max diff ${v.max_abs_diff_pips?.toExponential(2)} pips`}`).join("<br>");
  } catch (e) { $("#connStatus").textContent = e.message; }
}

async function checkEnt() {
  $("#dlStatus").textContent = "Checking…";
  try { const e = await api("/api/tv/entitlements"); $("#dlStatus").innerHTML = Object.entries(e).map(([k, v]) => `<b>${k}</b>: ${esc(v)}`).join("<br>"); }
  catch (e) { $("#dlStatus").textContent = e.message; }
}
async function pollDl() {
  const j = await api("/api/tv/download");
  const rows = Object.entries(j.status || {}).map(([k, v]) => `${k}: ${v[0]} → ${v[1]} (${v[2]} bars)`);
  $("#dlStatus").innerHTML = (j.running ? `<b>Downloading… ${esc(j.current || "")}</b><br>` : "<b>Done.</b><br>") +
    (j.error ? `<span style="color:var(--bad)">${esc(j.error)}</span><br>` : "") + rows.join("<br>") +
    (j.log && j.log.length ? "<br>" + j.log.map(esc).join("<br>") : "");
  if (j.running) setTimeout(pollDl, 2000); else { state.cfg = await api("/api/config"); setDateBounds(); }
}
async function startDl() {
  try {
    await api("/api/tv/download", { since: $("#dlSince").value, tfs: $("#dlTfs").value.split(","), pairs: state.cfg.pairs });
    pollDl();
  } catch (e) { $("#dlStatus").textContent = e.message; }
}

async function init() {
  state.cfg = await api("/api/config");
  const saved = LS.get("momo.params", null);
  state.params = structuredClone(state.cfg.defaults);
  if (saved && !LS.get("momo.v2", false)) { delete saved.bos_mode; delete saved.decision_price; delete saved.zero_mode; LS.set("momo.params", saved); }
  LS.set("momo.v2", true);
  if (saved && !LS.get("momo.v3", false)) { delete saved.sl_mode; delete saved.slippage_pips; LS.set("momo.params", saved); }
  LS.set("momo.v3", true);
  if (saved && !LS.get("momo.v4", false)) { delete saved.require_monotonic; delete saved.decision_price; LS.set("momo.params", saved); }
  LS.set("momo.v4", true);
  if (saved && !LS.get("momo.v5", false)) { ["tz", "s1_check_hour", "s1_end_hour", "s2_check_hour", "s2_end_hour", "weekend_close_hour"].forEach(k => delete saved[k]); LS.set("momo.params", saved); }
  LS.set("momo.v5", true);
  if (saved) Object.entries(saved).forEach(([k, v]) => { if (k in state.params) state.params[k] = (typeof v === "object" && !Array.isArray(v) && v) ? { ...state.params[k], ...v } : v; });
  setTZ(state.params.tz);
  buildParams(); updateBadge(); syncBe(); setDateBounds();
  $("#beOn").onchange = () => setP("be_enabled", $("#beOn").checked);
  $("#bePct").onchange = () => { const v = Math.min(100, Math.max(1, Number($("#bePct").value) || 50)); $("#bePct").value = v; setP("be_trigger_pct", v); };
  $("#beOff").onchange = () => setP("be_offset_pips", Number($("#beOff").value) || 0);
  $("#beRun").onclick = runBacktest;
  quickPeriod("tvmax");
  $("#oosRow").style.display = $("#oosOn").checked ? "" : "none";
  $("#oosOn").onchange = () => { $("#oosRow").style.display = $("#oosOn").checked ? "" : "none"; periodsFromUI(); };
  ["#isStart", "#isEnd", "#oosStart", "#oosEnd"].forEach(s => $(s).onchange = periodsFromUI);
  $$(".quick button").forEach(b => b.onclick = () => quickPeriod(b.dataset.q));
  const cov = Object.entries(state.cfg.coverage).map(([p, c]) => c.tv_5 ? `${p} 5m: ${c.tv_5[0].slice(0, 10)} → ${c.tv_5[1].slice(0, 10)}` : null).filter(Boolean);
  $("#covHint").innerHTML = (cov.length ? "Cached TradingView data:<br>" + cov.join("<br>") : "No data cached yet — the first run downloads it.") +
    "<br><b>Limit:</b> TradingView (anonymous) serves ~4 weeks of 5m and ~1 week of 1m; the cache keeps everything you download, so history grows if you run regularly. OANDA: years.";
  $$(".tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
  $("#btnRun").onclick = runBacktest;
  $("#btnConn").onclick = () => { $("#oaEnv").value = state.cfg.oanda_env; $("#connStatus").textContent = ""; $("#connDlg").showModal(); };
  $("#connSave").onclick = saveConn; $("#verifyBtn").onclick = verifyTv;
  $("#entBtn").onclick = checkEnt; $("#dlBtn").onclick = startDl;
  $("#dlCancel").onclick = () => api("/api/tv/download/cancel", {});
  ["#trRun", "#trPair", "#trDir", "#trExit"].forEach(s => $(s).onchange = renderTrades);
  $("#bdRun").onchange = renderBreakdown;
  ["#rjRun", "#rjReason", "#rjPair"].forEach(s => $(s).onchange = renderRejections);
  $("#csvBtn").onclick = exportCsv;
  $("#chLoad").onclick = () => loadChart();
  $$(".mk").forEach(x => x.onchange = () => loadChart());
  $("#resetDefaults").onclick = () => { state.params = structuredClone(state.cfg.defaults); saveParams(); buildParams(); updateBadge(); syncBe(); };
  const presets = () => LS.get("momo.presets", {});
  const fillPresets = () => { $("#presetSel").innerHTML = `<option value="">— saved presets —</option>` + Object.keys(presets()).map(n => `<option>${esc(n)}</option>`).join(""); };
  fillPresets();
  $("#presetSave").onclick = () => { const n = $("#presetName").value.trim(); if (!n) return; const p = presets(); p[n] = state.params; LS.set("momo.presets", p); fillPresets(); };
  $("#presetLoad").onclick = () => { const p = presets()[$("#presetSel").value]; if (!p) return; state.params = { ...structuredClone(state.cfg.defaults), ...structuredClone(p) }; saveParams(); buildParams(); updateBadge(); syncBe(); };
  renderRules();
}
init().catch(e => { document.body.insertAdjacentHTML("afterbegin", `<div class="warn err">Init failed: ${esc(e.message)}</div>`); });
