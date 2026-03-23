#!/usr/bin/env python3
"""
check_gap.py
============
Diagnose the gap detection system using the bot's actual gap_detector module.

Usage:
    python check_gap.py              # today's gap for all 3 indices
    python check_gap.py --history    # last 30 days of tradeable gaps
    python check_gap.py --scenarios  # run built-in test scenarios

No external state needed — reads directly from yfinance + config.
"""
from __future__ import annotations

import sys
from datetime import datetime, date, timedelta
from pathlib import Path

SEP  = "=" * 70
SEP2 = "-" * 70

# ---------------------------------------------------------------------------
# Bootstrap path so we can import bot modules from the project root
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

from config import settings
from bot.gap_detector import (
    GapDetector, calculate_gap_size, get_gap_signal,
    GapCategory, GapType,
)
from bot.index_config import NIFTY, BANKNIFTY, SENSEX


def _hdr(title: str) -> None:
    print(); print(SEP); print(f"  {title}"); print(SEP)


# ---------------------------------------------------------------------------
# Section 1 — Config sanity
# ---------------------------------------------------------------------------

def show_config() -> None:
    _hdr("⚙️  GAP STRATEGY CONFIGURATION")
    cfg = settings.gap
    enabled      = cfg.enabled
    min_pct      = cfg.min_gap_pct
    strong_pct   = cfg.strong_gap_pct
    sl_pct       = cfg.stop_loss_pct
    target_pct   = cfg.target_pct
    amo_enabled  = getattr(settings.trading, "amo_enabled", False)

    print(f"  GAP_ENABLED          : {'✅ YES' if enabled else '❌ NO'}")
    print(f"  GAP_MIN_GAP_PCT      : {min_pct}%   (weak gap threshold)")
    print(f"  GAP_STRONG_GAP_PCT   : {strong_pct}%  (immediate entry threshold)")
    print(f"  GAP_STOP_LOSS_PCT    : {sl_pct}%")
    print(f"  GAP_TARGET_PCT       : {target_pct}%")
    print(f"  AMO_ENABLED          : {'✅ YES' if amo_enabled else '❌ NO'}")

    print()
    if not enabled:
        print("  ⚠️  Gap strategy disabled. Add GAP_ENABLED=true to .env to activate.")
        return

    # Threshold sanity hints
    if min_pct < 0.4:
        print(f"  ⚠️  MIN_GAP_PCT={min_pct}% is below 0.4% — may produce false signals")
    if min_pct > 1.0:
        print(f"  ⚠️  MIN_GAP_PCT={min_pct}% is above 1.0% — may miss normal breakaway gaps")
    if strong_pct < 1.2:
        print(f"  ⚠️  STRONG_GAP_PCT={strong_pct}% is low — many gaps will be classified Strong")
    if strong_pct > 2.5:
        print(f"  ⚠️  STRONG_GAP_PCT={strong_pct}% is high — very few Exhaustion gaps classified")

    # Print the decision table
    print()
    print(f"  Decision table:")
    print(f"  ┌────────────────────────────────────────────────────────────────┐")
    print(f"  │ Gap < {min_pct}%            → NEUTRAL  (no trade)               │")
    print(f"  │ {min_pct}% ≤ gap < {strong_pct}%  → Moderate  (wait for ORB breakout)  │")
    print(f"  │ gap ≥ {strong_pct}%           → Strong    (enter CE/PE immediately)    │")
    print(f"  │ gap ≥ 2.5% (Exhaustion) → Fade       (PE if gap-up, CE if gap-down) │")
    print(f"  └────────────────────────────────────────────────────────────────┘")


# ---------------------------------------------------------------------------
# Section 2 — Today's gap for all indices
# ---------------------------------------------------------------------------

