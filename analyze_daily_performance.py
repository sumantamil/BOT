"""
Analyze daily performance to find pattern
"""
import json
from pathlib import Path
from collections import defaultdict


def analyze_daily():
    """Show P&L by day to find patterns"""

    if not Path("paper_trades.json").exists():
        print("No paper_trades.json found")
        return

    with open("paper_trades.json") as f:
        data = json.load(f)

    raw = data["trades"] if isinstance(data, dict) and "trades" in data else data
    trades = [t for t in raw if t.get("status") == "CLOSED"]

    # Group by date
    daily_pnl = defaultdict(float)
    daily_trades = defaultdict(list)

    for trade in trades:
        date = trade["entry_time"][:10]
        daily_pnl[date] += trade.get("pnl", 0)
        daily_trades[date].append(trade)

    print("\n" + "=" * 70)
    print("DAILY PERFORMANCE")
    print("=" * 70)

    total_days = len(daily_pnl)
    if total_days == 0:
        print("No closed trades found.")
        return

    winning_days = sum(1 for pnl in daily_pnl.values() if pnl > 0)
    losing_days = sum(1 for pnl in daily_pnl.values() if pnl < 0)

    print(f"\nTotal Trading Days : {total_days}")
    print(f"Winning Days       : {winning_days} ({winning_days / total_days * 100:.0f}%)")
    print(f"Losing Days        : {losing_days} ({losing_days / total_days * 100:.0f}%)")
    print(f"\nDaily Breakdown:")
    print("-" * 70)

    cumulative = 0.0
    for date in sorted(daily_pnl.keys()):
        pnl = daily_pnl[date]
        cumulative += pnl
        day_trades = daily_trades[date]
        num_trades = len(day_trades)
        wins = sum(1 for t in day_trades if t.get("pnl", 0) > 0)
        wr = wins / num_trades * 100 if num_trades else 0

        status = "GOOD" if pnl > 0 else ("FLAT" if pnl == 0 else "BAD ")
        print(
            f"[{status}] {date}  P&L=Rs{pnl:+8,.0f}  cum=Rs{cumulative:+9,.0f}  "
            f"Trades={num_trades}  WR={wr:.0f}%"
        )

        # Show each trade
        strategy_key = "strategy" if "strategy" in day_trades[0] else "source"
        for i, t in enumerate(day_trades, 1):
            src = t.get(strategy_key) or t.get("source") or "?"
            opt = t.get("option_type", "?")
            tpnl = t.get("pnl", 0)
            reason = (t.get("exit_reason") or "?")[:20]
            idx = t.get("index_name", "")
            print(f"       {i}. {src:10} {opt} {idx:10} Rs{tpnl:+7,.0f}  ({reason})")

    print("\n" + "=" * 70)

    # Patterns summary
    print("\nPATTERN SUMMARY:")
    all_by_strategy = defaultdict(list)
    for t in trades:
        key = t.get("strategy") or t.get("source") or "?"
        all_by_strategy[key].append(t)

    for strat, st in sorted(all_by_strategy.items()):
        wins = sum(1 for t in st if t.get("pnl", 0) > 0)
        pnl = sum(t.get("pnl", 0) for t in st)
        wr = wins / len(st) * 100
        print(f"   {strat:<15}: {len(st):3} trades  WR={wr:.0f}%  PnL=Rs{pnl:+,.0f}")

    print()
    # Time of day pattern
    print("TIME-OF-DAY PATTERN:")
    hour_map = defaultdict(list)
    for t in trades:
        try:
            hr = int(t["entry_time"][11:13])
        except Exception:
            hr = -1
        hour_map[hr].append(t)
    for hr in sorted(hour_map):
        sub = hour_map[hr]
        wins = sum(1 for t in sub if t.get("pnl", 0) > 0)
        pnl = sum(t.get("pnl", 0) for t in sub)
        wr = wins / len(sub) * 100
        flag = " <-- avoid" if wr < 50 or pnl < 0 else ""
        print(f"   {hr:02d}:xx  {len(sub):2} trades  WR={wr:.0f}%  PnL=Rs{pnl:+8,.0f}{flag}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    analyze_daily()
