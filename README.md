# NIFTY Options Trading Bot

A sophisticated automated trading bot for NIFTY, BANKNIFTY, and SENSEX options with **multi-broker support (Dhan + Zerodha)**, **6 intraday strategies**, **real-time P&L tracking**, **Kelly-based position sizing**, **performance analytics**, and **Telegram alerts**.

---

## Quick Start

Already set up? Run every morning before 9:15 AM:

```powershell
.venv\Scripts\Activate.ps1
python main.py
```

Then open: **http://localhost:8000**

> Before going live, run the pre-live health check:
> ```powershell
> python pre_live_checklist.py
> ```
> All critical checks must pass before setting `TRADING_AUTO_TRADE_ENABLED=true`.

---

## Installation (First Time Setup)

### Prerequisites

- **Python 3.10 or higher** — [Download](https://www.python.org/downloads/)
- **Git** — [Download](https://git-scm.com/downloads/)
- A **Dhan account** ([dhanhq.co](https://dhanhq.co/)) — free API, recommended

---

### Step 1 — Clone the Repository

```powershell
git clone <repo-url>
cd "VS Code BOT\BOT"
```

---

### Step 2 — Create Virtual Environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

> If PowerShell blocks the script, run once as Administrator:
> `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`

---

### Step 3 — Install Dependencies

```powershell
pip install -r requirements.txt
```

Core packages installed:

| Package | Purpose |
|---------|---------|
| `fastapi` + `uvicorn` | Web dashboard server |
| `dhanhq` | Dhan broker API (free) |
| `kiteconnect` | Zerodha Kite API (optional) |
| `playwright` | Browser automation — Zerodha token refresh only |
| `yfinance` | Market data (15m/5m/1m candles) |
| `pandas`, `numpy` | Data processing |
| `pydantic-settings` | Configuration management |
| `loguru` | Structured logging |
| `httpx` | Async HTTP (Telegram alerts) |
| `curl_cffi` | NSE option chain scraping |

**Zerodha users only** — install headless browser:
```powershell
playwright install chromium
```

---

### Step 4 — Create `.env` File

Create a file named `.env` in the project root:

```env
# ============ BROKER SELECTION ============
BROKER=dhan          # 'dhan' (recommended — free) or 'zerodha'

# ============ DHAN API ============
# Get from: https://developer.dhanhq.co → My Apps → your app → copy token
# Token is long-lived (~1 year). Regenerate from portal when expired.
DHAN_CLIENT_ID=your_dhan_client_id
DHAN_ACCESS_TOKEN=your_dhan_access_token

# ============ ZERODHA (only if BROKER=zerodha) ============
ZERODHA_USER_ID=your_user_id
ZERODHA_PASSWORD=your_password
ZERODHA_PIN=your_6_digit_pin         # used by get_kite_token.py for 2FA
ZERODHA_TOTP_SECRET=                 # optional: overrides PIN if set
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_ACCESS_TOKEN=                   # auto-filled by get_kite_token.py
KITE_USE_KITE_API=true

# ============ TRADING RISK SETTINGS (calibrated for ₹45,000 capital) ============
TRADING_AUTO_TRADE_ENABLED=false     # Set true only after testing

TRADING_MAX_POSITIONS=2
TRADING_MAX_LOSS_PER_TRADE=2250      # 5% of ₹45k capital
TRADING_MAX_DAILY_LOSS=4500          # 10% of ₹45k capital
TRADING_STOP_LOSS_PERCENTAGE=15
TRADING_TARGET_PERCENTAGE=30
TRADING_MAX_CONSECUTIVE_LOSSES=5
TRADING_PAUSE_AFTER_LOSSES_MINUTES=60
TRADING_MAX_TRADES_PER_DAY=3
TRADING_CLOSE_ALL_BEFORE_MARKET_CLOSE=3

# Adaptive time-stops (automatically applied per strategy source)
# GAP=60min, ORB=45min, VWAP=30min, Auto=90min, EOD=15min, Manual/Rule=45min
TRADING_TIME_STOP_MINUTES=45        # default; overridden per-strategy source

# Smart time-based exit — only close LOSING positions at 13:00 IST
# Profitable positions (pnl >= TIME_EXIT_MIN_PROFIT) are left to run toward target
TRADING_TIME_EXIT_ONLY_LOSERS=true
TRADING_TIME_EXIT_MIN_PROFIT=50      # ₹ threshold — positions above this skip the time stop

# Hard cutoffs
TRADING_HARD_TIME_EXIT_HOUR=13      # force-close positions entered before 13:00 at 13:00 IST

# India VIX filter
TRADING_VIX_FILTER_ENABLED=true
TRADING_VIX_MAX=27                  # auto-entries blocked when India VIX > 27

# IV Percentile filter (strike-level — needs 5+ days of .iv_history.json before activating)
TRADING_IV_FILTER_ENABLED=false     # enable after first week of live trading
TRADING_IV_PERCENTILE_MAX=80        # block if ATM IV > 80th pct of last 30 days

# Theta drain monitor (warns in logs/dashboard — does not auto-exit)
TRADING_THETA_EXIT_ENABLED=true
TRADING_THETA_EXIT_THRESHOLD=-50    # ₹/day drain that triggers warning


# Smart exits
TRADING_USE_TRAILING_STOP_LOSS=true
TRADING_TRAILING_STOP_PERCENTAGE=12
TRADING_TRAILING_STOP_ACTIVATION_PCT=10
TRADING_USE_PROFIT_TIERS=true
TRADING_TAKE_PROFIT_TIER_1_PERCENT=30    # partial exit at +30% premium gain
TRADING_TAKE_PROFIT_TIER_1_QUANTITY_PERCENT=50
TRADING_TAKE_PROFIT_TIER_2_PERCENT=60    # partial exit at +60% premium gain
TRADING_TAKE_PROFIT_TIER_2_QUANTITY_PERCENT=50
TRADING_TIGHT_TRAIL_AFTER_TIER2_PCT=5   # trail tightens to 5% after both tiers hit

# GTT / Forever Orders (exchange-level stop — survives bot crash)
TRADING_USE_GTT=true

# AMO — After Market Orders (Dhan only)
TRADING_AMO_ENABLED=true

TRADING_MIN_TIME_BETWEEN_TRADES_MINUTES=10
TREND_ANALYSIS_INTERVAL_SECONDS=60

# ============ STRATEGY THRESHOLDS ============
# Gap detection
GAP_ENABLED=true
GAP_MIN_GAP_PCT=0.75
GAP_STRONG_GAP_PCT=1.5
GAP_STOP_LOSS_PCT=25
GAP_TARGET_PCT=50

# VWAP Mean Reversion (tightened to avoid noise)
VWAP_DEVIATION_PCT=0.6        # % deviation from VWAP to trigger (default: 0.6)
VWAP_RSI_OVERSOLD=38          # for LONG signal (default: 38)
VWAP_RSI_OVERBOUGHT=62        # for SHORT signal (default: 62)

# EOD Closing Momentum (14:30–15:00 IST)
EOD_ENABLED=true
EOD_MIN_BODY_PCT=0.5          # 50% body minimum to avoid doji candles
# ============ ENTRY FILTER & POSITION SIZER ============
# Entry scoring — signals must score ≥70/100 to be accepted
FILTER_ENABLED=true
FILTER_MIN_CONFIDENCE_SCORE=70        # 0–100; higher = stricter signal gate

# Kelly-based dynamic position sizing
POSITION_SIZER_ENABLED=true
POSITION_SIZER_MAX_RISK_PER_TRADE_PCT=2.0   # max % of account risked per trade
POSITION_SIZER_KELLY_FRACTION=0.25          # fractional Kelly (25%); activates after 20 trades
TRADING_ACCOUNT_BALANCE=45000               # ← set to your actual capital for correct sizing

# ============ TELEGRAM ALERTS (Optional) ============
ALERT_TELEGRAM_ENABLED=false
ALERT_TELEGRAM_BOT_TOKEN=your_bot_token
ALERT_TELEGRAM_CHAT_ID=your_chat_id
```

---

### Step 5 — Verify Setup

```powershell
.venv\Scripts\python.exe -c "import dhanhq, yfinance, fastapi; print('All packages OK')"
```

---

### Step 6 — Start the Bot

```powershell
python main.py
```

Open **http://localhost:8000** — you should see the dashboard.

> The bot starts with `TRADING_AUTO_TRADE_ENABLED=false`. It will analyse and show signals but will not place real orders until you set that to `true`.

---

### Step 7 — Enable Telegram Alerts (Optional)

1. Open Telegram → search `@BotFather` → `/newbot` → copy token
2. Send any message to your new bot
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy `id` from `"chat"`
4. Add to `.env`:
   ```env
   ALERT_TELEGRAM_ENABLED=true
   ALERT_TELEGRAM_BOT_TOKEN=your_token
   ALERT_TELEGRAM_CHAT_ID=your_chat_id
   ```
5. Restart the bot

---

### Step 8 — Enable Live Trading

Once you've watched signals for 1–2 weeks and are satisfied:

```env
TRADING_AUTO_TRADE_ENABLED=true
TRADING_DEFAULT_QUANTITY=65    # NIFTY lot (30 for BANKNIFTY, 20 for SENSEX)
```

> **Start with 1 lot** (`TRADING_DEFAULT_QUANTITY=65` for NIFTY = 1 lot). Scale up gradually.

---

## Daily Startup Schedule (IST)

| Time | Action |
|------|--------|
| **8:45 AM** | `python main.py` |
| **9:15 AM** | Market opens — Gap strategy fires |
| **9:15–9:30 AM** | Opening gap window |
| **9:30–11:30 AM** | ORB entry window |
| **9:30 AM–2:00 PM** | Auto trend trades |
| **9:30 AM–2:30 PM** | VWAP mean reversion active |
| **2:30–3:00 PM** | EOD Closing Momentum window |
| **2:30–3:25 PM** | Late-Day Oscillation window (NIFTY, if enabled) |
| **3:15 PM** | Force-exit (ORB strategy) |
| **3:27 PM** | Bot force-closes all positions |
| **3:30 PM** | Market closes |

---

## Strategies

### 1. Gap Up / Gap Down
Fires once at market open (9:15–9:30 AM). Compares today's open to yesterday's close.

| Gap % | Action |
|-------|--------|
| > 1.5% up | Buy CE immediately |
| 0.75–1.5% up | Wait for ORB confirmation |
| < ±0.75% | No gap trade — normal strategies |
| 0.75–1.5% down | Wait for ORB confirmation |
| > 1.5% down | Buy PE immediately |

---

### 2. Opening Range Breakout (ORB)
- **Range built**: 9:15–9:30 AM (first 15 minutes)
- **Entry window**: 9:30–11:30 AM
- **Requires**: 2 consecutive closes beyond range + volume confirmation
- **Stop loss**: Opposite end of opening range
- **Targets**: Range width × 1.5 (T1), × 2.0 (T2)
- **Guards**: Minimum range width (≥75 pts NIFTY, ≥150 pts BANKNIFTY/SENSEX), trend-direction filter, 15m MTF confluence, RSI extreme filter
- **Late-entry time decay**: After 11:00 AM, breakout strength loses 2 pts per 6 minutes (max −20 pts, floor at 60%). A raw 80% signal at 11:18 AM becomes 74% — below the 75% threshold, so blocked.
- Max 1 trade per index per day

---

### 3. Trend Following (Auto)
- Runs every 60 seconds during market hours
- Uses SMA(20/50), EMA(9/21), RSI(14), MACD, Supertrend, VWAP, Bollinger Bands
- Only fires when regime confirms a trending market (ADX > 25, Hurst > 0.55)
- **Cutoff: 14:00 IST** — no new trend auto-entries after 2 PM (thin liquidity)
- Marks both daily slots on success — max 1 auto-trade per index per session

---

### 4. VWAP Mean Reversion
- Active when regime is **RANGING** or strong trend (ADX > 50) with neutral 5m signal
- Entry window: 9:30 AM–2:30 PM
- Requires deviation ≥ **0.6%** from VWAP (tightened from 0.4% to cut noise)
- RSI confirmation: < **38** for LONG, > **62** for SHORT (tightened from 42/58)
- Minimum **60% confidence score** required (volume spike or deep RSI extreme)
- Regime-direction alignment: TRENDING DOWN blocks CE; TRENDING UP blocks PE
- Max 1 VWAP trade per index per day

---

### 5. EOD Closing Momentum
- Fires once between **2:30–3:00 PM** per index per day
- Reads the last completed **15-minute candle**
- Requires candle body ≥ **50% of the candle's high-low range** (filters out doji/indecision)
- Bullish candle → Buy CE; Bearish candle → Buy PE
- Trend-direction alignment enforced (counter-trend trades blocked)
- Position exits at 3:27 PM force-close
- Validated on March 18, 2026: all 3 indices (NIFTY −72 pts, BANKNIFTY −151 pts, SENSEX −209 pts) predicted and traded correctly from the 14:30 candle

---

### 6. Late-Day Oscillation *(configurable — disabled by default)*
- Active window: **2:30–3:25 PM** IST, NIFTY only
- Detects oscillation / mean-reversion patterns in the last 30 minutes of the session
- Requires explicit validation before enabling: run `python analysis/late_day_oscillation_validator.py`
- Enable via `.env`: `LATE_DAY_ENABLED=true` (only after validator confirms edge)
- Max 1 signal per day — exits at or before 3:25 PM force-close

---

### Strategy Selection by Regime

| Regime | Active Strategies | Max Positions |
|--------|------------------|---------------|
| TRENDING UP/DOWN (ADX ≤ 50) | Trend + ORB + Gap + EOD | 2 |
| TRENDING UP/DOWN (ADX > 50) | Trend + ORB + VWAP + Gap + EOD | 2 |
| RANGING | VWAP + ORB + Gap + EOD | 2 |
| VOLATILE | ORB + Gap only | 1 |

Late-Day Oscillation is checked independently of regime (NIFTY only, 14:30–15:25 window).

---

## Risk Management

### 1. Entry Filter — High-Probability Signal Gate

Every signal passes through a **100-point scoring gate** before any capital is committed. Signals scoring below 70/100 are rejected.

| Gate | Max Points | Condition |
|------|-----------|-----------|
| Volume confirmation | 15 | Current volume vs 20-bar average |
| Trend alignment | 20 | 5m/15m/1h trend direction matches signal |
| Indicator confluence | 15 | RSI, MACD, SMA-20, Bollinger Bands, ADX |
| Support/Resistance respect | 15 | Distance from prev-day H/L, VWAP, BB bands |
| Historical win rate | 10 | Per-strategy win rate from journals |
| Optimal time window | 10 | ORB: 9:30–11:30, VWAP: 10:30–14:30, etc. |
| VIX / Volatility | 10 | Strategy-specific VIX checks |
| Calendar events | 5 | Thursday expiry heuristic |

Configure: `FILTER_ENABLED=true`, `FILTER_MIN_CONFIDENCE_SCORE=70`

### 2. Dynamic Position Sizing (Kelly Criterion)

Position size is calculated from actual risk — not a fixed lot count:

```
max_risk_₹       = account_balance × max_risk_pct / 100
risk_per_unit    = entry_price − stop_loss_price
base_qty         = max_risk_₹ / risk_per_unit
final_qty        = base_qty × confidence_factor × kelly_factor
```

**Confidence → size multipliers:**

| Signal Score | Multiplier |
|-------------|-----------|
| ≥ 90 | 1.3× |
| ≥ 80 | 1.1× |
| ≥ 70 | 1.0× |
| ≥ 60 | 0.8× |
| < 60 | 0.5× |

**Kelly factor** activates after ≥20 historical trades per strategy. Uses 25% fractional Kelly (`POSITION_SIZER_KELLY_FRACTION=0.25`).

Configure: `POSITION_SIZER_ENABLED=true`, `POSITION_SIZER_MAX_RISK_PER_TRADE_PCT=2.0`, `TRADING_ACCOUNT_BALANCE=<your capital>`

### 3. Exit Manager — Multi-Layer Exit System

Five exit components fire in priority order for every open position:

| Priority | Component | Condition |
|---------|-----------|-----------|
| 1 | **Dynamic Stop-Loss** | ORB: opposite range end; VWAP/Trend: 2×ATR; GAP_FADE: 0.8×ATR; LATE_DAY: 0.15% fixed |
| 2 | **Multi-Target (T1/T2/T3)** | 40% at 1R, 30% at 2R, 20% at 3R; SL auto-moves to breakeven after T1 |
| 3 | **Trailing Stop** | Activates at +20% profit; give-back: 50%→40%→30% as profit grows |
| 4 | **Time-Based Exit** | ORB: 2 hr, VWAP: 1 hr, LATE_DAY: 12 min; losers exit at 14:30; force-close 15:15 |
| 5 | **Volatility Exit** | VIX spike +20% + profit>15% → exit; ATR expansion >1.5× on scalps → exit |

### 4. Entry Guards (False Signal Prevention)

| Guard | Where | Detail |
|-------|-------|--------|
| **Entry filter score ≥ 70** | All strategies | 8-gate 100-point scoring system (new) |
| Market regime filter | All strategies | Only trades TRENDING/VOLATILE conditions |
| Trend-direction alignment | ORB, VWAP, EOD | Blocks counter-trend entries |
| 15m MTF confluence | ORB | 15m candle must agree with ORB direction |
| RSI extreme filter | ORB | Blocks CE when RSI ≥75 (overbought), PE when RSI ≤25 (oversold) |
| ORB strength ≥ 65% | ORB | Minimum breakout confidence |
| ORB min range width | ORB | ≥ 75 pts NIFTY / ≥ 150 pts BANKNIFTY/SENSEX |
| **ORB time decay after 11 AM** | ORB | Strength −2 pts per 6-min slot after 11:00 AM (max −20 pts, floor 60%) |
| VWAP deviation ≥ 0.6% | VWAP | Was 0.4% — tightened to cut noise |
| VWAP RSI thresholds 38/62 | VWAP | Was 42/58 — tightened for real extremes |
| VWAP confidence ≥ 60% | VWAP | Needs volume spike or deep RSI extreme |
| Auto-trade 14:00 cutoff | Auto | No trend entries after 2 PM |
| **India VIX filter** | Auto | Auto-entries blocked when India VIX > 20 |
| **IV percentile filter** | Auto | Blocks entry when ATM IV is above 80th pct of last 30-day history |
| **Hard 1 PM exit** | All (except EOD) | Positions entered before 13:00 are force-closed at 13:00 IST (minimum 15-min hold) |
| EOD body ≥ 50% | EOD | Doji / indecision candles skipped |
| 2 consecutive ORB closes | ORB | Requires 2 closes beyond trigger before entry |
| Strike-loss guard | OrderManager | Blocks re-entry on a strike already lost today |
| Over-sell guard | OrderManager | Prevents naked shorts |

### Position Limits

- Max 2 open positions at any time (`TRADING_MAX_POSITIONS=2`)
- Both ORB and VWAP slots tracked per-index per-day independently
- Auto-trade consumes **both** slots on success — no double-entry after SL
- Max **3 trades per day** total across all indices (calibrated for ₹45k capital)

### Adaptive Time-Stops (Per Strategy)

Positions that never move >2% into profit are closed automatically based on the strategy that opened them:

| Strategy | Time-Stop | Notes |
|----------|-----------|-------|
| GAP | 60 min | Entered at open — wider window |
| ORB | 45 min | Standard breakout window |
| VWAP | 30 min | Mean-reversion — resolves fast |
| Auto/Trend | 90 min | Wider trend continuation window |
| EOD | 15 min | Entered 14:30+, expires at force-close |
| Manual / Rule | 45 min | Default |

### Exit Ladder

Positions use a three-tier profit ladder + tightening trail (legacy ladder — ExitManager system is more granular):

| Tier | Trigger | Action |
|------|---------|--------|
| T1 | +30% premium gain | Exit 50% quantity; SL auto-moves to breakeven |
| T2 | +60% premium gain | Exit remaining 50% |
| Trail (normal) | After T1 | 12% trailing stop on remaining |
| Trail (tight) | After both T1 + T2 hit | Tightens to 5% — locks in profits |

**ExitManager** (new, per-trade): evaluates 5 layers every 15 s — SL → Multi-Target (1R/2R/3R) → Trailing Stop → Time-Based → Volatility. Activated for all new entries.

### Trade Record Analytics

Every trade now captures analytics fields for post-session review:

| Field | Description |
|-------|-------------|
| `source` | Strategy that opened the trade (GAP / ORB / VWAP / Auto / EOD / Rule / Manual) |
| `vix_at_entry` | India VIX at time of entry (auto-entries only; 0 for manual) |
| `slippage` | Fill price − pre-order LTP in ₹ (positive = paid more than expected) |
| `iv_pct_at_entry` | ATM Implied Volatility % at entry (e.g. 14.5 = 14.5%) |
| `iv_percentile_at_entry` | IV percentile vs last 30-day history (0–100; −1 = unknown) |
| `delta_at_entry` | Option delta (CE: 0–1, PE: −1–0) |
| `theta_daily_at_entry` | Daily theta in ₹ per unit at entry (negative) |
| `vega_at_entry` | Vega: ₹ change per 1% IV move per unit |

### Theta Drain Monitor

Runs every 15 seconds in the position monitor. If daily theta loss × quantity exceeds ₹50 **and** the position has not moved more than +5% into profit, a warning is broadcast to the dashboard and logged. Does not auto-exit — it alerts you to act.

### Daily Cap — Pre-Trade Risk Gate

Before each BUY, the bot projects the worst-case outcome:

```
potential_loss = LTP × quantity × stop_loss_pct
```

If `current_daily_pnl − potential_loss < −max_daily_loss`, the trade is **blocked before the order is placed** — not after. This closes the gap where a third simultaneous trade could push total losses past the daily cap even though `can_place_order()` showed green.

### EOD Strategy Performance Tracker

Win/loss results for every EOD Closing Momentum trade are persisted in `.eod_performance.json`. After **20 trades**, the bot logs a `CRITICAL` warning if win rate falls below 55%:

```
⚠️  EOD win rate: 48.0% over 23 trades (target ≥ 55%). Consider setting EOD_ENABLED=false.
```

Check current status any time: `iv status` in the chat console (shows EOD win rate alongside IV history).

### Daily P&L Persistence
Daily P&L is saved to `.daily_pnl.json` on each trade close. Survives bot restarts — the daily loss limit is never reset by restarting.

### Exchange-Level Stop Orders (GTT / Forever Orders)
Every entry automatically places a GTT/Forever Order at the exchange level. These survive bot crashes, internet outages, and system restarts.

---

## Available Commands

### Index Switching
```
index nifty          Switch to NIFTY 50 (lot: 65)
index banknifty      Switch to BANK NIFTY (lot: 30)
index sensex         Switch to SENSEX (lot: 20)
```

### Analysis
```
status               Bot status + current trend
analyze              Run immediate analysis
research             Smart strike recommendations
regime               Market regime: trending / ranging / volatile
mtf                  Multi-timeframe (5m + 15m + 1h) confluence
gap                  Today's gap-up/gap-down status
orb                  Opening Range Breakout levels
vwap                 VWAP Mean Reversion status
lateday              Late-Day Oscillation status (NIFTY)
iv status            IV history days per symbol + EOD win rate
iv backfill [days]   Seed IV history from 30 days of India VIX (run once on day 1)
```

### Performance & Diagnostics
```
perf                 Full performance report (per-strategy win rate, Sharpe, recommendations)
perf reload          Re-read journal files and update Kelly sizing data
perf strategy        Per-strategy win rate / profit factor table
perf recs            Top 8 actionable recommendations (DISABLE / INCREASE / AVOID)
perf sizer           Current position sizer config and account balance
```

### Trading
```
buy CE [strike] [qty]    Buy Call (deep research first)
buy PE [strike] [qty]    Buy Put (deep research first)
sell <symbol>            Exit position by symbol
close all                Close all open positions
positions                Show open positions
daily / summary          Today's P&L summary
```

### Strike Monitoring
```
strike 25000 CE 17mar    Analyse specific strike
watch 25000 CE 17mar     Monitor every 30s
stop                     Stop monitoring
```

### Custom Rules
```
rule add if rsi < 30 then buy ce
rule add if trend is bearish then buy pe
rule list
rule enable/disable/remove <id>
```

### Settings
```
set sl <pct>         Override stop-loss %
set target <pct>     Override target %
set qty <n>          Override lot quantity
pause / resume       Pause/resume auto-trading
orb on/off           Enable/disable ORB
vwap on/off          Enable/disable VWAP
lateday on/off       Enable/disable Late-Day Oscillation
```

### Research Tools
```
backtest [1m|3m|6m]  Backtest strategy (win rate, Sharpe)
validate [index]     Walk-forward validation (detects Trend overfitting, per-strategy verdict)
theta <premium> <days>   Theta decay clock
stock RELIANCE       Stock technical analysis
screen bullish       Scan for bullish stocks
journal              Today's session summary
```

---

## Project Structure

```
nifty-trading-bot/
├── main.py                     # Entry point (single-instance guard on port 8000)
├── config.py                   # All configuration (Dhan, Zerodha, Trading, VWAP, ORB, EOD, Gap,
│                               #   FilterConfig, PositionSizerConfig, LateDayConfig)
├── requirements.txt
├── .env                        # Credentials & settings (never commit)
├── pytest.ini                  # asyncio_mode=auto
├── README.md
├── pre_live_checklist.py       # ⭐ Go/no-go health check before enabling live trading (11 checks)
├── analyze_paper_trades.py     # Paper trade analysis: stats, per-strategy, exit reasons, recommendations
├── analyze_time_stop.py        # Time-stop opportunity-cost analysis (actual vs potential P&L)
├── visualize_time_impact.py    # Charts: actual vs potential P&L by exit type (matplotlib + ASCII fallback)
├── check_gap.py                # Gap strategy diagnostic: today's gaps, 8 scenario tests, 30-day history
├── check_eod.py                # EOD strategy diagnostic: config, live candle check, win/loss history
├── bot/
│   ├── engine.py               # Core orchestration: 6 strategies, 3-index scan, daily slots
│   ├── trend_analyzer.py       # 15-indicator trend scoring with adaptive ATR threshold
│   ├── order_manager.py        # Orders, risk, daily P&L persistence, GTT, AMO
│   ├── orb_strategy.py         # ORB strategy with min range width + 2-close confirmation
│   ├── vwap_strategy.py        # VWAP Mean Reversion (tightened thresholds)
│   ├── gap_detector.py         # Gap up/down detection & trading (enhanced: EXHAUSTION/RUNAWAY/BREAKAWAY)
│   ├── late_day_oscillation.py # Late-Day Oscillation strategy (14:30–15:25 IST, NIFTY)
│   ├── entry_filters.py        # HighProbabilityFilter — 8-gate 100-point signal scoring
│   ├── exit_manager.py         # ExitManager — 5-layer exit: SL/MultiTarget/Trail/Time/Volatility
│   ├── position_sizer.py       # PositionSizer — Kelly Criterion + confidence-based sizing
│   ├── performance_optimizer.py# PerformanceOptimizer — journal analysis, Sharpe, recommendations
│   ├── market_regime.py        # Regime detection (ADX + ATR + Hurst exponent)
│   ├── multi_timeframe.py      # Multi-timeframe confluence (5m/15m/1h)
│   ├── index_config.py         # NIFTY / BANKNIFTY / SENSEX configs
│   ├── market_research.py      # Smart strike and stock analysis
│   ├── instructions.py         # Custom rules engine
│   ├── backtester.py           # Strategy backtesting (7-indicator scoring)
│   ├── theta_clock.py          # Theta decay calculator
│   ├── trade_journal.py        # Trade history and reporting
│   ├── nse_scraper.py          # NSE option chain data
│   ├── bse_scraper.py          # BSE/SENSEX option chain data
│   ├── iv_monitor.py           # IV percentile filter + Black-Scholes Greeks (no scipy needed)
│   ├── straddle_strategy.py    # Long Straddle/Strangle strategy
│   ├── paper_trader.py         # Paper trading tracker (entries, P&L, exits)
│   └── strategy_validator.py   # Walk-forward backtest + overfitting detector for all 5 strategies
├── browser/
│   ├── dhan.py                 # DhanBroker: Dhan API, Forever Orders, AMO
│   ├── zerodha.py              # ZerodhaKite: Kite API + browser automation fallback
│   └── factory.py              # create_broker()
├── tests/
│   ├── test_dhan_order_flows.py        # Dhan orders, GTT, AMO, symbol matching, SL logic, profit tiers
│   ├── test_gtt_flows.py               # Forever Order flows
│   ├── test_zerodha.py                 # Kite API, browser fallback, position tracking
│   └── test_profitable_system.py       # 75 tests: entry_filters, exit_manager, position_sizer, performance_optimizer
├── web/
│   ├── app.py                  # FastAPI REST endpoints
│   ├── websocket.py            # WebSocket real-time chat
│   └── static/index_v2.html   # Dashboard UI
└── journals/                   # Daily trade logs (JSON) — used by PerformanceOptimizer for Kelly data
```

---

## Daily Usage

### Start the Bot
```powershell
.venv\Scripts\Activate.ps1
python main.py
```

### At End of Day
```
Ctrl+C
```

Or if running in background:
```powershell
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
```

### End-of-Day Analysis
```powershell
# Full paper trade report (overall stats, per-strategy, exit reasons, recommendations)
python analyze_paper_trades.py

# Check if time-stop is costing you potential profits
python analyze_time_stop.py

# Visual P&L comparison chart (saves time_stop_impact.png)
python visualize_time_impact.py --ascii   # ASCII (no extra deps)
python visualize_time_impact.py           # matplotlib charts
```

### Pre-Market Checks
```powershell
# Check today's opening gap for all 3 indices
python check_gap.py

# Verify all systems before going live
python pre_live_checklist.py
```

### During 14:30–15:00 IST (EOD Window)
```powershell
# Live EOD candle monitor — re-checks every 60 seconds
python check_eod.py --live

# Check EOD win/loss history
python check_eod.py --perf
```

### Zerodha — Daily Token Refresh (8:45 AM)
```powershell
.venv\Scripts\Activate.ps1
python get_kite_token.py   # automated: logs in, does 2FA, writes token to .env
python main.py
```

### Dhan — Token Expiry
Dhan tokens are long-lived (~1 year). When expired:
1. Log in to **https://developer.dhanhq.co**
2. Click your app → **Regenerate Token**
3. Update `DHAN_ACCESS_TOKEN` in `.env`
4. Restart the bot

---

## Running Unattended

```powershell
# Prevent sleep while plugged in (one-time)
powercfg /change standby-timeout-ac 0

# Start background script
.\start_bot_background.ps1
```

Lock your screen — the bot keeps running.

### Mobile Access

```powershell
# Allow port (run as Administrator, one-time)
New-NetFirewallRule -DisplayName "NIFTY Trading Bot" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

Find your PC's IP: `ipconfig` → look for **IPv4 Address** (e.g. `192.168.1.100`)

From your phone (same Wi-Fi): `http://192.168.1.100:8000`

---

## Running Tests

```powershell
.venv\Scripts\Activate.ps1
pip install pytest pytest-asyncio   # one-time
pytest tests/ -v
```

**257 tests — all passing ✅**

| Test File | Tests | Coverage |
|-----------|-------|---------|
| `test_dhan_order_flows.py` | ~8 | Dhan orders, GTT, AMO, symbol matching, SL logic, profit tiers |
| `test_gtt_flows.py` | — | Forever Order flows |
| `test_zerodha.py` | ~47 | Kite API, browser fallback, symbol building, position tracking |
| `test_profitable_system.py` | 75 | Entry filter, exit manager, position sizer, performance optimizer |

---

## Broker Comparison

| Feature | Dhan | Zerodha |
|---------|------|---------|
| API Cost | **Free** | ₹2,360/month |
| Token refresh | Long-lived (~1 year) | Daily via `get_kite_token.py` |
| GTT/Stop orders | ✅ Forever Orders | ✅ GTT Orders |
| AMO orders | ✅ Supported | ❌ Not implemented |
| Recommended for | All users | High-volume / existing Zerodha users |

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Bot state, P&L, positions |
| GET | `/api/positions` | Open positions |
| GET | `/api/trades` | All day trades |
| GET | `/api/compare-indices` | NIFTY vs BANKNIFTY vs SENSEX |
| GET | `/api/health-score` | Bot health score (0–100) + grade + issues |
| POST | `/api/login` | Trigger broker login |
| POST | `/api/start` | Start analysis loop |
| POST | `/api/stop` | Stop analysis loop |
| POST | `/api/command` | Execute chat command |
| WS | `/ws` | WebSocket live chat |
| GET | `/health` | Health check |

---

## Technical Indicators

| Indicator | Period | Use |
|-----------|--------|-----|
| SMA | 20 / 50 | Trend direction |
| EMA | 9 / 21 | Fast momentum |
| RSI | 14 | Overbought/oversold |
| MACD | 12/26/9 | Momentum |
| Bollinger Bands | 20 / 2σ | Support/resistance |
| ATR | 14 | Volatility (adaptive threshold) |
| Supertrend | 10 / 3.0 | Trend confirmation |
| VWAP | Intraday | Institutional price reference |
| ADX | 14 | Trend strength (regime) |
| Hurst exponent | 60 days | Trending vs mean-reverting |

---

## Capital Requirements

| Mode | Capital | Settings |
|------|---------|---------|
| Testing / watch-only | Any | `TRADING_AUTO_TRADE_ENABLED=false` |
| Conservative (start here) | ₹30,000+ | 1 lot, `MAX_DAILY_LOSS=2000` |
| **Current config (₹45k)** | **₹45,000** | **1 lot, MAX_LOSS=₹2,250, MAX_DAILY=₹4,500, MAX_TRADES=3** |
| Normal | ₹75,000+ | 1 lot per index, default settings |
| Aggressive | ₹1,50,000+ | 2 positions, higher limits |

**Recommended first week `.env`:**
```env
TRADING_AUTO_TRADE_ENABLED=true
TRADING_DEFAULT_QUANTITY=65      # 1 NIFTY lot
TRADING_MAX_POSITIONS=1
TRADING_MAX_LOSS_PER_TRADE=1000
TRADING_MAX_DAILY_LOSS=2000
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError` | `pip install -r requirements.txt` |
| Port 8000 in use | `taskkill /F /IM python.exe /T` then restart |
| Dhan 401 Unauthorized | Regenerate token at developer.dhanhq.co |
| Zerodha token expired | `python get_kite_token.py`, ensure `ZERODHA_PIN` is set |
| Signal score too low | `perf recs` in console — shows which filters are blocking. Set `FILTER_MIN_CONFIDENCE_SCORE=60` temporarily to diagnose. |
| No trades executing | Check `TRADING_AUTO_TRADE_ENABLED=true`, market hours, daily loss limit, RSI guard (ORB blocks CE<55 / PE>45 is now fixed ≥75/≤25) |
| Gap trade not firing | Ensure `GAP_ENABLED=true`, check logs — fires only 9:15–9:30 AM |
| EOD trade not firing | Check `EOD_ENABLED=true`, ensure the 14:30 candle body ≥ 50% |
| IV filter blocking all entries | Set `TRADING_IV_FILTER_ENABLED=false` until `.iv_history.json` has 5+ days of data |
| Theta drain warning every cycle | Normal — informational only. Adjust `TRADING_THETA_EXIT_THRESHOLD` or set `TRADING_THETA_EXIT_ENABLED=false` |
| Trade blocked "potential loss would breach daily cap" | Expected after earlier losses — the pre-trade risk gate blocks trades that would exceed `TRADING_MAX_DAILY_LOSS` even before the SL fires. Either wait for tomorrow or lower quantity. |
| EOD win rate warning in logs | After 20 EOD trades, check `iv status` for EOD win rate. Set `EOD_ENABLED=false` if below 55%. |
| Winners closed early at 1 PM | Set `TRADING_TIME_EXIT_ONLY_LOSERS=true` and `TRADING_TIME_EXIT_MIN_PROFIT=50` in `.env`. Run `python analyze_time_stop.py` to quantify the impact. |
| Time-stop has no effect on losers | Confirm `TRADING_HARD_TIME_EXIT_HOUR=13` and `TRADING_TIME_EXIT_ONLY_LOSERS=true` in `.env`. Restart the bot. |
| P&L shows wrong value | Verify broker API connection (green badge in dashboard) |
| `asyncio` test errors | `pip install pytest-asyncio` |
| PowerShell script blocked | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |

---

## Changelog

### March 2026 — Session 7: Smart Time Exit · Paper Trade Analytics · Pre-Live Checklist

**Smart time-based exit — winners let run:**
- Previous behaviour: ALL positions entered before 13:00 IST were force-closed at 13:00 regardless of P&L
- Analysis (`analyze_time_stop.py`) showed 4/6 paper trades closed by time stop with only ₹11.76 total profit — vs ₹2,062 potential (99.4% opportunity cost)
- New behaviour: positions already profitable (P&L ≥ `TRADING_TIME_EXIT_MIN_PROFIT`) are **skipped** at the hard-exit boundary; only losing/breakeven positions are closed
- Applies to both paper trading (`bot/paper_trader.py`) and live trading (`bot/engine.py`)
- New `.env` settings:
  ```env
  TRADING_TIME_EXIT_ONLY_LOSERS=true   # only close losers at hard-exit time
  TRADING_TIME_EXIT_MIN_PROFIT=50      # ₹ threshold — positions above this skip the time stop
  ```
- New `TradingConfig` fields: `time_exit_only_losers: bool` and `time_exit_min_profit: float`
- `PaperTrader.configure()` now accepts these settings; engine passes them from config at startup

**New analysis tool — `analyze_time_stop.py`:**
- Reads `paper_trades.json` and shows per-trade actual P&L vs potential P&L (if held to target)
- Calculates total opportunity cost from time-stop exits and severity classification (HIGH/MODERATE/LOW)
- Detects whether smart time-exit is already applied in `.env` and shows ✅ / ❌ status
- Usage: `python analyze_time_stop.py` (full report) or `--summary` (totals + recommendation)

**New analysis tool — `visualize_time_impact.py`:**
- Visual comparison: actual P&L vs potential P&L by exit type
- Three charts: total P&L by exit type, average per-trade, and opportunity-cost waterfall
- Saves PNG to `time_stop_impact.png`; falls back to ASCII bar chart if matplotlib not installed
- Usage: `python visualize_time_impact.py` / `--ascii` (force ASCII) / `--save` (PNG only, no window)

**New tool — `pre_live_checklist.py` — comprehensive go/no-go check:**
- 11-section health check against the real codebase — no mock classes
- Checks: configuration (capital, SL/target, R:R), broker credentials (Dhan JWT/Zerodha daily token), paper trading results (win rate, profit factor, per-strategy), risk management (trailing stop, profit tiers, VIX/IV/theta filters), time-exit rules, strategy enables/config, live market data (yfinance — NIFTY/BANKNIFTY/SENSEX/VIX), alerts (Telegram), files & storage, OrderManager import test, live system snapshot
- Returns exit code 0 = READY, 1 = NOT READY (scriptable)
- Usage: `python pre_live_checklist.py` (full, fetches live VIX) / `--fast` (skip yfinance)
- Current result: **50 passed / 0 critical / 6 warnings**

**New diagnostic scripts (from previous session):**
- `check_gap.py` — gap strategy diagnostic using real `bot/gap_detector.py` API; includes 8 built-in scenario tests (`--scenarios`) and 30-day history (`--history`). All 8/8 scenarios pass.
- `check_eod.py` — EOD strategy diagnostic; shows config, time window status, historical win/loss from `.eod_performance.json`, and live 15m candle check. `--live` loops every 60 s during 14:30–15:00 IST.
- `analyze_paper_trades.py` — comprehensive paper trade analysis: overall stats, per-strategy breakdown, exit-reason analysis, time-of-day chart, CE vs PE breakdown, recommendations. Verified on 6 live trades (₹+2,023 P&L, 100% win rate).

**Paper trading improvements:**
- `PaperTrader.check_exits()` now stores live unrealised P&L back to `pos["pnl"]` every cycle (was always 0)
- `PaperTrader.get_summary()` returns `realized_pnl`, `unrealized_pnl`, and combined `total_pnl`
- `PaperTrader.export_to_csv()` auto-called after every closed trade → `paper_trades_export.csv`

**Dashboard fixes (`web/app.py` + `web/static/index_v2.html`):**
- Analysis loop always auto-starts in paper mode (was only starting when `auto_trade_enabled=true`)
- `/api/status` now returns `regime`, `india_vix`, `trend` (were missing → dashboard showed "—")
- `/api/pnl`, `/api/trades`, `/api/positions` all have paper-mode early returns serving real paper data
- Dashboard: regime/VIX/trend DOM updates; Chart.js 4.4 P&L sparkline; toast notifications; theme toggle (🌙/☀️); live uptime badge; log-level colour coding; paper trade labels (📋 PAPER OPEN / TARGET HIT / SL HIT)
- Fixed JS duplicate code bug: 18-line orphaned trade-rendering block removed

**EOD enabled:**
- `EOD_ENABLED=true` set in `.env` (was `false`)
- EOD Closing Momentum now fires 14:30–15:00 IST on strong directional candles

**VIX threshold raised:**
- `TRADING_VIX_MAX`: 25 → **27** (India VIX was 25.03, previously blocking all entries)

---

### March 2026 — Session 6: Architecture Review + Bug Fixes

**Critical bug — ORB RSI guard was inverted:**
- Guard was blocking CE when RSI ≥ 55 and PE when RSI ≤ 45
- Those are exactly the RSI values present during a *confirmed* breakout — the check was silently killing nearly every valid ORB signal
- Fixed: now blocks CE when RSI ≥ 75 (overbought/extended) and PE when RSI ≤ 25 (oversold/exhausted)

**Dead code removed — ORB "Guard 3":**
- A NEUTRAL-trend check `if _trend == "NEUTRAL" and strength < 65` was unreachable because Guard 1 (`strength < 65 → return`) already caught it for all trends
- Deleted; Guard 1 threshold (65%) handles all cases correctly

**Stale duplicate code removed — `strategy_validator.py`:**
- A fragment of old `_GapSim.simulate()` code (part of the pre-enhanced gap logic) was left after the first `return trades`, causing orphaned bare keyword arguments after the method end
- Deleted; the enhanced path's `return trades` is now the only return

**`account_balance` added to `TradingConfig`:**
- `getattr(settings.trading, "account_balance", 100_000)` was always falling back to ₹1,00,000 because the field didn't exist in the config class
- Added `account_balance: float = Field(default=100_000.0)` with env prefix `TRADING_ACCOUNT_BALANCE`
- Position sizer now uses real capital for Kelly calculations — set via `.env`

**Architecture review findings documented:**
- `datetime.now()` (no timezone) vs `ZoneInfo("Asia/Kolkata")` — engine uses naive time throughout, which breaks on UTC servers; `order_manager._is_market_hours()` is correct
- `_last_checked` heartbeat timestamps only updated in paper mode; auto-mode always shows `ORB=never VWAP=never`
- VIX filter is applied in `_execute_option_with_research()` but bypassed in `_execute_orb_direct()`
- VWAP not checked during trending regime when signal is directional (only when ADX > 50 + neutral signal)

---

### March 2026 — Session 5: Profitable Entry/Exit System + Late-Day Oscillation + Gap System

**New module — `bot/late_day_oscillation.py`:**
- Late-Day Oscillation strategy detecting mean-reversion / oscillation patterns in 14:30–15:25 window
- NIFTY-only; max 1 signal per day; disabled by default (`LATE_DAY_ENABLED=false`)
- Validate before enabling: `python analysis/late_day_oscillation_validator.py`

**New module — `bot/entry_filters.py` — HighProbabilityFilter:**
- 8-gate 100-point scoring system applied before any trade entry
- Gates: Volume (15pts), Trend alignment (20pts), Indicator confluence (15pts), S/R respect (15pts), Historical win rate (10pts), Optimal time window (10pts), VIX/Volatility (10pts), Calendar events (5pts)
- Minimum passing score: 70/100 (configurable via `FILTER_MIN_CONFIDENCE_SCORE`)
- `build_market_data(**kwargs)` convenience helper for constructing the `market_data` dict

**New module — `bot/exit_manager.py` — 5-component ExitManager:**
- `DynamicStopLoss`: ORB=opposite range end, VWAP/Trend=2×ATR, GAP_FADE=0.8×ATR, LATE_DAY=0.15% fixed
- `MultiTargetExit`: 40% at 1R, 30% at 2R, 20% at 3R, 10% runner with trailing; SL auto-moves to BE after T1
- `TrailingStop`: activates at +20% profit; give-back regime: 50%→40%→30% as profit grows
- `TimeBasedExit`: ORB=2hr, VWAP=1hr, LATE_DAY=12min; losers exit 14:30; force-exit 15:15 IST
- `VolatilityExit`: VIX spike +20% + profit>15%→exit; ATR expansion >1.5× on scalps→exit; VIX>25 no-profit→exit
- `ExitManager`: master coordinator per trade; supports `current_time` override for testing

**New module — `bot/position_sizer.py` — Kelly Criterion Sizer:**
- Kelly formula: `f = (b·p − q) / b` where b=avg_win/avg_loss, p=win_rate; 25% fractional Kelly
- Confidence → size multipliers: 90+→1.3×, 80+→1.1×, 70+→1.0×, 60+→0.8×, <60→0.5×
- Hard cap: implied risk (entry×qty×SL%) ≤ 1.5× max_risk_₹
- Kelly only activates after ≥20 historical trades per strategy; defaults to 1.0× until then
- `explain()` method logs full sizing rationale for every trade

**New module — `bot/performance_optimizer.py` — PerformanceOptimizer:**
- Loads `journals/journal_YYYY-MM-DD.json` files at startup for historical context
- Computes per-strategy: win rate, profit factor, avg win/loss, annualised Sharpe ratio
- Per-hour heatmap: shows which IST hours have highest/lowest win rates
- Generates prioritized recommendations: DISABLE if WR<45%, INCREASE if WR≥65% & PF≥1.8, AVOID if WR<40% in a specific hour
- Singleton via `get_performance_optimizer()` — one instance shared across engine and sizer
- Every closed trade is fed in via `perf_optimizer.add_trade(trade_record)` then Kelly data refreshed

**`config.py` additions:**
- `FilterConfig` — `FILTER_` env prefix; controls all 8 gate enable flags + `min_confidence_score`
- `PositionSizerConfig` — `POSITION_SIZER_` prefix; `max_risk_per_trade_pct`, `kelly_fraction`, `min_kelly_trades`, `max_qty_multiplier`
- Both added to `Settings` as `entry_filter` and `position_sizer`

**`bot/engine.py` integration:**
- Imports: `HighProbabilityFilter`, `ExitManager`, `DynamicStopLoss`, `PositionSizer`, `PerformanceOptimizer`
- `__init__`: `self.entry_filter`, `self.dynamic_sl`, `self.position_sizer` (lazy), `self.perf_optimizer`, `self._exit_managers` dict
- `start()` / `initialize()`: creates `PositionSizer` with real account balance + Kelly data from journals
- Position monitor: `perf_optimizer.add_trade()` + `position_sizer.update_stats()` on every close
- New `perf` command with subcommands: `perf`, `perf reload`, `perf strategy`, `perf recs`, `perf sizer`

**Enhanced Gap system:**
- `_GapSim` now classifies: EXHAUSTION (>2.5%) → GAP_FADE; RUNAWAY (1.5–2.5%) / BREAKAWAY (0.8–1.5%) → GAP_CONTINUATION; COMMON (<0.8%) → skip
- `detect_all_gaps()` and `calculate_gap_fill_stats()` methods for reporting fill rates by category

**Test baseline: 257/257 tests passing** (~11.5s)

---

### March 2026 — Session 4: Daily Cap Risk Gate + EOD Tracker + IV Backfill + Health Score

**Daily cap pre-trade risk gate:**
- `can_place_order()` checked *current* P&L but couldn't prevent a third trade from overshooting the cap
- Now, before every BUY, the bot computes `potential_loss = LTP × qty × SL%` and projects forward
- If `current_daily_pnl − potential_loss < −max_daily_loss` → trade is blocked with a clear log message
- Closes the reviewer-identified gap where three average-SL trades could total ₹5,167 against a ₹4,500 cap

**EOD performance tracker (`_eod_tracker`):**
- Persists every EOD Closing Momentum trade result (win/loss) to `.eod_performance.json`
- After **20 trades**, logs `CRITICAL` if win rate < 55%
- Records on every auto-close path: SL/target exits and the 3:27 PM market-close force-exit
- Check via `iv status` command in the chat console

**IV history backfill (`iv_monitor.backfill_from_vix()`):**
- Seeds `.iv_history.json` from 30 days of India VIX history via yfinance (India VIX ≈ NIFTY ATM IV)
- Run on day 1 via chat command: `iv backfill`
- Only writes missing dates — never overwrites real option-chain data
- After backfill, flip `TRADING_IV_FILTER_ENABLED=true` immediately (no 5-day wait)

**Bot health score (`/api/health-score`):**
- New REST endpoint returning a 0–100 score with grade (A/B/C/F) and issue list
- Factors: win rate, IV history days, consecutive losses, critical theta on open positions, avg slippage, EOD win rate
- Example response: `🟢 Bot Health: 85/100 (Grade A)`

**`iv` chat command:**
- `iv status` — shows IV history days per symbol + EOD win rate
- `iv backfill [days]` — seeds IV history from India VIX (default 30 days)

---

### March 2026 — Session 3: IV Percentile Filter + Greeks Monitor + Trade Analytics

**New module — `bot/iv_monitor.py`:**
- Black-Scholes Greeks calculator (pure Python — no scipy dependency): Delta, Gamma, Theta (₹/day), Vega (₹ per 1% IV)
- Dividend-yield corrected formula (1.2% for NIFTY/BANKNIFTY); handles same-day expiry (DTE=0.5)
- Rolling 30-day ATM IV history stored in `.iv_history.json` (auto-populated from day 1)
- IV percentile rank: `count(history < current_iv) / total * 100`; returns −1 if fewer than 5 days of history

**IV Percentile filter** (before every auto-entry):
- Fetches ATM IV from NSE/BSE option chain; falls back to India VIX as proxy
- Blocks entry if IV percentile ≥ 80th pct — premiums too expensive relative to recent history
- `TRADING_IV_FILTER_ENABLED=false` by default; flip to `true` after first week (needs history)
- Insufficient history (<5 days) → allows entry with a log warning (never blocks cold-start)

**Greeks + IV stamped on every trade record at entry:**
- `iv_pct_at_entry`, `iv_percentile_at_entry`, `delta_at_entry`, `theta_daily_at_entry`, `vega_at_entry`
- Over time: compare avg VIX/IV of winners vs losers; group by strategy source

**Slippage tracking:**
- Pre-order LTP captured before each BUY; `trade.slippage = fill_price − pre_order_ltp`
- Slippage > ₹5 triggers a `WARNING` log — highlights which time/strategy has worst fills
- EOD slippage (2:30–3 PM thin market) will be clearly identifiable in logs

**Theta drain monitor** (position monitor, every 15 s):
- Computes live theta from current IV + DTE for each open position
- Broadcasts a dashboard warning if daily drain > ₹50 AND P&L < +5%
- Warning-only — no auto-exit; informs decision without overriding discretion

**Hard 1 PM exit — 15-minute minimum hold:**
- A 12:59 entry is no longer force-closed 1 minute later
- Added `position_age ≥ 15 minutes` guard to the hard-exit victim filter

**EOD experimental warning:**
- Logs `⚠️  EOD: EXPERIMENTAL` once per EOD signal fire (not every cycle)
- Reminder to monitor win rate and disable if <55% over 20+ days

---

### March 2026 — Session 2: Capital Calibration + Exit Management Overhaul

**Capital calibrated for ₹45,000:**
- `MAX_LOSS_PER_TRADE`: ₹2,500 → **₹2,250** (5% of capital)
- `MAX_DAILY_LOSS`: ₹5,000 → **₹4,500** (10% of capital)
- `MAX_TRADES_PER_DAY`: 4 → **3**
- `MAX_CONSECUTIVE_LOSSES`: 3 → **5**
- `PAUSE_AFTER_LOSSES_MINUTES`: 30 → **60 min**

**Exit management overhaul:**
- Profit tiers recalibrated: T1 **+15% → +30%**, T2 **+25% → +60%** (larger options moves needed to justify exits)
- Tight trail after both tiers hit: trail **tightens from 12% → 5%** (`tight_trail_after_tier2_pct`)
- `tier2_exited` field on `TradeRecord` tracks when to switch to tight trail
- **Hard 1 PM exit**: positions entered before 13:00 IST are force-closed at 13:00 (thin afternoon liquidity)
- **India VIX filter**: auto-entries blocked when India VIX > 20 (fetched via yfinance before every entry); manual trades bypass

**Adaptive time-stops per strategy source:**
- Every trade now carries a `source` field (GAP / ORB / VWAP / Auto / EOD / Rule / Manual)
- Time-stop is no longer flat 45 min — applied per source: GAP=60, ORB=45, VWAP=30, Auto=90, EOD=15

**ORB late-entry time decay:**
- After 11:00 AM, ORB breakout strength loses **2 pts per 6-minute slot** (max −20 pts, floor at 60%)
- Prevents low-quality late-morning breakouts that have less time to play out
- Formula: `strength = max(60.0, raw_strength − min(20, (late_minutes // 6) × 2))`

**EOD candle bug fix:**
- `_check_eod_signal()` was using `iloc[-2]` which picks wrong candle when yfinance lags
- Fixed: now filters candles by IST-aware timestamp (`candle_ts + 15min ≤ now_ist`), falls back to `iloc[-2]` only on timezone error

---

### March 2026 — Session 1: Quality Overhaul

**Bug fixes (runtime crashes):**
- Fixed `AttributeError` when typing `screenshot` command (Dhan has no `get_screenshot()`)
- Fixed `AttributeError` on `/api/login` endpoint — Dhan uses API key, no interactive login needed
- `order_manager.py` no longer imports `ZerodhaKite` at startup when `BROKER=dhan` (was loading Playwright unnecessarily)

**Dead code removed:**
- `get_kite_token.py` deleted (Kite OAuth script — dead for Dhan)
- `monitor.py` deleted (duplicate of `watchdog_monitor.py`)
- `DailyStats.gross_pnl` field removed (was declared but never written)
- `TrendAnalyzer.get_last_signal()` removed (was never called)
- `backtester._compute_signal()`: removed two `False # stoch_rsi placeholder` entries; divisor corrected `/8 → /7`
- `theta_clock.calculate()`: removed unused `iv_pct` parameter; removed unreachable `else time_value` branch

**False-entry prevention:**
- VWAP deviation threshold: `0.4%` → **`0.6%`** (0.4% = 92 pts at NIFTY 23k — pure noise)
- VWAP RSI oversold/overbought: `42/58` → **`38/62`** (42/58 was nearly neutral, not an extreme)
- VWAP minimum confidence gate: **60% required** (ensures volume spike or deep RSI, not just threshold crossing)
- Auto-trend-trade **14:00 IST cutoff**: no new trend entries after 2 PM (thin liquidity, time-stop kills them anyway)
- Auto-trend-trade now marks **both ORB and VWAP slots** on success (prevents a second sequential auto-entry after SL)
- ORB minimum opening range width: **≥75 pts NIFTY / ≥150 pts BANKNIFTY/SENSEX** (sub-threshold ranges are coin-flips)

**New strategy — EOD Closing Momentum:**
- Fires once per index between 14:30–15:00 IST
- Reads the last completed 15-minute candle
- Requires candle body ≥ 50% of high-low range (doji/indecision candles skipped)
- Trend-direction alignment enforced (counter-trend blocked)
- Validated March 18, 2026: NIFTY −72 pts, BANKNIFTY −151 pts, SENSEX −209 pts — all 3 correct from 14:30 candle

**Daily P&L persistence:**
- P&L now saved to `.daily_pnl.json` on every trade close
- Bot restart no longer resets daily P&L to zero (was a loophole for the daily loss limit)

---

## Disclaimer

**THIS SOFTWARE IS PROVIDED AS IS WITHOUT WARRANTY OF ANY KIND.**

- Trading is risky — you can lose money
- Past performance does not guarantee future results
- The authors are NOT responsible for financial losses
- Always test in signal-only mode (`TRADING_AUTO_TRADE_ENABLED=false`) for at least 1–2 weeks before enabling live trades
