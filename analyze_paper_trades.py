#!/usr/bin/env python3
"""
analyze_paper_trades.py
=======================
Reads paper_trades.json (written by PaperTrader) and prints a complete
strategy performance report with actionable recommendations.

Usage:
    python analyze_paper_trades.py          # full report + auto CSV export
    python analyze_paper_trades.py --csv    # force-regenerate CSV only
    python analyze_paper_trades.py --open   # show open positions only

No extra dependencies — uses Python stdlib only.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
TRADES_FILE = Path("paper_trades.json")
CSV_EXPORT  = Path("paper_trades_export.csv")
SEP  = "=" * 70
SEP2 = "-" * 70

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(num: float, den: float) -> float:
    return num / den * 100 if den else 0.0


def _profit_factor(win_pnl: float, loss_pnl: float) -> float:
    loss_abs = abs(loss_pnl)
    return round(win_pnl / loss_abs, 2) if loss_abs > 0 else 9.99


def _parse_dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None


def _hdr(title: str) -> None:
    print()
    print(SEP)
    print(f"  {title}")
    print(SEP)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_data() -> tuple[list[dict], list[dict]]:
    if not TRADES_FILE.exists():
        print(f"\n❌  {TRADES_FILE} not found — bot has not placed any paper trades yet.")
        print("    Start the bot and wait for market hours (9:15 AM IST).\n")
        sys.exit(0)
    with open(TRADES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("trades", []), data.get("open_positions", [])


# ---------------------------------------------------------------------------
# Analysis sections
# ---------------------------------------------------------------------------

def analyze_overall(trades: list[dict]) -> None:
    _hdr("📊 OVERALL PERFORMANCE")
    total = len(trades)
    if total == 0:
        print("  No closed trades yet.")
        return

    wins      = [t for t in trades if t.get("pnl", 0) > 0]
    losses    = [t for t in trades if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    win_pnl   = sum(t.get("pnl", 0) for t in wins)
    loss_pnl  = sum(t.get("pnl", 0) for t in losses)
    pf        = _profit_factor(win_pnl, loss_pnl)
    avg_pnl   = total_pnl / total

    print(f"  Total Closed Trades : {total}")
    print(f"  Winners             : {len(wins)}  ({_pct(len(wins), total):.1f}%)")
    print(f"  Losers              : {len(losses)}  ({_pct(len(losses), total):.1f}%)")
    print(f"  Total P&L           : ₹{total_pnl:+,.2f}")
    print(f"  Average P&L / trade : ₹{avg_pnl:+,.2f}")
    print(f"  Profit Factor       : {pf:.2f}  (>1.0 = net profitable)")

    if wins:
        avg_win = win_pnl / len(wins)
        best    = max(t.get("pnl", 0) for t in wins)
        print(f"  Avg Win             : ₹{avg_win:+,.2f}  |  Best trade: ₹{best:+,.2f}")
    if losses:
        avg_loss = loss_pnl / len(losses)
        worst    = min(t.get("pnl", 0) for t in losses)
        print(f"  Avg Loss            : ₹{avg_loss:+,.2f}  |  Worst trade: ₹{worst:+,.2f}")


def analyze_strategies(trades: list[dict], recs: list[dict]) -> None:
    _hdr("🎯 STRATEGY BREAKDOWN")
    by_strat: dict[str, list] = defaultdict(list)
    for t in trades:
        by_strat[t.get("strategy", "UNKNOWN")].append(t)

    for strat, tlist in sorted(by_strat.items()):
        wins      = [t for t in tlist if t.get("pnl", 0) > 0]
        losses    = [t for t in tlist if t.get("pnl", 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in tlist)
        win_pnl   = sum(t.get("pnl", 0) for t in wins)
        loss_pnl  = sum(t.get("pnl", 0) for t in losses)
        wr        = _pct(len(wins), len(tlist))
        pf        = _profit_factor(win_pnl, loss_pnl)
        avg_pnl   = total_pnl / len(tlist)

        symbol = "✅" if total_pnl > 0 else "❌"
        print(f"\n  {symbol} {strat}")
        print(f"     Trades     : {len(tlist)}")
        print(f"     Win Rate   : {wr:.1f}%")
        print(f"     Total P&L  : ₹{total_pnl:+,.2f}")
        print(f"     Avg P&L    : ₹{avg_pnl:+,.2f}")
        print(f"     Prof Factor: {pf:.2f}")

        # ── Recommendations ──────────────────────────────────────────────────
        if len(tlist) >= 5 and (wr < 40 or total_pnl <= -300):
            action  = "DISABLE" if total_pnl <= -500 else "REVIEW"
            config  = f"{strat.upper()}_ENABLED=false" if action == "DISABLE" else ""
            recs.append({
                "priority": "🔴 HIGH",
                "action"  : f"{action} strategy: {strat}",
                "reason"  : f"{wr:.0f}% win rate, ₹{total_pnl:+,.0f} total P&L",
                "config"  : config,
            })
        elif len(tlist) >= 5 and wr >= 60 and pf >= 1.5:
            recs.append({
                "priority": "🟢 POSITIVE",
                "action"  : f"Consider scaling up: {strat}",
                "reason"  : f"{wr:.0f}% win rate, profit factor {pf:.2f}",
                "config"  : "",
            })
        elif len(tlist) < 5:
            print(f"     ℹ️  Need ≥5 trades for reliable stats ({len(tlist)} so far)")


def analyze_exits(trades: list[dict], recs: list[dict]) -> None:
    _hdr("🚪 EXIT REASON ANALYSIS")
    by_reason: dict[str, list] = defaultdict(list)
    for t in trades:
        by_reason[t.get("exit_reason", "UNKNOWN")].append(t)

    sl_count     = len(by_reason.get("SL_HIT", []))
    target_count = len(by_reason.get("TARGET_HIT", []))
    total        = len(trades)

    for reason in sorted(by_reason):
        tlist     = by_reason[reason]
        total_pnl = sum(t.get("pnl", 0) for t in tlist)
        n_wins    = sum(1 for t in tlist if t.get("pnl", 0) > 0)
        print(f"  {reason:<22}  {len(tlist):>3} trades  |  "
              f"wins: {n_wins}  |  P&L: ₹{total_pnl:+,.2f}")

    if total > 0:
        if sl_count / total > 0.60:
            recs.append({
                "priority": "🟡 MEDIUM",
                "action"  : "WIDEN Stop Loss percentage",
                "reason"  : (f"{_pct(sl_count, total):.0f}% of trades are hitting SL — "
                             "stop may be too tight for normal intraday volatility"),
                "config"  : "TRADING_STOP_LOSS_PERCENTAGE=22  # raise from current value",
            })
        if total >= 5 and _pct(target_count, total) < 15:
            recs.append({
                "priority": "🟡 MEDIUM",
                "action"  : "LOWER Target percentage",
                "reason"  : (f"Only {_pct(target_count, total):.0f}% of trades reach target — "
                             "targets may be too ambitious; locking in partial gains earlier may help"),
                "config"  : "TRADING_TARGET_PERCENTAGE=40  # reduce from current value",
            })


def analyze_hours(trades: list[dict], recs: list[dict]) -> None:
    _hdr("⏰ TIME-OF-DAY ANALYSIS")
    by_hour: dict[int, list] = defaultdict(list)
    for t in trades:
        dt = _parse_dt(t.get("entry_time", ""))
        by_hour[dt.hour if dt else 9].append(t)

    for hour in sorted(by_hour):
        tlist     = by_hour[hour]
        total_pnl = sum(t.get("pnl", 0) for t in tlist)
        n_wins    = sum(1 for t in tlist if t.get("pnl", 0) > 0)
        wr        = _pct(n_wins, len(tlist))
        bar       = "▓" * int(abs(total_pnl) / 20)  # 1 block per ₹20
        sign      = "+" if total_pnl >= 0 else "-"
        print(f"  {hour:02d}:00–{hour:02d}:59  {len(tlist):>3} trades  "
              f"WR:{wr:5.1f}%  P&L: ₹{total_pnl:+,.2f}  {sign}{bar}")

        if len(tlist) >= 3 and total_pnl < -200:
            recs.append({
                "priority": "🟡 MEDIUM",
                "action"  : f"AVOID entries during {hour:02d}:00–{hour:02d}:59",
                "reason"  : f"Consistently losing ₹{abs(total_pnl):,.0f} in this hour",
                "config"  : "",
            })


def analyze_direction(trades: list[dict], recs: list[dict]) -> None:
    _hdr("📈 CALL vs PUT ANALYSIS")
    by_dir: dict[str, list] = defaultdict(list)
    for t in trades:
        by_dir[t.get("option_type", "?")].append(t)

    for opt in ["CE", "PE"]:
        tlist = by_dir.get(opt, [])
        if not tlist:
            continue
        wins      = [t for t in tlist if t.get("pnl", 0) > 0]
        total_pnl = sum(t.get("pnl", 0) for t in tlist)
        wr        = _pct(len(wins), len(tlist))
        avg_pnl   = total_pnl / len(tlist)
        label     = "Calls (bullish)" if opt == "CE" else "Puts  (bearish)"
        print(f"  {opt}  {label:<18}  {len(tlist):>3} trades  "
              f"WR:{wr:5.1f}%  P&L: ₹{total_pnl:+,.2f}  avg: ₹{avg_pnl:+,.2f}")
        if len(tlist) >= 5 and wr < 35:
            recs.append({
                "priority": "🟡 MEDIUM",
                "action"  : f"REDUCE {opt} trades — only {wr:.0f}% win rate",
                "reason"  : f"Strong direction bias against {opt} — market may be trending the other way",
                "config"  : "",
            })


def analyze_index(trades: list[dict], recs: list[dict]) -> None:
    _hdr("📊 INDEX BREAKDOWN")
    by_index: dict[str, list] = defaultdict(list)
    for t in trades:
        by_index[t.get("index_name", "?")].append(t)

    for idx, tlist in sorted(by_index.items()):
        wins      = [t for t in tlist if t.get("pnl", 0) > 0]
        total_pnl = sum(t.get("pnl", 0) for t in tlist)
        wr        = _pct(len(wins), len(tlist))
        avg_pnl   = total_pnl / len(tlist)
        print(f"  {idx:<12}  {len(tlist):>3} trades  WR:{wr:5.1f}%  "
              f"P&L: ₹{total_pnl:+,.2f}  avg: ₹{avg_pnl:+,.2f}")


def show_open_positions(opens: list[dict]) -> None:
    if not opens:
        return
    _hdr(f"📋 OPEN POSITIONS  ({len(opens)})")
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"  Current time: {now_str} IST\n")
    for pos in opens:
        pnl     = pos.get("pnl", 0.0)
        pnl_pct = pos.get("pnl_pct", 0.0)
        icon    = "🟢" if pnl >= 0 else "🔴"
        entry_t = _parse_dt(pos.get("entry_time", ""))
        age_min = int((datetime.now() - entry_t).total_seconds() / 60) if entry_t else 0
        print(f"  {icon} #{pos.get('id')}  {pos.get('index_name')} {pos.get('option_type')} "
              f"[{pos.get('strategy')}]")
        print(f"       Index entry: {pos.get('index_entry', 0):,.0f}  |  "
              f"Premium entry: ₹{pos.get('premium_entry', 0):.0f}  |  "
              f"Age: {age_min} min")
        print(f"       Live P&L   : ₹{pnl:+.2f}  ({pnl_pct:+.1f}%)")
        print(f"       SL: -{_get_sl_pct():.0f}%  |  Target: +{_get_target_pct():.0f}%")


def _get_sl_pct() -> float:
    """Read SL% from .env without importing full config."""
    try:
        env = Path(".env")
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("TRADING_STOP_LOSS_PERCENTAGE="):
                    return float(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return 20.0


def _get_target_pct() -> float:
    try:
        env = Path(".env")
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("TRADING_TARGET_PERCENTAGE="):
                    return float(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return 50.0


def print_recommendations(recs: list[dict]) -> None:
    _hdr("🎯 ACTIONABLE RECOMMENDATIONS")
    if not recs:
        print("  ✅ No issues found — strategies are performing within acceptable ranges.")
        return
    for i, rec in enumerate(recs, 1):
        print(f"\n  {i}. {rec['priority']}")
        print(f"     Action : {rec['action']}")
        print(f"     Reason : {rec['reason']}")
        if rec.get("config"):
            print(f"     .env   : {rec['config']}")


def export_csv(trades: list[dict]) -> None:
    if not trades:
        print("\n  No closed trades to export.")
        return
    fieldnames = [
        "id", "date", "entry_time", "exit_time", "duration_mins",
        "strategy", "index_name", "option_type",
        "index_entry", "premium_entry", "index_exit",
        "pnl", "pnl_pct", "exit_reason",
    ]
    with open(CSV_EXPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for t in trades:
            entry_dt = _parse_dt(t.get("entry_time", "")) or datetime.now()
            exit_dt  = _parse_dt(t.get("exit_time", ""))
            duration = round((exit_dt - entry_dt).total_seconds() / 60, 1) if exit_dt else 0.0
            w.writerow({
                "id"           : t.get("id"),
                "date"         : entry_dt.strftime("%Y-%m-%d"),
                "entry_time"   : entry_dt.strftime("%H:%M:%S"),
                "exit_time"    : exit_dt.strftime("%H:%M:%S") if exit_dt else "",
                "duration_mins": duration,
                "strategy"     : t.get("strategy", ""),
                "index_name"   : t.get("index_name", ""),
                "option_type"  : t.get("option_type", ""),
                "index_entry"  : t.get("index_entry", 0),
                "premium_entry": t.get("premium_entry", 0),
                "index_exit"   : t.get("index_exit", 0),
                "pnl"          : t.get("pnl", 0),
                "pnl_pct"      : t.get("pnl_pct", 0),
                "exit_reason"  : t.get("exit_reason", ""),
            })
    print(f"\n  ✅ CSV exported → {CSV_EXPORT}  ({len(trades)} trades)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args         = set(sys.argv[1:])
    csv_only     = "--csv"  in args
    open_only    = "--open" in args

    trades, opens = load_data()

    print()
    print(SEP)
    print("  📊 PAPER TRADING ANALYSIS REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(SEP)
    print(f"\n  Closed trades on record : {len(trades)}")
    print(f"  Currently open          : {len(opens)}")

    if csv_only:
        export_csv(trades)
        print()
        return

    if open_only:
        show_open_positions(opens)
        print()
        return

    recs: list[dict] = []

    analyze_overall(trades)
    analyze_strategies(trades, recs)
    analyze_exits(trades, recs)
    analyze_hours(trades, recs)
    analyze_direction(trades, recs)
    analyze_index(trades, recs)
    show_open_positions(opens)
    print_recommendations(recs)

    export_csv(trades)

    print()
    print(SEP)
    print()


if __name__ == "__main__":
    main()
