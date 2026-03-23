"""
pre_live_checklist.py
=====================
Comprehensive pre-live trading health check.

Reads the REAL bot configuration (config.py / .env), paper trade history, and
live market data to produce a go / no-go verdict before risking real money.

No fictional classes used — all checks work against the ACTUAL codebase.

Usage
-----
    python pre_live_checklist.py           # full check
    python pre_live_checklist.py --fast    # skip yfinance live data fetch

Exit code: 0 = READY / 1 = NOT READY (can be used in CI / scripts)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional

# ─── Make sure bot/ is importable ────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# ─── Load .env so os.getenv() sees all keys ──────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv may not be installed; settings still work via pydantic

# ─── Colourise output on Windows ─────────────────────────────────────────────
try:
    import colorama
    colorama.init()
    C_GREEN  = "\033[92m"
    C_YELLOW = "\033[93m"
    C_RED    = "\033[91m"
    C_CYAN   = "\033[96m"
    C_BOLD   = "\033[1m"
    C_RESET  = "\033[0m"
except ImportError:
    C_GREEN = C_YELLOW = C_RED = C_CYAN = C_BOLD = C_RESET = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class Checklist:
    """Accumulates pass / warn / fail results and prints a final verdict."""

    def __init__(self) -> None:
        self.passed:   List[str] = []
        self.warnings: List[str] = []
        self.failed:   List[str] = []
        self.critical: List[str] = []   # subset of failed

    # ── Outcome helpers ───────────────────────────────────────────────────────

    def ok(self, msg: str) -> None:
        print(f"   {C_GREEN}✅ PASS:{C_RESET} {msg}")
        self.passed.append(msg)

    def warn(self, msg: str) -> None:
        print(f"   {C_YELLOW}⚠️  WARN:{C_RESET} {msg}")
        self.warnings.append(msg)

    def fail(self, msg: str, critical: bool = False) -> None:
        icon = f"{C_RED}🔴 FAIL:{C_RESET}" if critical else f"{C_RED}❌ FAIL:{C_RESET}"
        print(f"   {icon} {msg}")
        self.failed.append(msg)
        if critical:
            self.critical.append(msg)

    def info(self, msg: str) -> None:
        print(f"   {C_CYAN}ℹ️  INFO:{C_RESET} {msg}")

    # ── Section header ────────────────────────────────────────────────────────

    def section(self, title: str) -> None:
        print()
        print(f"{C_BOLD}{'─' * 72}{C_RESET}")
        print(f"{C_BOLD}  [{title}]{C_RESET}")
        print(f"{C_BOLD}{'─' * 72}{C_RESET}")

    # ── Final verdict ─────────────────────────────────────────────────────────

    def verdict(self) -> int:
        """Print final scorecard. Returns 0 if ready, 1 if not."""
        print()
        print(f"{C_BOLD}{'=' * 72}{C_RESET}")
        print(f"{C_BOLD}  📊 FINAL ASSESSMENT{C_RESET}")
        print(f"{C_BOLD}{'=' * 72}{C_RESET}")

        print(f"\n  {C_GREEN}✅ Passed:      {len(self.passed)}{C_RESET}")
        print(f"  {C_YELLOW}⚠️  Warnings:    {len(self.warnings)}{C_RESET}")
        print(f"  {C_RED}❌ Failed:      {len(self.failed)}{C_RESET}")
        print(f"  {C_RED}🔴 Critical:    {len(self.critical)}{C_RESET}")

        if self.critical:
            print(f"\n  {C_RED}{C_BOLD}CRITICAL ISSUES (MUST FIX BEFORE GOING LIVE):{C_RESET}")
            for i, c in enumerate(self.critical, 1):
                print(f"    {i}. {c}")

        if self.warnings:
            top = self.warnings[:6]
            print(f"\n  {C_YELLOW}WARNINGS (review before live):{C_RESET}")
            for i, w in enumerate(top, 1):
                print(f"    {i}. {w}")
            if len(self.warnings) > 6:
                print(f"    … and {len(self.warnings) - 6} more")

        print()
        print(f"{C_BOLD}{'=' * 72}{C_RESET}")

        if self.critical:
            print(f"  {C_RED}{C_BOLD}🔴 VERDICT: NOT READY FOR LIVE TRADING{C_RESET}")
            print(f"{C_BOLD}{'=' * 72}{C_RESET}")
            print(f"\n  Fix the {len(self.critical)} critical issue(s) above, then re-run this check.\n")
            return 1

        if self.failed:
            print(f"  {C_YELLOW}{C_BOLD}🟡 VERDICT: NOT RECOMMENDED — fix failures first{C_RESET}")
            print(f"{C_BOLD}{'=' * 72}{C_RESET}")
            print(f"\n  {len(self.failed)} non-critical failure(s). Proceed with extreme caution.\n")
            return 1

        if len(self.warnings) > 6:
            print(f"  {C_YELLOW}{C_BOLD}🟡 VERDICT: READY WITH RESERVATIONS{C_RESET}")
            print(f"{C_BOLD}{'=' * 72}{C_RESET}")
            print(f"\n  Review the {len(self.warnings)} warnings before going live.")
            print("  Start with MINIMUM quantity (1 lot) and monitor every trade.\n")
            return 0

        print(f"  {C_GREEN}{C_BOLD}🟢 VERDICT: READY FOR LIVE TRADING{C_RESET}")
        print(f"{C_BOLD}{'=' * 72}{C_RESET}")
        print(f"\n  {C_GREEN}✅ All critical checks passed!{C_RESET}")
        print("  Precautions for first live week:")
        print("    1.  Set TRADING_DEFAULT_QUANTITY=1 (1 lot maximum)")
        print("    2.  Monitor EVERY trade manually for the first 3 days")
        print("    3.  Keep this terminal open: python pre_live_checklist.py")
        print("    4.  Know how to stop the bot: Ctrl+C or kill the python process")
        print("    5.  Review end-of-day P&L in analyze_paper_trades.py\n")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Load real config (may raise if .env is missing / invalid)
# ─────────────────────────────────────────────────────────────────────────────

def _load_settings():
    try:
        from config import settings
        return settings
    except Exception as exc:
        print(f"\n{C_RED}❌  Cannot load config: {exc}{C_RESET}")
        print("   Check that config.py and .env are present and valid.\n")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────

def check_configuration(cl: Checklist, s) -> None:
    cl.section("01 · Configuration")
    t = s.trading

    # Safety mode
    if not t.auto_trade_enabled:
        cl.ok("TRADING_AUTO_TRADE_ENABLED=false  — paper / safe mode ✓")
    else:
        cl.fail("TRADING_AUTO_TRADE_ENABLED=true — live mode is ON. Disable to analyse safely.", critical=True)

    # Capital
    bal = t.account_balance
    cl.info(f"Account balance: ₹{bal:,.0f}")
    if bal >= 100_000:
        cl.ok(f"Capital ₹{bal:,.0f} — adequate for options trading")
    elif bal >= 50_000:
        cl.warn(f"Capital ₹{bal:,.0f} — low; consider ₹1L+ for live trading")
    else:
        cl.fail(f"Capital ₹{bal:,.0f} — too low for options lot sizes", critical=True)

    # Default quantity (lot multiplier)
    qty = t.default_quantity
    cl.info(f"Default quantity: {qty} units per order")
    if qty <= 75:
        cl.ok(f"Default quantity {qty} — reasonable (≤ 1 NIFTY lot of 75)")
    elif qty <= 150:
        cl.warn(f"Default quantity {qty} — equals 2 lots; ensure capital supports this")
    else:
        cl.fail(f"Default quantity {qty} — very high; lower to ≤75 for live start", critical=False)

    # Max positions
    if 1 <= t.max_positions <= 3:
        cl.ok(f"Max open positions: {t.max_positions} (controlled)")
    elif t.max_positions <= 5:
        cl.warn(f"Max open positions: {t.max_positions} — consider ≤3 to start")
    else:
        cl.fail(f"Max open positions: {t.max_positions} — overleveraged for a new live account")

    # Daily loss & per-trade loss
    if t.max_daily_loss > 0:
        pct = t.max_daily_loss / bal * 100
        label = "conservative" if pct <= 3 else ("moderate" if pct <= 5 else "HIGH")
        fn = cl.ok if pct <= 5 else cl.warn
        fn(f"Max daily loss: ₹{t.max_daily_loss:,.0f}  ({pct:.1f}% of capital — {label})")
    else:
        cl.fail("TRADING_MAX_DAILY_LOSS not set", critical=True)

    if t.max_loss_per_trade > 0:
        cl.ok(f"Max loss per trade: ₹{t.max_loss_per_trade:,.0f}")
    else:
        cl.warn("TRADING_MAX_LOSS_PER_TRADE not configured")

    # Stop-loss & target
    cl.info(f"Stop-loss: {t.stop_loss_percentage}%  |  Target: {t.target_percentage}%  "
            f"|  Risk:Reward ≈ 1:{t.target_percentage / t.stop_loss_percentage:.1f}")
    if t.target_percentage / t.stop_loss_percentage >= 2:
        cl.ok(f"Risk:reward ≥ 1:2 (good)")
    else:
        cl.warn("Risk:reward < 1:2 — tighten target or widen stop-loss")

    # Trade limits
    cl.ok(f"Max trades per day: {t.max_trades_per_day}")
    cl.ok(f"Max consecutive losses before pause: {t.max_consecutive_losses}")
    cl.ok(f"Pause duration after losses: {t.pause_after_losses_minutes} min")


def check_broker_credentials(cl: Checklist, s) -> None:
    cl.section("02 · Broker Credentials")

    broker = os.getenv("BROKER", "").lower()
    cl.info(f"Broker: {broker or '(not set)'}")

    if broker == "dhan":
        client_id = os.getenv("DHAN_CLIENT_ID", "")
        token     = os.getenv("DHAN_ACCESS_TOKEN", "")
        if client_id:
            cl.ok(f"DHAN_CLIENT_ID set ({len(client_id)} chars)")
        else:
            cl.fail("DHAN_CLIENT_ID is empty", critical=True)
        if len(token) > 50:
            cl.ok(f"DHAN_ACCESS_TOKEN set ({len(token)} chars — looks like JWT)")
        else:
            cl.fail("DHAN_ACCESS_TOKEN missing or too short", critical=True)
        # Warn about token expiry (Dhan tokens expire periodically)
        cl.warn("Dhan access tokens expire — verify the token is current before going live")

    elif broker == "zerodha":
        api_key    = os.getenv("KITE_API_KEY", "")
        api_secret = os.getenv("KITE_API_SECRET", "")
        access_tok = os.getenv("KITE_ACCESS_TOKEN", "")
        if api_key:   cl.ok(f"KITE_API_KEY set")
        else:          cl.fail("KITE_API_KEY missing", critical=True)
        if api_secret: cl.ok(f"KITE_API_SECRET set")
        else:          cl.fail("KITE_API_SECRET missing", critical=True)
        if len(access_tok) > 10:
            cl.ok("KITE_ACCESS_TOKEN set")
            cl.warn("Kite access tokens expire daily — re-generate before each trading session")
        else:
            cl.fail("KITE_ACCESS_TOKEN missing — required for live Zerodha trading", critical=True)
    else:
        cl.fail(f"BROKER='{broker}' is not recognised (expected 'dhan' or 'zerodha')", critical=True)


def check_paper_trading_results(cl: Checklist) -> None:
    cl.section("03 · Paper Trading Results")

    trades_file = Path("paper_trades.json")
    if not trades_file.exists():
        cl.fail("paper_trades.json not found — run paper trading first", critical=True)
        return

    try:
        with open(trades_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        cl.fail(f"Cannot parse paper_trades.json: {exc}", critical=True)
        return

    trades = data.get("trades", [])
    open_positions = data.get("open_positions", [])

    cl.info(f"Closed trades: {len(trades)}   |   Open positions: {len(open_positions)}")

    if len(trades) == 0:
        cl.fail("No closed paper trades — need at least 20 before going live", critical=True)
        return

    if len(trades) < 20:
        cl.warn(f"Only {len(trades)} closed trades — recommend ≥ 20 for statistical confidence")
    elif len(trades) < 50:
        cl.warn(f"{len(trades)} trades — decent but 50+ gives higher confidence")
    else:
        cl.ok(f"{len(trades)} closed paper trades — good sample size")

    # P&L stats
    pnl_values = [float(t.get("pnl", 0)) for t in trades]
    total_pnl  = sum(pnl_values)
    wins       = [p for p in pnl_values if p > 0]
    losses     = [p for p in pnl_values if p <= 0]
    win_rate   = len(wins) / len(pnl_values) * 100

    if total_pnl > 0:
        cl.ok(f"Total paper P&L: ₹{total_pnl:+,.2f} (profitable ✓)")
    else:
        cl.fail(f"Total paper P&L: ₹{total_pnl:+,.2f} — strategy is LOSING in paper mode", critical=True)

    if win_rate >= 55:
        cl.ok(f"Win rate: {win_rate:.1f}%")
    elif win_rate >= 40:
        cl.warn(f"Win rate: {win_rate:.1f}% — acceptable but borderline")
    else:
        cl.fail(f"Win rate: {win_rate:.1f}% — too low for live trading", critical=True)

    # Profit factor
    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses)) or 0.01
    pf           = gross_profit / gross_loss
    if pf >= 2.0:   cl.ok(f"Profit factor: {pf:.2f} (excellent)")
    elif pf >= 1.5: cl.ok(f"Profit factor: {pf:.2f} (good)")
    elif pf >= 1.2: cl.warn(f"Profit factor: {pf:.2f} (marginal)")
    else:           cl.fail(f"Profit factor: {pf:.2f} — needs improvement before live", critical=True)

    # Max loss
    max_loss = min(pnl_values) if pnl_values else 0
    if max_loss > -500:
        cl.ok(f"Largest single loss: ₹{max_loss:,.2f} (controlled)")
    elif max_loss > -1500:
        cl.warn(f"Largest single loss: ₹{max_loss:,.2f} — review stop-loss")
    else:
        cl.fail(f"Largest single loss: ₹{max_loss:,.2f} — risk too high", critical=True)

    # Average per trade
    avg = total_pnl / len(trades)
    cl.info(f"Average profit per trade: ₹{avg:+,.2f}")

    # Time-stop impact (advisory)
    time_stops = [t for t in trades if (t.get("exit_reason") or "").startswith("TIME_STOP")]
    if time_stops:
        ts_pnl = sum(float(t.get("pnl", 0)) for t in time_stops)
        cl.info(f"TIME_STOP exits: {len(time_stops)} trades, ₹{ts_pnl:+,.2f}")
        if len(time_stops) > len(trades) // 2:
            cl.warn(
                f"{len(time_stops)}/{len(trades)} trades closed by time stop — "
                "run 'python analyze_time_stop.py' to quantify opportunity cost"
            )

    # Per-strategy breakdown
    by_strategy: dict = {}
    for t in trades:
        s = t.get("strategy", "UNKNOWN")
        by_strategy.setdefault(s, {"pnl": 0.0, "n": 0, "wins": 0})
        by_strategy[s]["pnl"]  += float(t.get("pnl", 0))
        by_strategy[s]["n"]    += 1
        by_strategy[s]["wins"] += 1 if float(t.get("pnl", 0)) > 0 else 0

    for strat, stats in sorted(by_strategy.items(), key=lambda x: -x[1]["pnl"]):
        wr = stats["wins"] / stats["n"] * 100
        line = f"{strat}: {stats['n']} trades  WR={wr:.0f}%  P&L=₹{stats['pnl']:+,.2f}"
        if stats["pnl"] >= 0:
            cl.ok(line)
        else:
            cl.warn(f"{line}  ← LOSING strategy — consider disabling")


def check_risk_management(cl: Checklist, s) -> None:
    cl.section("04 · Risk Management Rules")

    t = s.trading

    # Trailing stop
    if t.use_trailing_stop_loss:
        cl.ok(f"Trailing stop-loss ENABLED at {t.trailing_stop_percentage}%")
    else:
        cl.warn("Trailing stop-loss DISABLED — consider enabling for live")

    # Profit tiers
    if t.use_profit_tiers:
        cl.ok(
            f"Profit tiers ENABLED — Tier1: {t.take_profit_tier_1_percent}% (sell {t.take_profit_tier_1_quantity_percent:.0f}%)  "
            f"Tier2: {t.take_profit_tier_2_percent}% (sell remaining {t.take_profit_tier_2_quantity_percent:.0f}%)"
        )
    else:
        cl.warn("Profit tiers DISABLED — full position exits at once (riskier)")

    # VIX filter
    if t.vix_filter_enabled:
        cl.ok(f"VIX filter ENABLED — no new entries when VIX > {t.vix_max}")
        if t.vix_max > 22:
            cl.warn(f"VIX_MAX={t.vix_max} is relatively high — premiums above 22 are expensive for buyers")
    else:
        cl.warn("VIX filter DISABLED — entries will occur even when premiums are expensive")

    # IV percentile
    if t.iv_filter_enabled:
        cl.ok(f"IV percentile filter ENABLED (max {t.iv_percentile_max}th pct)")
    else:
        cl.warn("IV percentile filter DISABLED (set TRADING_IV_FILTER_ENABLED=true after 5+ days of history)")

    # Theta exit
    if t.theta_exit_enabled:
        cl.ok(f"Theta drain monitor ENABLED (threshold ₹{t.theta_exit_threshold}/day)")
    else:
        cl.warn("Theta drain monitor DISABLED")

    # Max consecutive losses
    cl.ok(f"Auto-pause after {t.max_consecutive_losses} consecutive losses ({t.pause_after_losses_minutes} min cool-down)")

    # Max trades per day
    cl.ok(f"Max {t.max_trades_per_day} trades/day cap enforced")

    # Position sizer
    try:
        ps = s.position_sizer
        if ps.enabled:
            cl.ok(
                f"PositionSizer ENABLED — max risk/trade={ps.max_risk_per_trade_pct}%  "
                f"Kelly fraction={ps.kelly_fraction}  "
                f"qty range: ×{ps.min_qty_multiplier}–×{ps.max_qty_multiplier}"
            )
        else:
            cl.warn("PositionSizer DISABLED — fixed quantity used for all trades")
    except AttributeError:
        cl.warn("PositionSizer config not found (POSITION_SIZER_* settings not set)")


def check_time_exits(cl: Checklist, s) -> None:
    cl.section("05 · Time-Based Exit Rules")

    t = s.trading

    if t.hard_time_exit_hour > 0:
        cl.ok(f"Hard time exit at {t.hard_time_exit_hour:02d}:00 IST — positions entered before this hour are closed")
    else:
        cl.warn("Hard time exit DISABLED (TRADING_HARD_TIME_EXIT_HOUR=0) — positions may drift into closing bell")

    if t.time_exit_only_losers:
        cl.ok(f"Smart time exit: only LOSING positions closed at {t.hard_time_exit_hour:02d}:00  "
              f"(winners with P&L ≥ ₹{t.time_exit_min_profit:.0f} let run ✓)")
    else:
        cl.warn(
            "time_exit_only_losers=False — ALL positions close at hard-exit time including winners. "
            "Set TRADING_TIME_EXIT_ONLY_LOSERS=true to let winners run."
        )

    market_close = f"{t.market_close_hour:02d}:{t.market_close_minute:02d}"
    safety_mins  = t.close_all_before_market_close
    cl.ok(f"Market-close auto-exit: all positions closed {safety_mins} min before {market_close}")

    # Time-stop (stagnant trade)
    if t.time_stop_minutes > 0:
        cl.ok(f"Time-stop for stagnant trades: exit after {t.time_stop_minutes} min if no significant move")
    else:
        cl.warn("Stagnant-trade time-stop is DISABLED (TRADING_TIME_STOP_MINUTES=0)")


def check_strategy_config(cl: Checklist, s) -> None:
    cl.section("06 · Strategy Configuration")

    enabled_strategies = []

    try:
        if s.orb.enabled:
            cl.ok(f"ORB: ENABLED — entry window closes at {s.orb.entry_end_hour:02d}:{s.orb.entry_end_minute:02d}  "
                  f"target ×{s.orb.target_multiplier}")
            enabled_strategies.append("ORB")
        else:
            cl.warn("ORB: DISABLED")
    except AttributeError:
        cl.warn("ORB config not found")

    try:
        if s.vwap.enabled:
            cl.ok(f"VWAP: ENABLED — deviation ±{s.vwap.deviation_pct}%  "
                  f"RSI band {s.vwap.rsi_oversold}–{s.vwap.rsi_overbought}")
            enabled_strategies.append("VWAP")
        else:
            cl.warn("VWAP: DISABLED")
    except AttributeError:
        cl.warn("VWAP config not found")

    try:
        if s.gap.enabled:
            cl.ok(f"GAP: ENABLED — min gap {s.gap.min_gap_pct}%  strong gap {s.gap.strong_gap_pct}%  "
                  f"SL {s.gap.stop_loss_pct}%  target {s.gap.target_pct}%")
            enabled_strategies.append("GAP")
        else:
            cl.warn("GAP: DISABLED")
    except AttributeError:
        cl.warn("GAP config not found")

    try:
        if s.eod.enabled:
            cl.ok(f"EOD: ENABLED — entry window {s.eod.entry_start_hour:02d}:{s.eod.entry_start_minute:02d}–"
                  f"{s.eod.entry_end_hour:02d}:{s.eod.entry_end_minute:02d}")
            enabled_strategies.append("EOD")
        else:
            cl.info("EOD: DISABLED (set EOD_ENABLED=true to activate closing-momentum strategy)")
    except AttributeError:
        cl.warn("EOD config not found")

    if not enabled_strategies:
        cl.fail("No strategies are ENABLED — bot will not trade", critical=True)
    else:
        cl.info(f"Active strategies: {', '.join(enabled_strategies)}")


def check_market_data(cl: Checklist, fast: bool = False) -> Optional[dict]:
    """Returns {index: price} dict for downstream checks."""
    cl.section("07 · Market Data Connection  (yfinance)")

    if fast:
        cl.warn("--fast mode: skipping live data fetch")
        return None

    try:
        import yfinance as yf
    except ImportError:
        cl.fail("yfinance not installed — run: pip install yfinance")
        return None

    prices = {}
    tests = [
        ("NIFTY",     "^NSEI"),
        ("BANKNIFTY", "^NSEBANK"),
        ("SENSEX",    "^BSESN"),
        ("VIX",       "^INDIAVIX"),
    ]

    for name, ticker in tests:
        try:
            t0   = time.time()
            data = yf.Ticker(ticker).history(period="1d", interval="1m")
            ms   = int((time.time() - t0) * 1000)
            if not data.empty:
                price = float(data["Close"].iloc[-1])
                prices[name] = price
                cl.ok(f"{name} ({ticker}): ₹{price:,.2f}  [{ms}ms]")
            else:
                cl.warn(f"{name}: empty response from yfinance")
        except Exception as exc:
            cl.fail(f"{name}: cannot fetch data — {exc}")

    if "VIX" in prices:
        vix = prices["VIX"]
        try:
            from config import settings
            vix_max = settings.trading.vix_max
            if vix <= vix_max:
                cl.ok(f"VIX {vix:.2f} ≤ VIX_MAX {vix_max}  — new entries ALLOWED")
            else:
                cl.warn(f"VIX {vix:.2f} > VIX_MAX {vix_max}  — new entries BLOCKED right now")
        except Exception:
            cl.info(f"Current VIX: {vix:.2f}")

    return prices if prices else None


def check_alerts(cl: Checklist, s) -> None:
    cl.section("08 · Alert & Notification System")

    try:
        a = s.alert
        if a.telegram_enabled and a.telegram_bot_token and a.telegram_chat_id:
            cl.ok(f"Telegram alerts ENABLED — chat_id: {a.telegram_chat_id}")
        elif a.telegram_enabled:
            cl.fail("ALERT_TELEGRAM_ENABLED=true but token/chat_id is missing", critical=True)
        else:
            cl.warn("Telegram alerts DISABLED — you won't receive trade notifications on your phone")

        if getattr(a, "telegram2_enabled", False) and a.telegram2_bot_token:
            cl.ok(f"Telegram channel 2 ENABLED — chat_id: {a.telegram2_chat_id}")

        if a.send_daily_report:
            cl.ok(f"Daily P&L report: ENABLED at {a.daily_report_time}")
        else:
            cl.warn("Daily report DISABLED — enable with ALERT_SEND_DAILY_REPORT=true")

        for trigger, attr in [
            ("trade entry", "alert_on_trade_entry"),
            ("trade exit",  "alert_on_trade_exit"),
            ("daily loss",  "alert_on_daily_loss_limit"),
            ("consec. losses", "alert_on_consecutive_losses"),
        ]:
            if getattr(a, attr, False):
                cl.ok(f"  Alert on {trigger}: ✓")
            else:
                cl.warn(f"  Alert on {trigger}: OFF")

    except AttributeError:
        cl.warn("Alert config not found (ALERT_* settings may not be set)")


def check_files_and_storage(cl: Checklist) -> None:
    cl.section("09 · Files & Storage")

    # .env
    if Path(".env").exists():
        cl.ok(".env present")
    else:
        cl.fail(".env file missing — bot cannot load settings", critical=True)

    # paper_trades.json
    if Path("paper_trades.json").exists():
        size = Path("paper_trades.json").stat().st_size
        cl.ok(f"paper_trades.json exists ({size / 1024:.1f} KB)")
    else:
        cl.warn("paper_trades.json not found — no paper trade history yet")

    # CSV export
    if Path("paper_trades_export.csv").exists():
        cl.ok("paper_trades_export.csv exists")
    else:
        cl.warn("paper_trades_export.csv not found (will be created on first trade close)")

    # EMERGENCY_STOP sentinel
    if Path("EMERGENCY_STOP").exists():
        cl.fail("EMERGENCY_STOP file exists — the bot will reject new entries until it is deleted!", critical=True)
    else:
        cl.ok("No EMERGENCY_STOP sentinel — bot is free to run")

    # bot/ modules present
    required_modules = [
        "bot/engine.py",
        "bot/order_manager.py",
        "bot/paper_trader.py",
        "bot/trend_analyzer.py",
        "bot/orb_strategy.py",
        "bot/vwap_strategy.py",
        "bot/gap_detector.py",
        "bot/position_sizer.py",
        "bot/telegram_handler.py",
    ]
    missing = [m for m in required_modules if not Path(m).exists()]
    if missing:
        for m in missing:
            cl.fail(f"Missing module: {m}", critical=True)
    else:
        cl.ok(f"All {len(required_modules)} required bot modules present")

    # Check for any __pycache__ byte-code matching current sources (just verify compilable)
    broken = []
    for pyfile in Path("bot").glob("*.py"):
        try:
            import ast
            ast.parse(pyfile.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            broken.append(f"{pyfile.name}: {exc}")
    if broken:
        for b in broken:
            cl.fail(f"Syntax error in {b}", critical=True)
    else:
        cl.ok(f"All bot/*.py files are syntax-clean")


def check_order_manager_readiness(cl: Checklist, s) -> None:
    cl.section("10 · OrderManager & Emergency Controls")

    # Import-test OrderManager (without a live broker connection)
    try:
        from bot.order_manager import OrderManager, TradeRecord, DailyStats
        cl.ok("OrderManager imported successfully")
    except Exception as exc:
        cl.fail(f"Cannot import OrderManager: {exc}", critical=True)
        return

    # Verify key methods exist
    for method in ["close_all_positions", "check_stop_loss_targets", "can_place_order", "get_daily_summary"]:
        if hasattr(OrderManager, method):
            cl.ok(f"OrderManager.{method}() exists")
        else:
            cl.fail(f"OrderManager.{method}() missing", critical=True)

    # Emergency stop info
    cl.info("Emergency stop options:")
    cl.info("  (A) Keyboard:  Ctrl+C in the bot terminal")
    cl.info("  (B) PowerShell: Get-Process python | Stop-Process")
    cl.info("  (C) Sentinel file: New-Item EMERGENCY_STOP (bot stops opening new trades)")
    cl.info("  (D) Dashboard: manually close all via /api/close_all endpoint")


def check_system_summary(cl: Checklist, prices: Optional[dict]) -> None:
    cl.section("11 · Live System Snapshot")

    now = datetime.now()
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if now.weekday() >= 5:
        cl.warn("Today is a weekend — market is CLOSED")
    elif market_open <= now <= market_close:
        mins_left = int((market_close - now).total_seconds() / 60)
        cl.info(f"Market is OPEN — {mins_left} minutes until close")
    else:
        cl.info(f"Market is CLOSED (session: 09:15–15:30 IST on weekdays)")

    if prices:
        for name, price in prices.items():
            cl.info(f"  {name}: ₹{price:,.2f}")

    # Summarise any warning from the time-stop analysis file
    if Path("paper_trades.json").exists():
        try:
            with open("paper_trades.json") as f:
                data = json.load(f)
            trades = data.get("trades", [])
            ts = [t for t in trades if (t.get("exit_reason") or "").startswith("TIME_STOP")]
            if ts:
                ts_pnl = sum(float(t.get("pnl", 0)) for t in ts)
                cl.info(f"Time-stop summary: {len(ts)} exits  ₹{ts_pnl:+,.2f}  "
                        f"(run: python analyze_time_stop.py for full opportunity-cost report)")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    fast = "--fast" in sys.argv

    print()
    print(f"{C_BOLD}{'=' * 72}{C_RESET}")
    print(f"{C_BOLD}  🚦 PRE-LIVE TRADING COMPREHENSIVE CHECK{C_RESET}")
    print(f"{C_BOLD}{'=' * 72}{C_RESET}")
    print(f"  Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {C_YELLOW}⚠️  DO NOT go live until all critical checks pass!{C_RESET}")

    cl = Checklist()
    s  = _load_settings()

    check_configuration(cl, s)
    check_broker_credentials(cl, s)
    check_paper_trading_results(cl)
    check_risk_management(cl, s)
    check_time_exits(cl, s)
    check_strategy_config(cl, s)
    prices = check_market_data(cl, fast=fast)
    check_alerts(cl, s)
    check_files_and_storage(cl)
    check_order_manager_readiness(cl, s)
    check_system_summary(cl, prices)

    exit_code = cl.verdict()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