def show_todays_gap() -> None:
    _hdr("📊 TODAY'S GAP STATUS")
    print(f"  Analysing: {date.today()}  ({datetime.now().strftime('%H:%M:%S')} IST)\n")

    for idx in [NIFTY, BANKNIFTY, SENSEX]:
        print(f"  {idx.display_name}  ({idx.yahoo_symbol})")
        print(f"  {SEP2}")
        detector = GapDetector(index_config=idx)
        result   = detector.analyze()

        if result is None:
            print(f"  ❌  Could not fetch data (market may be closed / yfinance error)")
            print()
            continue

        direction_icon = "📈" if result.gap_pct > 0 else "📉" if result.gap_pct < 0 else "↔️"
        print(f"  Previous Close : ₹{result.prev_close:,.2f}")
        print(f"  Today's Open   : ₹{result.open_price:,.2f}")
        print(f"  Gap            : ₹{result.gap_points:+,.2f}  ({result.gap_pct:+.2f}%)  {direction_icon}")
        print(f"  Classification : {result.gap_type.value}")
        print(f"  Trade Signal   : {result.trade_direction}")
        print(f"  Wait for ORB?  : {'YES' if result.wait_for_confirmation else 'NO — enter now'}")
        print(f"  Note           : {result.note}")
        print()

        # Also compute enhanced category
        info = calculate_gap_size(result.open_price, result.prev_close)
        now_mins = max(0, (datetime.now().hour * 60 + datetime.now().minute) - (9 * 60 + 15))
        sig  = get_gap_signal(info, result.open_price, now_mins)
        print(f"  Enhanced info  : {info['category']}  /  {info['strength']}")
        if sig:
            print(f"  Signal         : {sig['strategy']}  →  {sig['direction']}")
            print(f"  Confidence     : {sig['confidence']}%")
            print(f"  Reasoning      : {sig['reasoning']}")
        else:
            print(f"  get_gap_signal : No signal at this time-since-open ({now_mins} min)")
        print()


# ---------------------------------------------------------------------------
# Section 3 — Historical gaps (last N trading days)
# ---------------------------------------------------------------------------

def show_historical_gaps(days: int = 30) -> None:
    _hdr(f"📅 HISTORICAL GAPS — LAST {days} TRADING DAYS  (NIFTY 50)")
    import yfinance as yf
    import pandas as pd

    ticker = yf.Ticker(NIFTY.yahoo_symbol)
    hist   = ticker.history(period=f"{days + 10}d", interval="1d")
    if hist.empty or len(hist) < 2:
        print("  ❌ Insufficient data from yfinance")
        return

    hist = hist.tail(days + 1)
    cfg  = settings.gap
    rows = []
    for i in range(1, len(hist)):
        prev  = float(hist["Close"].iloc[i - 1])
        open_ = float(hist["Open"].iloc[i])
        info  = calculate_gap_size(open_, prev)
        rows.append({
            "date"    : hist.index[i].date(),
            "gap_pct" : info["gap_pct"],
            "category": info["category"],
            "strength": info["strength"],
            "dir"     : info["direction"],
        })

    tradeable = [r for r in rows if r["category"] not in ("NONE", "COMMON")]
    print(f"  Total days    : {len(rows)}")
    print(f"  Tradeable gaps: {len(tradeable)}  (BREAKAWAY / RUNAWAY / EXHAUSTION)")
    print()

    category_counts: dict = {}
    for r in tradeable:
        category_counts[r["category"]] = category_counts.get(r["category"], 0) + 1
    if category_counts:
        print("  Category breakdown:")
        for cat, n in sorted(category_counts.items(), key=lambda x: -x[1]):
            bar = "▓" * n
            print(f"    {cat:<12} {n:>3}  {bar}")
        print()

    print("  Recent tradeable gaps:")
    for r in tradeable[-10:]:
        icon = "📈" if r["dir"] == "UP" else "📉"
        print(f"    {r['date']}  {icon} {r['gap_pct']:+.2f}%  {r['category']:<12}  {r['strength']}")


# ---------------------------------------------------------------------------
# Section 4 — Scenario tests
# ---------------------------------------------------------------------------

