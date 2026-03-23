"""
analyze_time_stop.py
====================
Analyses the impact of time-based exits on paper trades.

Reads  paper_trades.json  (written by PaperTrader) and shows:
  • What each TIME_STOP trade actually made
  • What it COULD have made if held to its target
  • The opportunity cost per trade and in total
  • A concrete recommendation with .env lines

No third-party libraries required — stdlib only.

Usage
-----
    python analyze_time_stop.py            # full analysis
    python analyze_time_stop.py --summary  # one-liner totals only
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

_TRADES_FILE = "paper_trades.json"
_CSV_FILE    = "paper_trades_export.csv"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_trades() -> list[dict]:
    """Return all closed trades from paper_trades.json."""
    if not Path(_TRADES_FILE).exists():
        print(f"\n❌  {_TRADES_FILE} not found — run the bot first to generate paper trades.")
        sys.exit(1)
    try:
        with open(_TRADES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"\n❌  Cannot parse {_TRADES_FILE}: {exc}")
        sys.exit(1)
    return data.get("trades", [])


def _fmt_pnl(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"₹{sign}{value:,.2f}"


def _duration(entry_iso: str, exit_iso: str) -> str:
    try:
        e = datetime.fromisoformat(entry_iso)
        x = datetime.fromisoformat(exit_iso)
        mins = int((x - e).total_seconds() / 60)
        return f"{mins}m"
    except Exception:
        return "?"


# ─────────────────────────────────────────────────────────────────────────────
# Core analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse(summary_only: bool = False) -> None:
    trades = _load_trades()
    if not trades:
        print("\n📋 No closed trades found in paper_trades.json.")
        return

    # Split by exit type
    time_stop  = [t for t in trades if (t.get("exit_reason") or "").startswith("TIME_STOP")]
    target_hit = [t for t in trades if t.get("exit_reason") == "TARGET_HIT"]
    sl_hit     = [t for t in trades if t.get("exit_reason") == "SL_HIT"]
    other      = [t for t in trades if t not in time_stop + target_hit + sl_hit]

    # ── Quick summary ─────────────────────────────────────────────────────────
    total_actual = sum(t.get("pnl", 0) for t in time_stop)
    total_all    = sum(t.get("pnl", 0) for t in trades)

    print()
    print("=" * 72)
    print("  ⏱️  TIME STOP IMPACT ANALYSIS")
    print("=" * 72)
    print(f"\n  Total trades closed:        {len(trades)}")
    print(f"  ├─ TARGET_HIT:             {len(target_hit)} trades  ({_fmt_pnl(sum(t.get('pnl',0) for t in target_hit))})")
    print(f"  ├─ TIME_STOP exits:        {len(time_stop)} trades  ({_fmt_pnl(total_actual)})")
    print(f"  ├─ SL_HIT:                 {len(sl_hit)} trades  ({_fmt_pnl(sum(t.get('pnl',0) for t in sl_hit))})")
    other_pnl = sum(t.get('pnl',0) for t in other)
    if other:
        print(f"  └─ Other exits:            {len(other)} trades  ({_fmt_pnl(other_pnl)})")
    print(f"\n  Grand total realised P&L:   {_fmt_pnl(total_all)}")

    if not time_stop:
        print("\n  ✅  No TIME_STOP exits found — time exit is not affecting results.\n")
        print("=" * 72)
        return

    if summary_only:
        target_pct = _detect_target_pct(trades)
        total_potential = sum(float(t.get("premium_entry", 0)) * target_pct / 100 for t in time_stop)
        _print_recommendation(time_stop, total_actual, total_potential, total_potential - total_actual)
        return

    # ── Per-trade detail ──────────────────────────────────────────────────────
    target_pct = _detect_target_pct(trades)
    sl_pct     = _detect_sl_pct(trades)

    print(f"\n  Detected config  →  SL: -{sl_pct:.0f}%   Target: +{target_pct:.0f}%")
    print()
    print("-" * 72)
    print(f"  {'#':<4}  {'Strategy':<10}  {'Type':<4}  {'Entry ₹':>8}  "
          f"{'Exit ₹':>8}  {'Duration':<8}  {'Actual P&L':>12}  "
          f"{'Potential P&L':>14}  {'Opp. Cost':>10}")
    print("-" * 72)

    total_potential = 0.0
    total_opp_cost  = 0.0

    for t in time_stop:
        trade_id      = t.get("id", "?")
        strategy      = t.get("strategy", "?")
        opt_type      = t.get("option_type", "?")
        premium_entry = float(t.get("premium_entry", 0))
        actual_pnl    = float(t.get("pnl", 0))
        exit_reason   = t.get("exit_reason", "")
        entry_time    = t.get("entry_time", "")
        exit_time     = t.get("exit_time", "")
        dur           = _duration(entry_time, exit_time)

        # Potential P&L if held all the way to target
        potential_pnl = premium_entry * target_pct / 100
        opp_cost      = potential_pnl - actual_pnl

        total_potential += potential_pnl
        total_opp_cost  += opp_cost

        entry_idx  = float(t.get("index_entry", 0))
        exit_idx   = float(t.get("index_exit",  0))

        print(f"  #{trade_id:<3}  {strategy:<10}  {opt_type:<4}  "
              f"{entry_idx:>8,.0f}  {exit_idx:>8,.0f}  {dur:<8}  "
              f"{_fmt_pnl(actual_pnl):>12}  "
              f"{_fmt_pnl(potential_pnl):>14}  "
              f"{_fmt_pnl(opp_cost):>10}")

    print("-" * 72)
    print(f"  {'TOTAL':<35}  "
          f"{'':>8}  {'':>8}  {'':>8}  "
          f"{_fmt_pnl(total_actual):>12}  "
          f"{_fmt_pnl(total_potential):>14}  "
          f"{_fmt_pnl(total_opp_cost):>10}")
    print()

    # ── What-if scenarios ─────────────────────────────────────────────────────
    print("=" * 72)
    print("  💭 WHAT-IF SCENARIOS")
    print("=" * 72)

    pct_left = (total_opp_cost / total_potential * 100) if total_potential else 0
    multiplier = (total_potential / total_actual) if total_actual > 0 else float("inf")

    print(f"\n  With time stop (actual):        {_fmt_pnl(total_actual)}")
    print(f"  Without time stop (potential):  {_fmt_pnl(total_potential)}")
    print(f"  Opportunity cost:               {_fmt_pnl(total_opp_cost)}")
    if pct_left > 0:
        print(f"\n  ⚠️  Time stop took away {pct_left:.1f}% of potential profit!")
        if multiplier < float("inf"):
            print(f"     Without it you'd have earned {multiplier:.1f}× more from these trades.")

    # ── Per-trade breakdown with context ─────────────────────────────────────
    print()
    print("=" * 72)
    print("  🔍 PER-TRADE BREAKDOWN")
    print("=" * 72)

    for i, t in enumerate(time_stop, 1):
        trade_id      = t.get("id", "?")
        strategy      = t.get("strategy", "?")
        opt_type      = t.get("option_type", "?")
        index_name    = t.get("index_name", "?")
        premium_entry = float(t.get("premium_entry", 0))
        actual_pnl    = float(t.get("pnl", 0))
        entry_idx     = float(t.get("index_entry", 0))
        exit_idx      = float(t.get("index_exit", 0))
        entry_time    = t.get("entry_time", "")
        exit_time     = t.get("exit_time", "")
        exit_reason   = t.get("exit_reason", "")

        potential_pnl = premium_entry * target_pct / 100
        opp_cost      = potential_pnl - actual_pnl

        try:
            entry_fmt = datetime.fromisoformat(entry_time).strftime("%H:%M:%S")
            exit_fmt  = datetime.fromisoformat(exit_time).strftime("%H:%M:%S")
        except Exception:
            entry_fmt = entry_time
            exit_fmt  = exit_time

        print(f"\n  Trade #{trade_id}: {index_name} {opt_type}  [{strategy}]")
        print(f"    Entry: index {entry_idx:,.0f}  at {entry_fmt}  (premium ≈ ₹{premium_entry:.0f})")
        print(f"    Exit:  index {exit_idx:,.0f}  at {exit_fmt}  [{exit_reason}]")
        print(f"    Actual P&L:    {_fmt_pnl(actual_pnl)}")
        print(f"    If hit target: {_fmt_pnl(potential_pnl)}  (+{target_pct:.0f}% on premium)")
        print(f"    Opp. cost:     {_fmt_pnl(opp_cost)}", end="")
        if opp_cost > 0:
            print(f"  ← left on the table! 🚪")
        else:
            print()

    # ── Recommendation ────────────────────────────────────────────────────────
    _print_recommendation(time_stop, total_actual, total_potential, total_opp_cost)


def _detect_target_pct(trades: list[dict]) -> float:
    """Estimate the target % from closed trades (highest pnl_pct / premium ratio)."""
    # Use a safe fallback; try to infer from TARGET_HIT trades
    best = 50.0
    for t in trades:
        if t.get("exit_reason") == "TARGET_HIT":
            prem = float(t.get("premium_entry", 0))
            pnl  = float(t.get("pnl", 0))
            if prem > 0:
                best = round(pnl / prem * 100, 0)
                break
    return best


def _detect_sl_pct(trades: list[dict]) -> float:
    best = 20.0
    for t in trades:
        if t.get("exit_reason") == "SL_HIT":
            prem = float(t.get("premium_entry", 0))
            pnl  = float(t.get("pnl", 0))
            if prem > 0:
                best = abs(round(pnl / prem * 100, 0))
                break
    return best


def _print_recommendation(
    time_stop:       list[dict],
    total_actual:    float,
    total_potential: float = 0.0,
    total_opp_cost:  float = 0.0,
) -> None:
    print()
    print("=" * 72)
    print("  🎯 RECOMMENDATION")
    print("=" * 72)

    impact = total_opp_cost if total_potential > 0 else total_actual
    # Classify all time-stop trades as winners or losers at exit
    winners = [t for t in time_stop if float(t.get("pnl", 0)) > 0]
    losers  = [t for t in time_stop if float(t.get("pnl", 0)) <= 0]

    if total_potential > 0 and total_opp_cost > 500:
        severity = "🔴 HIGH IMPACT"
        action   = "REMOVE or postpone the 1 PM time stop entirely"
    elif total_potential > 0 and total_opp_cost > 100:
        severity = "🟡 MODERATE IMPACT"
        action   = "Enable smart time exit (only close losers at 1 PM)"
    else:
        severity = "🟢 LOW IMPACT"
        action   = "Monitor — time stop is not costing much yet"

    print(f"\n  {severity}")
    print(f"  Recommended action: {action}\n")

    print("  Winning trades closed by time stop:  ", len(winners))
    print("  Losing  trades closed by time stop:  ", len(losers))

    if winners:
        print(f"\n  → These {len(winners)} winners were profitable at exit but could have earned more.")
        print("    The FIX below prevents the bot from closing them early.")

    print()
    print("  ─── Apply these changes to your .env ──────────────────────────────")
    print()
    print("  # Keep 1 PM time stop ON for losing positions (avoids theta decay)")
    print("  TRADING_HARD_TIME_EXIT_HOUR=13")
    print()
    print("  # ✨ NEW: Only close losers — let winners run to their target")
    print("  TRADING_TIME_EXIT_ONLY_LOSERS=true")
    print("  TRADING_TIME_EXIT_MIN_PROFIT=50   # ₹ profit threshold to 'let run'")
    print()
    print("  ─── Status ─────────────────────────────────────────────────────────")
    _check_current_env()
    print()
    print("=" * 72)


def _check_current_env() -> None:
    """Read .env and show whether the fix is already applied."""
    env_path = Path(".env")
    if not env_path.exists():
        print("  ⚠️  .env not found — cannot check current settings")
        return

    content_lower = env_path.read_text(encoding="utf-8").lower().replace(" ", "")
    has_only_losers = "trading_time_exit_only_losers=true" in content_lower
    has_min_profit  = "trading_time_exit_min_profit" in content_lower

    if has_only_losers and has_min_profit:
        print("  ✅  Smart time-exit is ALREADY enabled in your .env — nice!")
    elif has_only_losers:
        print("  ⚠️  TRADING_TIME_EXIT_ONLY_LOSERS=true is set but TRADING_TIME_EXIT_MIN_PROFIT is missing")
    else:
        print("  ❌  Smart time-exit NOT yet applied — add the lines above to .env, then restart the bot.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    summary_only = "--summary" in sys.argv
    analyse(summary_only=summary_only)
