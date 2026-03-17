# NIFTY Options Trading Bot

A sophisticated automated trading bot for NIFTY, BANKNIFTY, and SENSEX options with **multi-broker support (Dhan + Zerodha)**, **gap-up/gap-down strategy**, **AMO (After Market Orders)**, **real-time P&L tracking**, and **Telegram alerts**.

---

## Quick Start

Already set up? Run every morning before 9:15 AM:

**Dhan broker (recommended — default):**
```powershell
.venv\Scripts\Activate.ps1
python main.py   # no token refresh needed — token expires Apr 14, 2026
```

**Zerodha broker (optional):**
```powershell
.venv\Scripts\Activate.ps1
python get_kite_token.py   # refresh daily access token (fully automated)
python main.py             # start the bot
```

Then open: **http://localhost:8000**

---

## Initial One-Time Setup

### Step 1: Clone Repository
```powershell
git clone <repo-url>
cd nifty-trading-bot
```

### Step 2: Create Virtual Environment
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### Step 3: Install Dependencies
```powershell
pip install -r requirements.txt

# Zerodha users only — install headless browser for daily token refresh:
playwright install chromium
```

Required packages:
- `fastapi` + `uvicorn` — Web server
- `dhanhq` — Dhan API SDK (**free broker, recommended**)
- `kiteconnect` — Zerodha REST API (optional)
- `playwright` — Browser automation (**Zerodha only**: token refresh + fallback)
- `pyotp` — TOTP 2FA auto-login (Zerodha, optional)
- `yfinance` — Market data
- `curl_cffi` — NSE option chain data (TLS bypass)
- `pandas`, `numpy` — Data processing
- `ta` — Technical indicators
- `pydantic` + `pydantic-settings` — Configuration
- `loguru` — Logging
- `python-dotenv` — `.env` file loading

### Step 4: Create `.env` File

Create `.env` in the project root:

```env
# ============ BROKER SELECTION ============
# 'dhan'     = Dhan API (free, permanent token) — RECOMMENDED
# 'zerodha'  = Kite Connect API (₹2,360/month)
BROKER=dhan

# ============ DHAN API (if BROKER=dhan) ============
# Get your token from: https://developer.dhanhq.co/apps
# Steps:
#   1. Log in to https://developer.dhanhq.co
#   2. Navigate to "My Apps" or use the direct link above
#   3. Click on your app (or create one if needed)
#   4. Copy the "Sandbox Client ID" and "Access Token" from the table
#   5. Paste below
# Token expires on the date shown in the portal (e.g., "14 Apr" = April 14, 2026)
# When expired, log in to the portal and click "Regenerate Token"
DHAN_CLIENT_ID=your_dhan_client_id
DHAN_ACCESS_TOKEN=your_dhan_access_token_from_portal
DHAN_MOBILE=your_10_digit_mobile         # for manual token regeneration
DHAN_PASSWORD=your_dhan_account_password # for manual token regeneration
DHAN_PORTAL_URL=https://developer.dhanhq.co

# ============ ZERODHA CREDENTIALS (if BROKER=zerodha) ============
ZERODHA_USER_ID=your_user_id_here
ZERODHA_PASSWORD=your_password_here
ZERODHA_PIN=your_6_digit_pin      # used by auto token refresh (2FA)
ZERODHA_TOTP_SECRET=              # optional: overrides PIN if set

# ============ KITE CONNECT API (if BROKER=zerodha) ============
# Get from: https://kite.trade/
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
KITE_ACCESS_TOKEN=                # auto-filled by get_kite_token.py
KITE_USE_KITE_API=true

# ============ TRADING CONFIGURATION ============
TRADING_AUTO_TRADE_ENABLED=false   # Set true after testing

# Safety
TRADING_MAX_POSITIONS=2
TRADING_MAX_LOSS_PER_TRADE=2500
TRADING_MAX_DAILY_LOSS=5000
TRADING_STOP_LOSS_PERCENTAGE=15
TRADING_TARGET_PERCENTAGE=30
TRADING_MAX_CONSECUTIVE_LOSSES=3
TRADING_PAUSE_AFTER_LOSSES_MINUTES=30
TRADING_MAX_TRADES_PER_DAY=4
TRADING_CLOSE_ALL_BEFORE_MARKET_CLOSE=3

# Smart Exits
TRADING_USE_TRAILING_STOP_LOSS=true
TRADING_TRAILING_STOP_PERCENTAGE=12
TRADING_USE_PROFIT_TIERS=true
TRADING_TAKE_PROFIT_TIER_1_PERCENT=15
TRADING_TAKE_PROFIT_TIER_1_QUANTITY_PERCENT=50
TRADING_TAKE_PROFIT_TIER_2_PERCENT=25
TRADING_TAKE_PROFIT_TIER_2_QUANTITY_PERCENT=50

# GTT / Forever Orders (exchange-level stop orders — survive bot crash/restart)
TRADING_USE_GTT=true

# AMO — After Market Orders (place gap trades during evening/overnight window)
# Window: 17:00–23:59 and 00:00–09:08 IST on weekdays
TRADING_AMO_ENABLED=true

# Market Filters
TRADING_AVOID_RSI_RANGE=true
TRADING_VOLATILITY_THRESHOLD=2.0
TRADING_AVOID_LOW_VOLUME_HOURS=true

# Trade Frequency
TRADING_MIN_TIME_BETWEEN_TRADES_MINUTES=10  # min gap between consecutive trades

# Analysis
TREND_ANALYSIS_INTERVAL_SECONDS=60   # market scan interval in seconds

# ============ GAP STRATEGY ============
GAP_ENABLED=true
GAP_MIN_GAP_PCT=0.75          # moderate gap threshold
GAP_STRONG_GAP_PCT=1.5        # strong gap → immediate entry
GAP_STOP_LOSS_PCT=25
GAP_TARGET_PCT=50
GAP_QUANTITY_MULTIPLIER=1.0   # 1.0 = normal lot size

# ============ TELEGRAM ALERTS (Optional) ============
ALERT_TELEGRAM_ENABLED=false
ALERT_TELEGRAM_BOT_TOKEN=your_bot_token_here
ALERT_TELEGRAM_CHAT_ID=your_chat_id_here
```