def run_scenarios() -> None:
    _hdr("🧪 GAP SCENARIO TESTS")

    SCENARIOS = [
        ("No gap",            23000, 23035,  "NONE",       None),
        ("Common (+0.5%)",    23000, 23115,  "COMMON",     None),
        ("Breakaway (+1.2%)", 23000, 23276,  "BREAKAWAY",  "CE"),
        ("Runaway (+2.0%)",   23000, 23460,  "RUNAWAY",    "CE"),
        ("Exhaustion (+3%)",  23000, 23690,  "EXHAUSTION", "PE"),   # fade → PE
        ("Breakaway down",    23000, 22724,  "BREAKAWAY",  "PE"),
        ("Runaway down",      23000, 22540,  "RUNAWAY",    "PE"),
        ("Exhaustion down",   23000, 22310,  "EXHAUSTION", "CE"),   # fade → CE
    ]

    passed = failed = 0
    for name, prev, open_, exp_cat, exp_dir in SCENARIOS:
        info      = calculate_gap_size(open_, prev)
        sig       = get_gap_signal(info, open_, time_since_open=5)
        got_cat   = info["category"]
        got_dir   = sig["direction"] if sig else None

        cat_ok = got_cat == exp_cat
        dir_ok = (got_dir == exp_dir) or (exp_dir is None and got_dir is None) or (exp_dir is not None)
        ok     = cat_ok and (exp_dir is None or got_dir == exp_dir)

        icon = "✅" if ok else "❌"
        print(f"  {icon}  {name:<28}  gap={info['gap_pct']:+.2f}%  "
              f"cat={got_cat:<12}  dir={got_dir}")
        if not ok:
            print(f"       Expected: cat={exp_cat}  dir={exp_dir}")
            failed += 1
        else:
            passed += 1

    print()
    print(f"  Passed: {passed}/{passed+failed}")
    if failed == 0:
        print("  ✅ All scenarios pass — gap classification logic is correct.")


# ---------------------------------------------------------------------------
# Section 5 — AMO order logic check
# ---------------------------------------------------------------------------

def check_amo() -> None:
    _hdr("📋 AMO ORDER READINESS")
    now   = datetime.now().time()
    from datetime import time as dtime
    amo_start = dtime(7, 45)
    amo_end   = dtime(9, 0)
    mkt_open  = dtime(9, 15)

    print(f"  Current time    : {now.strftime('%H:%M:%S')} IST")
    print(f"  AMO window      : 07:45 – 09:00 IST")
    print(f"  Market open     : 09:15 IST")
    print()

    amo_enabled = getattr(settings.trading, "amo_enabled", False)
    print(f"  TRADING_AMO_ENABLED : {'✅ YES' if amo_enabled else '❌ NO (set AMO_ENABLED=true)'}")

    if amo_start <= now <= amo_end:
        print()
        print("  ✅ Currently IN AMO window.")
        print("  The bot will detect today's gap and place an AMO order now.")
        print("  Order executes at 9:15 AM when the market opens.")
    elif now < amo_start:
        delta = (datetime.combine(date.today(), amo_start) - datetime.now())
        mins  = int(delta.total_seconds() / 60)
        print(f"\n  ⏳ AMO window opens in {mins} minutes.")
    elif now > mkt_open:
        print("\n  ℹ️  Market already open — AMO window has passed.")
        print("  Gap trades now enter via normal intraday order at open.")

    print()
    print("  How AMO gap orders work in this bot:")
    print("    1. Gap detected via GapDetector.analyze() at startup")
    print("    2. Engine calls _execute_option_with_research() with source='GAP'")
    print("    3. DhanBroker.place_order() routes as AMO when AMO_ENABLED=true")
    print("       and current time is in the pre-market window")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = set(sys.argv[1:])
    history   = "--history"   in args
    scenarios = "--scenarios" in args

    print()
    print(SEP)
    print("  🔍 GAP STRATEGY DIAGNOSTIC")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')} IST")
    print(SEP)

    show_config()

    if scenarios:
        run_scenarios()
    elif history:
        show_historical_gaps()
    else:
        show_todays_gap()
        check_amo()
        print()
        print("  Tip: run with --history to see last 30 days of gaps")
        print("       run with --scenarios to verify classification logic")

    print()


if __name__ == "__main__":
    main()
