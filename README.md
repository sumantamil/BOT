# NIFTY Options Trading Bot

A sophisticated automated trading bot for NIFTY, BANKNIFTY, and SENSEX options with **multi-broker support (Dhan + Zerodha)**, **5 intraday strategies**, **real-time P&L tracking**, and **Telegram alerts**.

---

## Quick Start

Already set up? Run every morning before 9:15 AM:

```powershell
.venv\Scripts\Activate.ps1
python main.py
```

Then open: **http://localhost:8000**

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

# ============ TRADING RISK SETTINGS ============
TRADING_AUTO_TRADE_ENABLED=false     # Set true only after testing

TRADING_MAX_POSITIONS=2
TRADING_MAX_LOSS_PER_TRADE=2500
TRADING_MAX_DAILY_LOSS=5000
TRADING_STOP_LOSS_PERCENTAGE=15
TRADING_TARGET_PERCENTAGE=30
TRADING_MAX_CONSECUTIVE_LOSSES=3
TRADING_PAUSE_AFTER_LOSSES_MINUTES=30
TRADING_MAX_TRADES_PER_DAY=4
TRADING_CLOSE_ALL_BEFORE_MARKET_CLOSE=3
TRADING_TIME_STOP_MINUTES=45        # exit positions open > 45 min without profit

# Smart exits
TRADING_USE_TRAILING_STOP_LOSS=true
TRADING_TRAILING_STOP_PERCENTAGE=12
TRADING_TRAILING_STOP_ACTIVATION_PCT=10
TRADING_USE_PROFIT_TIERS=true
TRADING_TAKE_PROFIT_TIER_1_PERCENT=15
TRADING_TAKE_PROFIT_TIER_1_QUANTITY_PERCENT=50
TRADING_TAKE_PROFIT_TIER_2_PERCENT=25
TRADING_TAKE_PROFIT_TIER_2_QUANTITY_PERCENT=50

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

### 5. EOD Closing Momentum *(new)*
- Fires once between **2:30–3:00 PM** per index per day
- Reads the last completed **15-minute candle**
- Requires candle body ≥ **50% of the candle's high-low range** (filters out doji/indecision)
- Bullish candle → Buy CE; Bearish candle → Buy PE
- Trend-direction alignment enforced (counter-trend trades blocked)
- Position exits at 3:27 PM force-close
- Validated on March 18, 2026: all 3 indices (NIFTY −72 pts, BANKNIFTY −151 pts, SENSEX −209 pts) predicted and traded correctly from the 14:30 candle

---

### Strategy Selection by Regime

| Regime | Active Strategies | Max Positions |
|--------|------------------|---------------|
| TRENDING UP/DOWN (ADX ≤ 50) | Trend + ORB + Gap + EOD | 2 |
| TRENDING UP/DOWN (ADX > 50) | Trend + ORB + VWAP + Gap + EOD | 2 |
| RANGING | VWAP + ORB + Gap + EOD | 2 |
| VOLATILE | ORB + Gap only | 1 |

---

## Risk Management

### Entry Guards (False Signal Prevention)

| Guard | Where | Detail |
|-------|-------|--------|
| Market regime filter | All strategies | Only trades TRENDING/VOLATILE conditions |
| Trend-direction alignment | ORB, VWAP, EOD | Blocks counter-trend entries |
| 15m MTF confluence | ORB | 15m candle must agree with ORB direction |
| RSI extreme filter | ORB | Blocks CE when RSI > 88, PE when RSI < 12 |
| ORB strength ≥ 75% | ORB | Minimum breakout confidence |
| ORB strength ≥ 65% (NEUTRAL) | ORB | Relaxed only on NEUTRAL trend days |
| ORB min range width | ORB | ≥ 75 pts NIFTY / ≥ 150 pts BANKNIFTY/SENSEX |
| VWAP deviation ≥ 0.6% | VWAP | Was 0.4% — tightened to cut noise |
| VWAP RSI thresholds 38/62 | VWAP | Was 42/58 — tightened for real extremes |
| VWAP confidence ≥ 60% | VWAP | Needs volume spike or deep RSI extreme |
| Auto-trade 14:00 cutoff | Auto | No trend entries after 2 PM |
| EOD body ≥ 50% | EOD | Doji / indecision candles skipped |
| 2 consecutive ORB closes | ORB | Requires 2 closes beyond trigger before entry |
| Strike-loss guard | OrderManager | Blocks re-entry on a strike already lost today |
| Over-sell guard | OrderManager | Prevents naked shorts |