> **Note on Lot Sizes**: The bot automatically uses the correct lot size per index.
> - **NIFTY** → 65 units/lot
> - **BANKNIFTY** → 30 units/lot
> - **SENSEX** → 20 units/lot

### Step 5: Configure Kite API Redirect URL — **Zerodha only**

1. Log in at [kite.trade](https://kite.trade/) → **My Apps** → select your app
2. Set **Redirect URL** to exactly: `http://127.0.0.1`
3. Save — without this, the automated token capture will time out

### Step 6: Get Kite API Access Token — **Zerodha only**

Add your Zerodha PIN to `.env`:
```env
ZERODHA_PIN=your_6_digit_pin
```

Then run the token refresher:
```powershell
python get_kite_token.py
```

This is **fully automated** — it launches a headless browser, logs into Zerodha, completes 2FA with your PIN (or TOTP if configured), captures the OAuth token, and writes it to `.env` automatically. No manual steps needed.

> **Access tokens expire every day at midnight IST.** Run `get_kite_token.py` every morning before 9:15 AM.

If you have a TOTP app set up for Zerodha, set `ZERODHA_TOTP_SECRET` instead of `ZERODHA_PIN` for even more reliable 2FA automation.

---

## Telegram Alerts (Optional)

1. Open Telegram → search `@BotFather` → `/newbot` → copy token
2. Send any message to your new bot
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy `id` from `"chat"`
4. Add both to `.env` and set `ALERT_TELEGRAM_ENABLED=true`
5. Restart the bot

---

## Daily Usage

### Daily Timing Schedule (IST)

**Dhan broker (no token refresh needed):**

| Time | Action |
|---|---|
| **8:45 AM** | Start bot: `python main.py` |
| **9:15 AM** | Market opens — gap detector fires, all strategies activate |
| **9:15–9:30 AM** | Gap Up/Down strategy window (fires if gap > 0.75%) |
| **9:30–11:30 AM** | ORB (Opening Range Breakout) entry window |
| **9:15 AM – 3:30 PM** | VWAP & Trend Following active |
| **3:27 PM** | Bot auto-closes all open positions (3 min before close) |
| **3:30 PM** | Market closes — no new trades |
| **5:00 PM – 9:08 AM** | AMO window: gap orders placed for next day open |

**Zerodha broker (daily token refresh required):**

| Time | Action |
|---|---|
| **8:45 AM** | Run `get_kite_token.py` — refresh daily access token |
| **8:55 AM** | Run `python main.py` — start the bot (20 min before open) |
| **9:15 AM** | Market opens — gap detector fires, all strategies activate |
| **9:15–9:30 AM** | Gap Up/Down strategy window (fires if gap > 0.75%) |
| **9:30–11:30 AM** | ORB (Opening Range Breakout) entry window |
| **9:15 AM – 3:30 PM** | VWAP & Trend Following active |
| **3:27 PM** | Bot auto-closes all open positions (3 min before close) |
| **3:30 PM** | Market closes — no new trades |

> The auto-close time is controlled by `TRADING_CLOSE_ALL_BEFORE_MARKET_CLOSE=3` in `.env`.

### Every Morning (Dhan — 8:45 AM IST)
```powershell
.venv\Scripts\Activate.ps1
python main.py   # no token refresh needed until expiry date
```

### When Dhan Token Expires (Check portal for expiry date)
1. Log in to https://developer.dhanhq.co/apps
2. Click your app → Click "Regenerate Token" button
3. Copy the new token → paste into `.env` → `DHAN_ACCESS_TOKEN=...`
4. Restart the bot

### Every Morning (Zerodha — 8:45 AM IST)
```powershell
.venv\Scripts\Activate.ps1
python get_kite_token.py   # refresh token (run at 8:45 AM)
python main.py             # start bot   (run at 8:55 AM)
```

Open: **http://localhost:8000**

### Every Evening (3:35 PM IST)
```powershell
# Stop the bot after market close
Ctrl+C
```

Or if running in background:
```powershell
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
```

### Dashboard Overview

**Left — Controls:**
- Login to Kite, Start/Stop analysis, Analyze Now
- Symbol Finder with NSE/BSE exchange filter

**Center — Trading Console:**
- Live chat interface for all commands
- Quick-action buttons: Status, Analyze, Smart Strikes, Positions, Daily

**Right — Live Statistics:**
- Trend, current index price, RSI
- Daily P&L (pulled from Kite API — includes trades from any source)
- **Best to Trade Today** — real-time NIFTY / BANKNIFTY / SENSEX comparison
- **Trades Today** — all day trades with OPEN/CLOSED status and individual P&L

### Stop the Bot
```
Ctrl+C
```

---

## Running Unattended (Background Mode)

The bot can run in the background when you lock your screen or step away from your computer.

### Quick Start for Unattended Trading

```powershell
# 1. Disable sleep (one-time setup)
powercfg /change standby-timeout-ac 0

# 2. Start the bot
.\start_bot_background.ps1

# 3. Minimize the window (don't close it!)
# 4. Lock your screen (Windows + L)
# 5. Go anywhere - bot keeps trading! ✅
```

### Important Requirements

**✅ Bot WILL keep running when you:**
- Lock your screen (Windows + L)
- Switch users
- Minimize the terminal window
- Step away from your computer

**⚠️ Bot WILL STOP if you:**
- Close the terminal window
- Log out of Windows
- Computer goes to sleep/hibernate
- Shut down or restart computer

### Best Practices

1. **Disable sleep while plugged in** (one-time setup):
   ```powershell
   powercfg /change standby-timeout-ac 0
   ```

2. **Start bot using the background script**:
   - Double-click `start_bot_background.ps1` 
   - Or right-click → "Run with PowerShell"

3. **Minimize the window** (don't close it!)

4. **Lock your screen** and go anywhere — bot continues trading

5. **Access remotely** (optional - see detailed setup below)

### Remote Access from Mobile/Tablet

To monitor your bot from your phone or tablet:

**Step 1: Configure Firewall (First-time setup)**

Open PowerShell **as Administrator** and run:
```powershell
# Allow port 8000 through Windows Firewall
New-NetFirewallRule -DisplayName "NIFTY Trading Bot" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

**Step 2: Find Your Computer's IP Address**
```powershell
# Run this on your PC
ipconfig
```
Look for **IPv4 Address** under your active network adapter (usually starts with `192.168.` or `10.0.`)

Example output:
```
Wireless LAN adapter Wi-Fi:
   IPv4 Address. . . . . . . . . . . : 192.168.1.100
```

**Step 3: Access Dashboard from Mobile**

1. Make sure your phone/tablet is on the **same Wi-Fi network** as your PC
2. Open browser on your mobile device
3. Navigate to: `http://YOUR_PC_IP:8000`
   - Example: `http://192.168.1.100:8000`
4. Bookmark it for quick access!

**Troubleshooting Mobile Access:**

If you can't connect from mobile:
- ✅ Verify both devices are on the same Wi-Fi network
- ✅ Check firewall rule is added (Step 1 above)
- ✅ Confirm bot is running on your PC
- ✅ Try accessing from PC first: `http://localhost:8000` (should work)
- ✅ Disable VPN on either device if active
- ✅ Restart the bot after adding firewall rule

### Security Tips

- Always lock your screen before leaving (Windows + L)
- Set a strong Windows password
- Keep `.env` file secure (contains API credentials)
- Monitor your bot remotely via the dashboard

---

## Available Commands

### Index Switching
```
index nifty          Switch to NIFTY 50 (lot: 65)
index banknifty      Switch to BANK NIFTY (lot: 30)
index sensex         Switch to SENSEX (lot: 20)
index                Show current index info
```

### Analysis
```
status               Bot status + current trend
analyze              Run immediate analysis
research             Smart strike recommendations (option chain)
regime               Market regime: trending / ranging / volatile
mtf / confluence     Multi-timeframe (5m + 15m + 1h) confluence
gap                  Today's gap-up/gap-down analysis
```

### Options Trading
```
buy CE [strike] [qty]    Buy Call (deep research runs first)
buy PE [strike] [qty]    Buy Put (deep research runs first)
buy CE                   ATM strike selected automatically
sell <symbol>            Exit position by symbol
close all                Close all open positions
positions                Show open positions
```

### Strike Analysis & Monitoring
```
strike 25000 CE 17mar    Analyse 25000 CE expiring 17 Mar
strike 24900 PE          Analyse put at next expiry
watch 25000 CE 17mar     Monitor every 30s until stopped
watch 25000 CE 17mar 60  Monitor every 60s
stop / unwatch           Stop monitoring
```

### ORB & VWAP Strategies
```
orb                  Opening Range Breakout status / levels
orb on/off           Enable/disable ORB auto-trading
vwap                 VWAP Mean Reversion status / levels
vwap on/off          Enable/disable VWAP auto-trading (RANGING days)
vwap set dev <N>     Deviation threshold % (default 0.4)
```

### Stock / Equity (research only, no execution)
```
stock RELIANCE       Technical analysis for any stock
stock HDFCBANK NSE   Specify exchange
screen bullish       Scan for bullish signals
screen oversold      RSI < 30 candidates
screen overbought    RSI > 70 candidates
```

### Custom Rules
```
rule list                         Show all rules
rule add if rsi < 30 then buy ce
rule add if trend is bearish and rsi > 65 then buy pe
rule enable / disable <id>        Toggle rule
rule remove <id>                  Delete rule
rule help                         Full syntax guide
```

### Advanced Analysis
```
backtest [1m|3m|6m|1y|2y]    Backtest strategy (win rate, Sharpe, drawdown)
theta <premium> <days>        Theta decay clock for an option
journal                       Today's session summary
journal review                Post-market lessons
journal export                Export to CSV
```

### Settings & Control
```
set sl <pct>       Override stop-loss %
set target <pct>   Override target %
pause / resume     Pause/resume auto-trading
```

---

## Strategies

### 1. Trend Following (default)
Runs every `TREND_ANALYSIS_INTERVAL_SECONDS`. Uses SMA(20/50), RSI(14), MACD, and EMA(9/21) to detect BULLISH / BEARISH signals. Only trades when the market regime confirms a trending condition.

### 2. Opening Range Breakout (ORB)
Captures the high/low of the first 15 minutes (9:15–9:30 AM). Trades breakouts with volume confirmation. Entry window: 9:30–11:30 AM. Max 1 trade per day.

### 3. VWAP Mean Reversion
Active on **RANGING** regime days and on **strong TRENDING days (ADX > 50)** with neutral 5-minute signal. Enters when price deviates >0.4% from VWAP with RSI confirmation. Good for sideways, choppy markets and strong-trend mean-reversion entries.

- On RANGING days: runs independently alongside ORB
- On TRENDING days (ADX > 50): acts as a **secondary entry** when the 5-minute signal is neutral
- Regime-direction alignment enforced: TRENDING DOWN only allows PE entry; TRENDING UP only allows CE entry
- Max 1 VWAP trade per day (2nd slot of the 2-position limit)

### 4. Gap Up / Gap Down (new)
Runs once daily at market open (9:15–9:30 AM). Detects opening gaps caused by overnight events (global markets, news, geopolitical/war events).

| Gap % | Type | Action |
|---|---|---|
| > 1.5% up | Strong Gap Up 🚀 | Buy CE immediately |
| 0.75–1.5% up | Moderate Gap Up 📈 | Wait for ORB confirmation |
| < ±0.75% | Neutral ➡️ | Normal ORB / trend strategy |
| 0.75–1.5% down | Moderate Gap Down 📉 | Wait for ORB confirmation |
| > 1.5% down | Strong Gap Down 💥 | Buy PE immediately |

Configure thresholds in `.env`:
```env
GAP_MIN_GAP_PCT=0.75      # minimum gap to trade
GAP_STRONG_GAP_PCT=1.5    # threshold for immediate (no-wait) entry
```

Type `gap` in the chat UI anytime to see today's gap analysis.

### Strategy Selection by Regime

The bot supports **2 simultaneous open positions** — one from ORB and one from VWAP, running independently. `TRADING_MAX_POSITIONS=2` controls this limit.

| Regime | Active Strategies | Max Simultaneous Positions |
|---|---|---|
| TRENDING UP/DOWN (ADX ≤ 50) | Trend Following + ORB + Gap | 1 (ORB only) |
| TRENDING UP/DOWN (ADX > 50) | Trend Following + ORB + **VWAP** + Gap | **2 (ORB + VWAP)** |
| RANGING | VWAP Mean Reversion + ORB + Gap | **2 (ORB + VWAP)** |
| VOLATILE | ORB + Gap only (trend-follow paused) | 1 (ORB only) |

> **How 2 simultaneous trades work:** ORB fires in the morning (9:30–11:30 AM) and takes slot 1. VWAP fires later in the day when price deviates >0.4% from VWAP, taking slot 2. Both positions are monitored independently with their own GTT stop-loss orders.

---

## Broker Selection

Set `BROKER` in `.env` to choose your broker:

| Broker | Setting | Cost | Auth | GTT / Stop Orders |
|---|---|---|---|---|
| **Dhan** (recommended) | `BROKER=dhan` | **Free** | Permanent token — set once | Forever Orders (exchange-level) |
| **Zerodha** | `BROKER=zerodha` | ₹2,360/month (Kite Connect) | Daily token via `get_kite_token.py` | GTT Orders |

Both brokers implement an identical interface — the rest of the bot is unchanged.

### Switching to Dhan (Free API)

1. Sign up at **https://dhanhq.co/** → open an account
2. Go to **API portal** → create an app → copy **Client ID** and **Access Token**
   - Sandbox (for testing, no real money): **https://developer.dhanhq.co**
   - Live (real trading): **https://developer.dhanhq.co** → Live section
3. Set in `.env`:
   ```env
   BROKER=dhan
   DHAN_CLIENT_ID=your_client_id
   DHAN_ACCESS_TOKEN=your_permanent_token
   DHAN_PASSWORD=your_dhan_account_password
   DHAN_PORTAL_URL=https://developer.dhanhq.co
   ```
4. `pip install dhanhq` (already in `requirements.txt`)
5. Start the bot normally — no token refresh script needed

> The Dhan instrument master CSV is downloaded automatically on first run each day and cached at `browser/.dhan_instruments.csv` (18,000+ FNO entries).

> **Token regeneration**: Dhan tokens are long-lived (~1 year). If your token ever expires or is revoked, run `python get_dhan_token.py` to auto-regenerate it — no manual copy-paste needed.

### AMO (After Market Orders) — Dhan

With Dhan, the bot can place orders outside market hours for execution at the next open:

| Window | Type |
|---|---|
| 17:00 – 23:59 (evening) | AMO window |
| 00:00 – 09:08 (overnight/morning) | AMO window |
| 09:15 – 15:30 | Normal intraday orders |

Enable in `.env`:
```env
TRADING_AMO_ENABLED=true
```

AMO orders are placed as `CNC` product type with `after_market_order=True`. They execute at market open. Stop-loss / target are set via **Forever Orders** (Dhan's exchange-level GTT equivalent) placed alongside.

### Forever Orders (Dhan GTT)

Dhan uses **Forever Orders** as the equivalent of Zerodha GTT. They are placed at the exchange level and survive bot restarts, internet outages, and system crashes. The bot automatically places a Forever Order for stop-loss whenever a position is opened.

---

### Kite API — Execution Modes (Zerodha only)

The bot supports two order execution modes when `BROKER=zerodha`:

| Mode | Setting | Description |
|---|---|---|
| **Kite API** (recommended) | `KITE_USE_KITE_API=true` | Official REST API, reliable, real P&L |
| **Browser Automation** (fallback) | `KITE_USE_KITE_API=false` | Playwright-driven, less reliable |

**Symbol format** is built automatically per index and expiry type:
- NIFTY/SENSEX weekly: `NIFTY{YY}{month_code}{DD}{strike}{CE|PE}` (e.g. `NIFTY26317CE24000`)
- BANKNIFTY monthly: `BANKNIFTY{DD}{MON}{YY}{strike}{CE|PE}` (e.g. `BANKNIFTY25MAR2648000CE`)
- SENSEX uses `BFO` exchange; NIFTY/BANKNIFTY use `NFO`

---

## Project Structure

```
nifty-trading-bot/
├── main.py                       # Entry point
├── config.py                     # All configuration (Dhan, Zerodha, Trading, Gap, etc.)
├── get_kite_token.py             # Zerodha only: automated daily token refresher
├── get_dhan_token.py             # Dhan: regenerate access token (rarely needed)
├── requirements.txt
├── .env                          # Credentials & settings (never commit)
├── pytest.ini                    # Test config (asyncio_mode=auto)
├── README.md
├── bot/
│   ├── engine.py                 # Core orchestration engine (broker-agnostic)
│   ├── trend_analyzer.py         # Technical analysis (SMA/RSI/MACD)
│   ├── order_manager.py          # Order execution, risk management, AMO window
│   ├── index_config.py           # Index configs (NIFTY / BANKNIFTY / SENSEX)
│   ├── market_research.py        # Smart strike and stock analysis
│   ├── instructions.py           # Custom rules engine
│   ├── backtester.py             # Strategy backtesting
│   ├── market_regime.py          # Regime detection (ADX + ATR + Hurst)
│   ├── multi_timeframe.py        # Multi-timeframe confluence (5m/15m/1h)
│   ├── orb_strategy.py           # Opening Range Breakout strategy
│   ├── vwap_strategy.py          # VWAP Mean Reversion strategy
│   ├── gap_detector.py           # Gap up/gap-down detection & trading
│   ├── theta_clock.py            # Theta decay calculator
│   ├── trade_journal.py          # Trade history and reporting
│   ├── nse_scraper.py            # NSE option chain data
│   └── bse_scraper.py            # BSE/SENSEX option chain data
├── browser/
│   ├── dhan.py                   # DhanBroker: Dhan API, Forever Orders, AMO
│   ├── zerodha.py                # ZerodhaKite: Kite API + browser automation fallback
│   ├── factory.py                # create_broker() — returns DhanBroker or ZerodhaKite
│   └── .dhan_instruments.csv     # Dhan FNO instrument master (auto-refreshed daily)
├── tests/
│   ├── test_zerodha.py           # 47 unit tests (Kite API + browser modes)
│   └── test_dhan_order_flows.py  # 8 unit tests (Dhan orders, AMO, symbol matching)
└── web/
    ├── app.py                    # FastAPI REST endpoints
    ├── websocket.py              # WebSocket for real-time chat
    └── static/
        └── index_v2.html           # Dashboard UI (shows broker badge: Dhan / Kite)
```

---

## Features

### Multi-Index Support
- Switch between NIFTY 50, BANK NIFTY, and SENSEX at runtime
- Correct lot sizes applied automatically (65 / 30 / 20)
- Correct strike intervals and expiry days per index
- Live index comparison with auto-switch option

### Free Broker Support (Dhan)
- Dhan API is completely free — no monthly subscription
- Permanent access token — set once, never refresh
- Instrument master auto-downloaded daily (18,000+ FNO entries)
- Flexible symbol matching handles all Dhan compact/dash option formats
- Security-ID cache ensures correct strikes for GTT and exit orders

### Real P&L and Trade Tracking
- Daily P&L pulled from Kite API — reflects **all** trades including manual Kite web trades
- Trades Today panel shows every day trade (open + closed) with individual P&L
- Position count matches Kite's own Positions counter

### Safety Guardrails
- Daily loss limit, per-trade loss limit, max open positions
- Max trades per day, pause after N consecutive losses
- Auto-close all positions before market close

### Smart Exits
- Trailing stop loss (percentage-based, trails from peak)
- Profit tiers (partial exits at two configurable levels)
- RSI market filter, volatility filter, avoid low-volume hours

### Gap Strategy
- Detects gap-up/gap-down at market open
- Immediate entry on strong gaps (>1.5%); ORB confirmation on moderate gaps
- Fires once per trading day, resets automatically at midnight
- Configurable thresholds and position sizing multiplier

### Deep Research Before Every Trade
- Option chain analysis (OI, PCR, IV estimation)
- Conservative / Moderate / Aggressive strike recommendations
- Strike-level GO / NO-GO with confidence score
- NSE real option chain data when available

### Advanced Analysis Tools
- Market regime detection (trending / ranging / volatile, using ADX + Hurst exponent)
- Multi-timeframe confluence (5m + 15m + 1h must all agree)
- Strategy backtesting with win rate, profit factor, Sharpe ratio
- Theta decay clock
- Trade journal with daily review and CSV export

### Automated Daily Token Refresh (Zerodha only)
- `get_kite_token.py` runs headless Playwright to log in, complete 2FA (PIN or TOTP), and auto-update `.env`
- No manual copy-paste required
- **Not needed for Dhan** — Dhan tokens are permanent
- Run once every morning before 9:15 AM IST (Zerodha users)

### AMO (After Market Orders)
- Place gap-strategy orders in the evening for next-day market-open execution
- AMO window: **17:00–23:59** and **00:00–09:08** IST (weekdays)
- Orders execute at market open alongside a Forever Order stop-loss
- Controlled by `TRADING_AMO_ENABLED=true` in `.env`
- Supported on Dhan only

### Alerts
- Telegram notifications (trade entry, exit, gap alerts, P&L updates)
- Daily report at market close
- Real-time WebSocket dashboard updates

---

## Running Tests

```powershell
.venv\Scripts\Activate.ps1
python -m pytest tests/ -v
```

**55 tests total** across two test files:

`tests/test_zerodha.py` — **47 tests**:
- Kite API initialization (key/token, missing credentials)
- `_build_option_symbol` for all indices and month codes
- `place_order` — buy/sell, NFO/BFO exchange, no `price` on MARKET orders
- `get_positions` / `close_position` via API and browser
- `get_instrument_price`, `get_holdings`
- Order routing (API vs browser fallback)
- Browser login/logout, initialize skip in API mode

`tests/test_dhan_order_flows.py` — **8 tests**:
- `test_place_order` — correct security ID, exchange, INTRADAY, MARKET, qty
- `test_gtt_place` — Forever Order trigger/limit prices, SELL INTRADAY
- `test_gtt_cancel` — `cancel_forever_order` called with correct ID
- `test_close_position` — SELL MARKET, correct quantity
- `test_stop_loss_logic` — SL −20%, target +35%, trailing SL, no false trigger
- `test_profit_tiers` — Tier 1 +20% exit 50%, Tier 2 +30% after Tier 1
- `test_amo_order` — 18:30/08:00 = AMO, 10:30 = not AMO; `after_market_order=True` + `CNC` sent; `OrderManager` AMO bypass
- `test_symbol_matching` — all 5 symbol format variants; cache-first priority; map fallback

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Bot state, P&L, position count (from Kite) |
| GET | `/api/positions` | Open positions (from Kite net positions) |
| GET | `/api/trades` | All day trades with OPEN/CLOSED status |
| GET | `/api/compare-indices` | NIFTY vs BANKNIFTY vs SENSEX comparison |
| GET | `/api/symbols` | Symbol search for auto-suggest |
| POST | `/api/login` | Trigger Kite login |
| POST | `/api/start` | Start analysis loop |
| POST | `/api/stop` | Stop analysis loop |
| POST | `/api/command` | Execute a trading command |
| GET | `/health` | Health check |
| WS | `/ws` | WebSocket for live chat |

---

## Troubleshooting

### `ModuleNotFoundError`
```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Bot will not start
1. Activate venv first
2. Check `python --version` — must be 3.10+
3. Reinstall: `pip install -r requirements.txt`

### Port 8000 in use
```powershell
taskkill /F /IM python.exe /T
Start-Sleep -Seconds 2
python main.py
```

### Kite token expired / `Incorrect api_key or access_token` (Zerodha only)
```powershell
python get_kite_token.py   # fully automated — no manual steps
python main.py             # restart bot
```

Make sure `ZERODHA_PIN` (or `ZERODHA_TOTP_SECRET`) is set in `.env`.

### Dhan `401 Unauthorized` / token error
- Your Dhan access token may have expired or been revoked
- Run the automated token refresher:
  ```powershell
  python get_dhan_token.py
  python main.py
  ```
- Or regenerate manually: log in at **https://developer.dhanhq.co** → generate a new token → update `DHAN_ACCESS_TOKEN` in `.env`

### P&L shows 0 or wrong value
- Ensure broker API is connected (status badge shows green — **Dhan: Online** or **Kite: Online**)
- P&L is fetched live from the broker — requires a valid access token

### Trades Today panel is empty
- Trades appear only for the current trading day
- Requires a valid broker API connection

### No trades executing
1. Check `TRADING_AUTO_TRADE_ENABLED=true` in `.env`
2. Verify daily loss limit has not been hit
3. Check market hours (9:15 AM – 3:30 PM IST, weekdays only)
4. Check bot logs or Telegram for error messages

### Gap trade not firing
- Ensure `GAP_ENABLED=true` in `.env`
- Gap check only runs between 9:15–9:30 AM IST
- If gap is < `GAP_MIN_GAP_PCT` (0.75%), it is classified as NEUTRAL — no trade

### Browser Automation (Fallback)
Set `KITE_USE_KITE_API=false` to use Playwright browser automation.
This mode does **not** support real P&L or position fetching from Kite.
Use only if Kite API setup is unavailable.

---

## Technical Indicators Reference

| Indicator | Period | Use |
|-----------|--------|-----|
| SMA(20) | 20 candles | Short-term trend |
| SMA(50) | 50 candles | Long-term trend |
| RSI | 14 candles | Overbought (>70) / Oversold (<30) |
| MACD | 12/26/9 | Momentum |
| ATR | 14 candles | Volatility |
| ADX | 14 candles | Trend strength (regime detection) |
| Hurst Exponent | 60 days | Trending vs mean-reverting |
| Bollinger Bands | 20/2σ | Support / Resistance |

**Trend logic:**
- **BULLISH**: SMA(20) > SMA(50), RSI < 70, EMA(9) > EMA(21)
- **BEARISH**: SMA(20) < SMA(50), RSI > 30, EMA(9) < EMA(21)
- **NEUTRAL**: Mixed signals

**Regime logic:**
- **TRENDING**: ADX > 25, Hurst > 0.55
- **RANGING**: ADX < 20, Hurst < 0.45
- **VOLATILE**: High ATR percentile, no clear direction

---

## Capital Requirements

| Risk Level | Capital | Settings |
|---|---|---|
| Conservative (recommended to start) | ₹75,000+ | `MAX_POSITIONS=2`, `MAX_DAILY_LOSS=5000` |
| Moderate | ₹1,50,000+ | Default `.env` settings |
| Aggressive | ₹2,50,000+ | Default + increase position limits |

### Start Conservatively (recommended first week)

When going live, start with minimal risk settings:

```env
TRADING_AUTO_TRADE_ENABLED=true
TRADING_DEFAULT_QUANTITY=1         # Start with 1 lot only
TRADING_MAX_POSITIONS=1            # Limit to 1 position
TRADING_MAX_LOSS_PER_TRADE=1000    # Lower loss limit
TRADING_MAX_DAILY_LOSS=2000        # Lower daily limit
```

**Capital needed:** ₹20,000–₹30,000

### Gradually Scale Up (after 5-10 successful days)

Once you've verified the bot works correctly with your broker and you're comfortable with the strategy:

```env
TRADING_DEFAULT_QUANTITY=65        # Full NIFTY lot (or 30 for BANKNIFTY, 20 for SENSEX)
TRADING_MAX_POSITIONS=2            # Back to default
TRADING_MAX_LOSS_PER_TRADE=2500    # Normal limits
TRADING_MAX_DAILY_LOSS=5000        # Normal daily limit
```

**Capital needed:** ₹75,000+

> **Start conservative.** Run in signal-only mode (`TRADING_AUTO_TRADE_ENABLED=false`) for 2–4 weeks, review journal, then enable live trading with 1 lot. Scale up gradually after consistent success.


---

## Testing & Validation

All core functionality is covered by automated tests:

```powershell
pytest tests/ -v
```

**Test Coverage (55 tests passing):**
- ✅ Dhan broker: Order placement, GTT (Forever Orders), position management, AMO orders
- ✅ Zerodha broker: API mode, browser fallback, symbol matching, position tracking
- ✅ All order flows validated with mocked broker responses

**Latest Test Run:** March 15, 2026 — **55/55 PASSED** ✅

---

## Broker Comparison

| Feature | Dhan | Zerodha |
|---|---|---|
| **API Cost** | Free 🎉 | ₹2,360/month |
| **Token Refresh** | Manual (expires ~monthly) | Daily (8:45 AM) |
| **Token Script** | Manual copy from portal | `get_kite_token.py` (automated) |
| **GTT Orders** | ✅ Supported | ✅ Supported |
| **AMO Orders** | ✅ Supported | ❌ Not implemented yet |
| **Position Tracking** | Real-time via API | Real-time via API |
| **Recommended For** | All users (cost-effective) | High-volume traders |

**Currently Active:** Dhan (set in `.env` as `BROKER=dhan`)

---

## File Structure

```
nifty-trading-bot/
├── .env                    # Configuration (never commit!)
├── main.py                 # Bot entry point
├── config.py               # Settings loader
├── get_kite_token.py       # Zerodha token refresh (automated)
├── requirements.txt        # Python dependencies
├── README.md              # This file
├── bot/                   # Trading strategies & logic
│   ├── engine.py          # Main trading engine
│   ├── gap_detector.py    # Gap up/down strategy
│   ├── orb_strategy.py    # Opening Range Breakout
│   ├── vwap_strategy.py   # VWAP mean reversion
│   ├── trend_analyzer.py  # Trend detection
│   ├── market_regime.py   # Regime classification
│   ├── multi_timeframe.py # MTF confluence
│   ├── backtester.py      # Strategy backtesting
│   ├── order_manager.py   # Order execution logic
│   ├── trade_journal.py   # Session logging
│   └── ...
├── browser/               # Broker integrations
│   ├── dhan.py           # Dhan API wrapper
│   ├── zerodha.py        # Zerodha Kite Connect + browser
│   └── factory.py        # Broker factory
├── web/                   # Web dashboard
│   ├── app.py            # FastAPI server
│   ├── websocket.py      # Real-time updates
│   └── static/           # HTML/JS UI
├── tests/                 # Automated test suite
│   ├── test_dhan_order_flows.py      # Dhan tests
│   └── test_zerodha.py               # Zerodha tests
└── journals/              # Daily trade logs (JSON)
```

---

## Disclaimer

**THIS SOFTWARE IS PROVIDED AS IS WITHOUT WARRANTY OF ANY KIND.**

- Trading is risky — you can lose money
- Past performance does not guarantee future results
- The authors are NOT responsible for financial losses
- Always test with paper trading before enabling live trades

