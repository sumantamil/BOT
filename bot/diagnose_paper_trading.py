"""
Paper Trading Diagnostic Tool
==============================
Checks every possible failure point that could prevent paper trades from firing.
Run from the project root with venv active:

    python bot/diagnose_paper_trading.py

Works whether the bot is running or not — reads config, state files,
and yfinance directly. Does NOT connect to the live bot process.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, date, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Path setup ──────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from config import settings  # pydantic-settings — loads .env automatically

IST = ZoneInfo("Asia/Kolkata")

RESET   = "\033[0m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
CYAN    = "\033[96m"
BOLD    = "\033[1m"

def ok(msg):  print(f"  {GREEN}✅ {msg}{RESET}")
def err(msg): print(f"  {RED}❌ {msg}{RESET}")
def warn(msg):print(f"  {YELLOW}⚠️  {msg}{RESET}")
def info(msg):print(f"  {CYAN}ℹ️  {msg}{RESET}")
def hdr(msg): print(f"\n{BOLD}{msg}{RESET}\n{'─'*60}")


issues:   list[dict] = []
warnings: list[dict] = []

def issue(severity: str, section: str, problem: str, fix: str):
    issues.append({"severity": severity, "section": section, "problem": problem, "fix": fix})

def warning(section: str, problem: str, impact: str):
    warnings.append({"section": section, "problem": problem, "impact": impact})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ist_now() -> datetime:
    return datetime.now(IST)

def _fetch_vix() -> float:
    try:
        import yfinance as yf
        vi = yf.Ticker("^INDIAVIX").fast_info
        v  = float(vi.get("lastPrice") or 0)
        if v <= 0:
            d = yf.Ticker("^INDIAVIX").history(period="1d", interval="1d")
            if not d.empty:
                v = float(d["Close"].iloc[-1])
        return v
    except Exception as e:
        return 0.0

def _fetch_nifty_5m():
    try:
        import yfinance as yf
        import pandas as pd
        df = yf.Ticker("^NSEI").history(period="1d", interval="5m")
        if df.index.tz is None:
            df.index = pd.to_datetime(df.index).tz_localize("UTC").tz_convert(IST)
        else:
            df.index = df.index.tz_convert(IST)
        return df
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
# CHECKS
# ════════════════════════════════════════════════════════════════════════════

def check_1_mode():
    hdr("[CHECK 1] Trading Mode")

    auto_trade = settings.trading.auto_trade_enabled
    if auto_trade:
        err("TRADING_AUTO_TRADE_ENABLED=true — bot is in LIVE mode, not paper mode")
        issue("CRITICAL", "Mode", "auto_trade=True means REAL orders, not paper trades",
              "Set TRADING_AUTO_TRADE_ENABLED=false in .env and restart")
    else:
        ok("Paper trading mode active (TRADING_AUTO_TRADE_ENABLED=false)")


def check_2_market_hours():
    hdr("[CHECK 2] Market Hours & Timing")

    now = _ist_now()
    t   = now.time()
    info(f"System time (IST): {now.strftime('%Y-%m-%d %H:%M:%S')}")

    market_open  = dtime(9, 15)
    market_close = dtime(15, 30)

    if now.weekday() >= 5:
        warn("Today is a weekend — Indian markets are closed")
        warning("Timing", "Weekend", "No signals generated on weekends")
    elif not (market_open <= t <= market_close):
        warn(f"Outside market hours ({t.strftime('%H:%M')} IST). "
             f"Window: {market_open.strftime('%H:%M')}–{market_close.strftime('%H:%M')}")
        warning("Timing", f"Current time {t.strftime('%H:%M')} outside 09:15–15:30",
                "No signals generated outside market hours — this is NORMAL if it's evening/weekend")
    else:
        ok(f"Inside market hours ({t.strftime('%H:%M')} IST)")

    # ORB window check
    orb_start = dtime(9, 30)
    orb_end   = dtime(settings.orb.entry_end_hour, settings.orb.entry_end_minute)
    if orb_start <= t <= orb_end:
        ok(f"Inside ORB entry window ({orb_start.strftime('%H:%M')}–{orb_end.strftime('%H:%M')})")
    else:
        info(f"Outside ORB entry window (closes {orb_end.strftime('%H:%M')})")

    # VWAP window
    if dtime(9, 30) <= t <= dtime(14, 30):
        ok("Inside VWAP window (09:30–14:30)")
    else:
        info("Outside VWAP window (09:30–14:30)")

    # EOD window
    if dtime(14, 30) <= t <= dtime(15, 0):
        ok("Inside EOD window (14:30–15:00)")
    else:
        info("Outside EOD window (14:30–15:00)")

    # Auto-trade cutoff
    if t >= dtime(14, 0):
        warn("Past 14:00 IST — no new TREND auto-entries allowed (thin liquidity cutoff)")


def check_3_strategy_flags():
    hdr("[CHECK 3] Strategy Enable Flags")

    strategies = {
        "ORB":      settings.orb.enabled,
        "VWAP":     settings.vwap.enabled,
        "GAP":      settings.gap.enabled,
        "EOD":      settings.eod.enabled,
        "LATE_DAY": settings.late_day.enabled,
    }

    enabled_count = sum(1 for v in strategies.values() if v)
    for name, enabled in strategies.items():
        if enabled:
            ok(f"{name} enabled")
        else:
            warn(f"{name} disabled ({name}_ENABLED=false in .env)")

    if enabled_count == 0:
        err("ALL strategies are disabled — no signals can fire")
        issue("CRITICAL", "Strategy Flags", "Every strategy is disabled",
              "Enable at least one: ORB_ENABLED=true (or VWAP_ENABLED=true)")


def check_4_vix_filter():
    hdr("[CHECK 4] India VIX Filter")

    vix_enabled = settings.trading.vix_filter_enabled
    vix_max     = settings.trading.vix_max
    info(f"VIX filter enabled: {vix_enabled}  |  Max allowed: {vix_max}")

    if not vix_enabled:
        warn("VIX filter is OFF — entries allowed regardless of VIX level")
        return

    print("  Fetching India VIX from yfinance…")
    vix = _fetch_vix()

    if vix <= 0:
        warn("Could not fetch India VIX — filter will be skipped (non-blocking)")
        warning("VIX Filter", "yfinance VIX fetch failed",
                "On live market days the filter may silently skip if network is flaky")
        return

    info(f"Current India VIX: {vix:.2f}")
    if vix > vix_max:
        err(f"VIX {vix:.2f} > threshold {vix_max} — ALL auto-entries are blocked")
        issue("HIGH", "VIX Filter",
              f"India VIX {vix:.2f} exceeds TRADING_VIX_MAX={vix_max}",
              f"Raise TRADING_VIX_MAX to {int(vix)+2} in .env, or set TRADING_VIX_FILTER_ENABLED=false temporarily")
    else:
        ok(f"VIX {vix:.2f} ≤ {vix_max} — VIX filter passes")


def check_5_orb_state():
    hdr("[CHECK 5] ORB Strategy State")

    if not settings.orb.enabled:
        warn("ORB disabled — skipping ORB state check")
        return

    # Import ORBStrategy to check current state
    try:
        from bot.orb_strategy import ORBStrategy, ORBState
        from bot.index_config import NIFTY
        orb = ORBStrategy(index_config=NIFTY)
        orb.analyze()  # runs _ensure_daily_reset + builds state
        state = orb._state

        info(f"ORB state: {state.value}")
        info(f"ORB range built: {orb._range_high > 0} "
             f"(high={orb._range_high:.0f} low={orb._range_low:.0f})")
        info(f"ORB signal fired today: {orb._signal_fired}")

        if state == ORBState.TRIGGERED:
            info("ORB already fired once today (1/day limit) — this is normal after the first paper trade")
        elif state == ORBState.EXPIRED:
            warn("ORB in EXPIRED state — entry window has closed for today")
        elif state == ORBState.WAITING:
            warn("ORB in WAITING state — before 09:15 IST")
        elif state == ORBState.BUILDING:
            info("ORB still building opening range (before 09:30)")
        elif state == ORBState.READY:
            ok("ORB READY — watching for breakout")

        # Minimum range width check
        rw = orb._range_width
        min_width = 75  # NIFTY
        if rw > 0 and rw < min_width:
            warn(f"ORB range width {rw:.0f} pts < minimum {min_width} pts — breakout will be skipped")
            issue("MEDIUM", "ORB Range Width",
                  f"Opening range too narrow ({rw:.0f} pts < {min_width} pts min)",
                  "Wait for a wider opening range day, or lower ORB_MIN_RANGE_WIDTH in config")
        elif rw > 0:
            ok(f"ORB range width {rw:.0f} pts ≥ {min_width} pts minimum")

    except Exception as e:
        err(f"Could not instantiate ORBStrategy: {e}")
        issue("HIGH", "ORB Strategy", f"ORBStrategy import/init failed: {e}",
              "Check bot/orb_strategy.py for errors")


def check_6_market_data():
    hdr("[CHECK 6] Market Data (yfinance)")

    print("  Fetching NIFTY 5m data…")
    df = _fetch_nifty_5m()

    if df is None or df.empty:
        err("No NIFTY 5m data returned from yfinance")
        issue("HIGH", "Market Data", "yfinance returned empty NIFTY 5m data",
              "Check internet connection. yfinance only provides 60 days of 5m data.")
        return

    latest_price = df["Close"].iloc[-1]
    latest_time  = df.index[-1]

    ok(f"Data available — {len(df)} bars")
    info(f"Latest bar: {latest_time.strftime('%H:%M IST')}  NIFTY close = {latest_price:,.2f}")

    now_ist = _ist_now()
    try:
        stale_min = (now_ist - latest_time).total_seconds() / 60
        if stale_min > 15:
            warn(f"Last data bar is {stale_min:.0f} minutes old — may be stale (outside market hours is normal)")
        else:
            ok(f"Data freshness: {stale_min:.0f} min old")
    except Exception:
        info("(Could not compute data age — timezone mismatch)")


def check_7_regime():
    hdr("[CHECK 7] Market Regime")

    try:
        from bot.market_regime import regime_detector
        from bot.index_config import NIFTY
        regime_detector.set_index(NIFTY)
        result = regime_detector.analyze()

        if result is None:
            warn("Regime detector returned None — data fetch failed")
            warning("Regime", "regime_detector.analyze() returned None",
                    "Strategies that require regime confirmation will use fallback (should_trade=True)")
            return

        info(f"Regime: {result.regime.value}")
        info(f"ADX: {result.adx:.1f}  Hurst: {result.hurst:.3f}  Should trade: {result.should_trade}")

        if not result.should_trade:
            warn("Regime says SHOULD NOT TRADE — all auto-trend strategies skipped")
            warning("Regime", f"regime.should_trade=False (regime={result.regime.value})",
                    "TREND auto-trades are skipped in choppy conditions. ORB/VWAP still run independently.")
        else:
            ok(f"Regime allows trading ({result.regime.value})")

    except Exception as e:
        warn(f"Could not check regime: {e}")
        warning("Regime", f"regime_detector error: {e}",
                "Regime check skipped — strategies will fall back to no-regime mode")


def check_8_paper_trade_file():
    hdr("[CHECK 8] Paper Trade History (paper_trades.json)")

    path = _ROOT / "paper_trades.json"
    if not path.exists():
        info("paper_trades.json not found — no paper trades recorded yet")
        warning("Paper Trades", "paper_trades.json missing",
                "File is created on first paper trade entry. Absence means bot has never entered a paper position.")
        return

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        err(f"Could not read paper_trades.json: {e}")
        return

    trades   = data.get("trades", [])
    open_pos = data.get("open_positions", [])
    summary  = data.get("summary", {})

    info(f"Total closed trades: {len(trades)}")
    info(f"Open positions:      {len(open_pos)}")
    info(f"Total P&L:           ₹{summary.get('total_pnl', 0):+,.0f}")
    info(f"Win rate:            {summary.get('win_rate', 'N/A')}")

    if trades:
        last = sorted(trades, key=lambda t: t.get("exit_time", ""))[-1]
        info(f"Last trade: {last.get('index_name')} {last.get('option_type')} "
             f"[{last.get('strategy')}] exited {last.get('exit_time', 'unknown')[:16]} "
             f"| P&L ₹{last.get('pnl', 0):+,.0f} ({last.get('exit_reason')})")
    else:
        warn("No closed paper trades on record — paper_buy() has never fired or saved")

    if open_pos:
        ok(f"{len(open_pos)} open paper position(s):")
        for p in open_pos:
            entry_t = p.get("entry_time", "")[:16]
            info(f"  #{p.get('id')}  {p.get('index_name')} {p.get('option_type')} "
                 f"[{p.get('strategy')}]  entered {entry_t}")


def check_9_filter_blocks():
    hdr("[CHECK 9] Filter Block Counters (today's session logs)")

    log_path = _ROOT / "errors.log"
    if not log_path.exists():
        info("errors.log not found — no persistent session log to scan")
        return

    today_str = date.today().isoformat()
    filter_keywords = [
        "GATE[", "BLOCK", "Skipping", "skipping",
        "already triggered", "daily_limit", "ORB_RSI", "ORB_trend",
        "VWAP_regime", "VWAP_strength", "VIX", "IV",
    ]

    hits: list[str] = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if today_str in line and any(kw in line for kw in filter_keywords):
                    hits.append(line.strip())
    except Exception as e:
        warn(f"Could not scan errors.log: {e}")
        return

    if hits:
        info(f"Found {len(hits)} filter-related lines in today's log:")
        for h in hits[-15:]:   # last 15
            print(f"    {YELLOW}{h[-120:]}{RESET}")
    else:
        info("No filter-block lines found for today in errors.log")
        info("(Bot logs filter rejections at INFO level — check console output)")


def check_10_entry_filter_score():
    hdr("[CHECK 10] Entry Filter min-score Gate")

    enabled = settings.entry_filter.enabled
    min_score = settings.entry_filter.min_confidence_score
    info(f"Entry filter enabled: {enabled}  |  Min score required: {min_score}/100")

    if not enabled:
        ok("Entry filter disabled — scoring gate won't block signals")
    elif min_score >= 80:
        warn(f"Min score {min_score}/100 is very strict — most signals will be rejected")
        warning("Entry Filter",
                f"FILTER_MIN_CONFIDENCE_SCORE={min_score} is high",
                "Lower to 60–70 if signals exist but are being filtered. Default 70 is reasonable.")
    else:
        ok(f"Entry filter threshold {min_score}/100 is within normal range")


def check_11_daily_limits():
    hdr("[CHECK 11] Daily Trade Limits & Daily P&L")

    # Daily P&L persistence
    pnl_file = _ROOT / ".daily_pnl.json"
    daily_pnl = 0.0
    if pnl_file.exists():
        try:
            d = json.loads(pnl_file.read_text())
            if d.get("date") == date.today().isoformat():
                daily_pnl = float(d.get("pnl", 0.0))
        except Exception:
            pass

    info(f"Today's realized P&L: ₹{daily_pnl:+,.0f}")
    info(f"Daily loss cap:        ₹{settings.trading.max_daily_loss:,.0f}")
    info(f"Trades today limit:    {settings.trading.max_trades_per_day}")
    info(f"Max positions:         {settings.trading.max_positions}")

    if daily_pnl <= -settings.trading.max_daily_loss:
        err(f"Daily loss cap hit (P&L ₹{daily_pnl:+,.0f} ≤ -₹{settings.trading.max_daily_loss:,.0f}) — bot halted for today")
        issue("CRITICAL", "Daily Loss Cap",
              f"Daily P&L ₹{daily_pnl:+,.0f} has hit the ₹{settings.trading.max_daily_loss:,.0f} cap",
              "Delete .daily_pnl.json to reset (only safe to do next morning), or raise TRADING_MAX_DAILY_LOSS")
    else:
        ok(f"Daily loss cap not reached (₹{daily_pnl:+,.0f} of -₹{settings.trading.max_daily_loss:,.0f})")


def check_12_rsi_guard():
    hdr("[CHECK 12] ORB RSI Guard (recently fixed)")

    # Verify the fix is in place
    engine_path = _ROOT / "bot" / "engine.py"
    try:
        content = engine_path.read_text(encoding="utf-8")
        # The fixed guard uses 75 and 25; the broken one used 55 and 45
        has_correct_75  = "_rsi >= 75" in content
        has_correct_25  = "_rsi <= 25" in content
        has_wrong_55 = "_rsi >= 55"   in content and "ORB" in content
        has_wrong_45 = "_rsi <= 45"   in content and "ORB" in content

        if has_correct_75 and has_correct_25:
            ok("ORB RSI guard uses correct thresholds (CE blocks ≥75, PE blocks ≤25)")
        elif has_wrong_55 or has_wrong_45:
            err("ORB RSI guard still has INVERTED thresholds (CE blocks ≥55, PE blocks ≤45)")
            issue("CRITICAL", "ORB RSI Guard",
                  "RSI guard blocks valid breakout signals — CE rejected when RSI ≥55 (normal on bullish days)",
                  "In engine.py _check_orb_signal(): change `_rsi >= 55` → `_rsi >= 75` and `_rsi <= 45` → `_rsi <= 25`")
        else:
            info("RSI guard thresholds not clearly detected — inspect engine.py manually")
    except Exception as e:
        warn(f"Could not inspect engine.py: {e}")


def check_13_paper_index_prices():
    hdr("[CHECK 13] Paper Exit Price Feed")

    info("Paper exits require _paper_index_prices to be populated by trend analyzer")
    info("The analysis loop updates these each cycle via `signal.current_price`")
    info("If TrendAnalyzer.analyze() returns None (yfinance error), prices won't update")
    info("→ check_exits() will log 'no index price in cache' warnings for open positions")

    # Check via paper_trades.json open positions
    path = _ROOT / "paper_trades.json"
    if path.exists():
        try:
            d = json.loads(path.read_text())
            open_pos = d.get("open_positions", [])
            if open_pos:
                warn(f"{len(open_pos)} open paper positions — ensure trending data updates every cycle")
            else:
                ok("No open paper positions currently — price feed N/A")
        except Exception:
            pass
    else:
        ok("No paper_trades.json — nothing to check")


# ════════════════════════════════════════════════════════════════════════════
# REPORT
# ════════════════════════════════════════════════════════════════════════════

def print_report():
    print(f"\n\n{'='*60}")
    print(f"{BOLD}  DIAGNOSTIC REPORT{RESET}")
    print(f"{'='*60}\n")

    critical = [i for i in issues if i["severity"] == "CRITICAL"]
    high     = [i for i in issues if i["severity"] == "HIGH"]
    medium   = [i for i in issues if i["severity"] == "MEDIUM"]

    if critical:
        print(f"{RED}{BOLD}🚨 CRITICAL ({len(critical)}){RESET}")
        for i, x in enumerate(critical, 1):
            print(f"\n  {i}. [{x['section']}] {x['problem']}")
            print(f"     💡 FIX: {x['fix']}")

    if high:
        print(f"\n{YELLOW}{BOLD}⚠️  HIGH PRIORITY ({len(high)}){RESET}")
        for i, x in enumerate(high, 1):
            print(f"\n  {i}. [{x['section']}] {x['problem']}")
            print(f"     💡 FIX: {x['fix']}")

    if medium:
        print(f"\n{CYAN}{BOLD}ℹ️  MEDIUM ({len(medium)}){RESET}")
        for i, x in enumerate(medium, 1):
            print(f"\n  {i}. [{x['section']}] {x['problem']}")
            print(f"     💡 FIX: {x['fix']}")

    if warnings:
        print(f"\n{BOLD}📋 WARNINGS ({len(warnings)}){RESET}")
        for i, w in enumerate(warnings, 1):
            print(f"\n  {i}. [{w['section']}] {w['problem']}")
            print(f"     Impact: {w['impact']}")

    if not issues:
        print(f"{GREEN}✅ No blocking issues found!{RESET}")

    print(f"\n{'='*60}")
    print(f"{BOLD}  MOST LIKELY ROOT CAUSES (check in order){RESET}")
    print(f"{'='*60}")

    now_ist = _ist_now()
    t = now_ist.time()
    in_market = (
        now_ist.weekday() <= 4
        and dtime(9, 15) <= t <= dtime(15, 30)
    )

    print("""