### Position Limits

- Max 2 open positions at any time (`TRADING_MAX_POSITIONS=2`)
- Both ORB and VWAP slots tracked per-index per-day independently
- Auto-trade consumes **both** slots on success — no double-entry after SL
- Max 4 trades per day total across all indices

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
```

### Research Tools
```
backtest [1m|3m|6m]  Backtest strategy (win rate, Sharpe)
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
├── config.py                   # All configuration (Dhan, Zerodha, Trading, VWAP, ORB, EOD, Gap)
├── requirements.txt
├── .env                        # Credentials & settings (never commit)
├── pytest.ini                  # asyncio_mode=auto
├── README.md
├── bot/
│   ├── engine.py               # Core orchestration: 5 strategies, 3-index scan, daily slots
│   ├── trend_analyzer.py       # 15-indicator trend scoring with adaptive ATR threshold
│   ├── order_manager.py        # Orders, risk, daily P&L persistence, GTT, AMO
│   ├── orb_strategy.py         # ORB strategy with min range width + 2-close confirmation
│   ├── vwap_strategy.py        # VWAP Mean Reversion (tightened thresholds)
│   ├── gap_detector.py         # Gap up/down detection & trading
│   ├── market_regime.py        # Regime detection (ADX + ATR + Hurst exponent)
│   ├── multi_timeframe.py      # Multi-timeframe confluence (5m/15m/1h)
│   ├── index_config.py         # NIFTY / BANKNIFTY / SENSEX configs
│   ├── market_research.py      # Smart strike and stock analysis
│   ├── instructions.py         # Custom rules engine
│   ├── backtester.py           # Strategy backtesting (7-indicator scoring)
│   ├── theta_clock.py          # Theta decay calculator
│   ├── trade_journal.py        # Trade history and reporting
│   ├── nse_scraper.py          # NSE option chain data
│   └── bse_scraper.py          # BSE/SENSEX option chain data
├── browser/
│   ├── dhan.py                 # DhanBroker: Dhan API, Forever Orders, AMO
│   ├── zerodha.py              # ZerodhaKite: Kite API + browser automationfallback
│   └── factory.py              # create_broker()
├── tests/
│   ├── test_dhan_order_flows.py   # 8 Dhan tests
│   ├── test_gtt_flows.py          # GTT / Forever Order tests
│   └── test_zerodha.py            # 47 Zerodha tests
├── web/
│   ├── app.py                  # FastAPI REST endpoints
│   ├── websocket.py            # WebSocket real-time chat
│   └── static/index_v2.html   # Dashboard UI
└── journals/                   # Daily trade logs (JSON)
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

**55 tests — all passing ✅**

| Test File | Tests | Coverage |
|-----------|-------|---------|
| `test_dhan_order_flows.py` | 8 | Dhan orders, GTT, AMO, symbol matching, SL logic, profit tiers |
| `test_gtt_flows.py` | — | Forever Order flows |
| `test_zerodha.py` | 47 | Kite API, browser fallback, symbol building, position tracking |

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
| No trades executing | Check `TRADING_AUTO_TRADE_ENABLED=true`, market hours, daily loss limit |
| Gap trade not firing | Ensure `GAP_ENABLED=true`, check logs — fires only 9:15–9:30 AM |
| EOD trade not firing | Check `EOD_ENABLED=true`, ensure the 14:30 candle body ≥ 50% |
| P&L shows wrong value | Verify broker API connection (green badge in dashboard) |
| `asyncio` test errors | `pip install pytest-asyncio` |
| PowerShell script blocked | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |

---

## Changelog

### March 2026 — Session Quality Overhaul

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
