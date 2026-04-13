"""
Analyze bot losses and identify root causes
"""
import json
import pandas as pd
from datetime import datetime
from pathlib import Path


def analyze_paper_trades():
    """Analyze paper trades to find loss patterns"""

    if not Path("paper_trades.json").exists():
        print("No paper_trades.json found. Run bot first.")
        return

    with open("paper_trades.json") as f:
        data = json.load(f)

    # Support both flat list and nested structure
    raw = data["trades"] if isinstance(data, dict) and "trades" in data else data
    trades = [t for t in raw if t.get("status") == "CLOSED"]

    df = pd.DataFrame(trades)

    # Normalise field names: paper_trades uses 'strategy', scripts use 'source'
    if "strategy" in df.columns and "source" not in df.columns:
        df["source"] = df["strategy"]
    if "premium_entry" in df.columns and "entry_price" not in df.columns:
        df["entry_price"] = df["premium_entry"]
    if "premium_exit" in df.columns and "exit_price" not in df.columns:
        df["exit_price"] = df["premium_exit"]
    elif "exit_price" not in df.columns:
        df["exit_price"] = float("nan")

    print("\n" + "=" * 70)
    print("LOSS ANALYSIS REPORT")
    print("=" * 70)

    # 1. Overall stats
    total_trades = len(df)
    if total_trades == 0:
        print("No closed trades found.")
        return

    winning_trades = len(df[df["pnl"] > 0])
    losing_trades = len(df[df["pnl"] < 0])
    break_even = len(df[df["pnl"] == 0])

    win_rate = winning_trades / total_trades * 100
    total_pnl = df["pnl"].sum()
    avg_win = df[df["pnl"] > 0]["pnl"].mean() if winning_trades > 0 else 0
    avg_loss = abs(df[df["pnl"] < 0]["pnl"].mean()) if losing_trades > 0 else 0

    print(f"\n1) OVERALL STATS")
    print(f"   Total Trades  : {total_trades}")
    print(f"   Winning       : {winning_trades} ({win_rate:.1f}%)")
    print(f"   Losing        : {losing_trades} ({100 - win_rate:.1f}%)")
    print(f"   Break-even    : {break_even}")
    print(f"   Total P&L     : Rs{total_pnl:+,.0f}")
    print(f"   Avg Win       : Rs{avg_win:,.0f}")
    print(f"   Avg Loss      : Rs{avg_loss:,.0f}")
    print(f"   Profit Factor : {avg_win / max(avg_loss, 0.1):.2f}x")

    # 2. By strategy/source
    print(f"\n2) LOSSES BY STRATEGY")
    for strategy in sorted(df["source"].unique()):
        st = df[df["source"] == strategy]
        st_wins = len(st[st["pnl"] > 0])
        st_losses = len(st[st["pnl"] < 0])
        st_loss_total = st[st["pnl"] < 0]["pnl"].sum()
        print(f"\n   {strategy}:")
        print(f"      Trades      : {len(st)}")
        print(f"      Win Rate    : {st_wins / len(st) * 100:.1f}%")
        print(f"      Losing      : {st_losses}")
        print(f"      Total Loss  : Rs{st_loss_total:,.0f}")

    # 3. Exit reasons for losing trades
    print(f"\n3) LOSING TRADES - EXIT REASONS")
    losing = df[df["pnl"] < 0]
    if len(losing) > 0:
        for reason, count in losing["exit_reason"].value_counts().items():
            loss_amt = losing[losing["exit_reason"] == reason]["pnl"].sum()
            print(f"   {reason}: {count} trades  Rs{loss_amt:,.0f}")
    else:
        print("   No losing trades!")

    # 4. Time analysis
    print(f"\n4) LOSSES BY TIME OF DAY")
    df["entry_hour"] = pd.to_datetime(df["entry_time"]).dt.hour
    by_hour = df.groupby("entry_hour")["pnl"].agg(["count", "sum", "mean"])
    for hour, row in by_hour.iterrows():
        status = "BAD " if row["mean"] < 0 else "GOOD"
        print(
            f"   {int(hour):02d}:00-{int(hour):02d}:59 [{status}]: "
            f"{int(row['count'])} trades  avg Rs{row['mean']:+,.0f}"
        )

    # 5. Biggest losses
    print(f"\n5) BIGGEST LOSSES (top 5)")
    worst_cols = ["entry_time", "source", "option_type", "entry_price", "exit_price", "pnl"]
    worst_cols = [c for c in worst_cols if c in df.columns]
    for _, trade in df.nsmallest(5, "pnl")[worst_cols].iterrows():
        ep = trade.get("entry_price", 0)
        xp = trade.get("exit_price", 0)
        print(
            f"   {trade['entry_time']}: {trade['source']} {trade['option_type']} "
            f"Entry={ep:.2f}  Exit={xp:.2f}  Loss=Rs{trade['pnl']:,.0f}"
        )

    # 6. Root cause
    print(f"\n6) ROOT CAUSE ANALYSIS")
    any_flag = False

    gap_trades = df[df["source"].str.upper().str.contains("GAP", na=False)]
    if len(gap_trades) > 0:
        gap_wr = len(gap_trades[gap_trades["pnl"] > 0]) / len(gap_trades) * 100
        if gap_wr < 40:
            print(f"   WARNING GAP: {gap_wr:.0f}% win rate (below 50% target)")
            print(f"      Gap strategy firing on noise — check min_gap_pct settings")
            any_flag = True

    early_trades = df[df["entry_hour"].between(9, 11)]
    if len(early_trades) > 0:
        early_wr = len(early_trades[early_trades["pnl"] > 0]) / len(early_trades) * 100
        if early_wr < 45:
            print(f"   WARNING EARLY ENTRIES (9-11 AM): {early_wr:.0f}% win rate")
            print(f"      Morning session choppy — raise entry filter threshold")
            any_flag = True

    late_trades = df[df["entry_hour"] >= 14]
    if len(late_trades) > 0:
        late_wr = len(late_trades[late_trades["pnl"] > 0]) / len(late_trades) * 100
        if late_wr < 45:
            print(f"   WARNING LATE ENTRIES (14:00+): {late_wr:.0f}% win rate — reduce EOD trading")
            any_flag = True

    sl_hits = len(losing[losing["exit_reason"] == "SL_HIT"]) if len(losing) > 0 else 0
    if len(losing) > 0 and sl_hits / len(losing) > 0.7:
        print(f"   WARNING {sl_hits / len(losing) * 100:.0f}% of losses are SL hits — SL too tight or bad entries")
        any_flag = True

    # TIME_STOP dominance
    ts_count = len(df[df["exit_reason"] == "TIME_STOP_13:00"])
    ts_pnl = df[df["exit_reason"] == "TIME_STOP_13:00"]["pnl"].sum()
    if ts_count > 0:
        print(f"   INFO TIME_STOP: {ts_count} trades exited at 13:00, total PnL Rs{ts_pnl:+,.0f}")
        print(f"      Most of these are tiny wins/losses — trades not hitting target or SL before 13:00")
        print(f"      Consider: tighter target, or earlier time-stop at 12:30")
        any_flag = True

    if not any_flag:
        print("   No critical issues detected.")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    analyze_paper_trades()