1. Running OUTSIDE market hours (9:15–15:30 IST, Mon–Fri)
   → Paper signals ONLY fire during live market hours
   → Run the bot between 9:15 AM and 3:30 PM on a weekday

2. India VIX above TRADING_VIX_MAX (default 20)
   → When VIX > 20, ALL auto+paper entries are blocked
   → Check with: python -c "import yfinance as yf; print(yf.Ticker('^INDIAVIX').fast_info['lastPrice'])"

3. RSI guard inverted (blocking ≥55 instead of ≥75)
   → Was blocking valid breakouts; should be fixed now
   → Verify Check 12 shows "correct thresholds"

4. ORB daily limit already triggered before bot restart
   → The 1/day guard resets at midnight — but if bot crashed & restarted
     during ORB window, the guard may have been restored from positions

5. Trend analyzer returning NEUTRAL → no VWAP/Trend entries
   → VWAP only fires in RANGING regime or when ADX > 50
   → Trend entries only fire when regime confirms TRENDING + ADX > 25

6. Entry filter score below 70/100
   → If entry_filter.enabled=True, signals need 70+ points to pass
   → Check 10 shows current threshold
""")

    if not in_market:
        print(f"{YELLOW}⭐ MOST LIKELY RIGHT NOW: Outside market hours{RESET}")
        print(f"   Current IST time: {now_ist.strftime('%H:%M:%S')}")
        print(f"   Re-run this diagnostic at 9:30–11:00 AM on a weekday to see real signal state.\n")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{'='*60}")
    print(f"{BOLD}  PAPER TRADING DIAGNOSTIC TOOL{RESET}")
    print(f"  {_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'='*60}")

    check_1_mode()
    check_2_market_hours()
    check_3_strategy_flags()
    check_4_vix_filter()
    check_5_orb_state()
    check_6_market_data()
    check_7_regime()
    check_8_paper_trade_file()
    check_9_filter_blocks()
    check_10_entry_filter_score()
    check_11_daily_limits()
    check_12_rsi_guard()
    check_13_paper_index_prices()

    print_report()


if __name__ == "__main__":
    main()
