"""
visualize_time_impact.py
========================
Visual comparison of actual P&L vs. potential P&L broken down by exit type.

Requires matplotlib (not bundled with the bot):
    pip install matplotlib

Falls back to an ASCII bar chart if matplotlib is not installed.

Usage
-----
    python visualize_time_impact.py           # show charts (+ save PNG)
    python visualize_time_impact.py --ascii   # force ASCII output
    python visualize_time_impact.py --save    # save PNG without showing window
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_TRADES_FILE = "paper_trades.json"
_PNG_FILE    = "time_stop_impact.png"


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_trades() -> list[dict]:
    if not Path(_TRADES_FILE).exists():
        print(f"\n❌  {_TRADES_FILE} not found — run the bot first.")
        sys.exit(1)
    with open(_TRADES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("trades", [])


def _detect_target_pct(trades: list[dict]) -> float:
    for t in trades:
        if t.get("exit_reason") == "TARGET_HIT":
            prem = float(t.get("premium_entry", 0))
            pnl  = float(t.get("pnl", 0))
            if prem > 0:
                return round(pnl / prem * 100, 0)
    return 50.0


def _build_groups(trades: list[dict], target_pct: float) -> dict:
    time_stops  = [t for t in trades if (t.get("exit_reason") or "").startswith("TIME_STOP")]
    target_hits = [t for t in trades if t.get("exit_reason") == "TARGET_HIT"]
    sl_hits     = [t for t in trades if t.get("exit_reason") == "SL_HIT"]
    other       = [t for t in trades if t not in time_stops + target_hits + sl_hits]

    def actual_pnl(lst):   return sum(float(t.get("pnl", 0)) for t in lst)
    def potential(lst):    return sum(float(t.get("premium_entry", 0)) * target_pct / 100 for t in lst)

    return {
        "time_stop":  {"trades": time_stops,  "actual": actual_pnl(time_stops),  "potential": potential(time_stops)},
        "target_hit": {"trades": target_hits, "actual": actual_pnl(target_hits), "potential": actual_pnl(target_hits)},
        "sl_hit":     {"trades": sl_hits,     "actual": actual_pnl(sl_hits),     "potential": 0.0},
        "other":      {"trades": other,        "actual": actual_pnl(other),       "potential": 0.0},
    }


# ─────────────────────────────────────────────────────────────────────────────
# ASCII fallback
# ─────────────────────────────────────────────────────────────────────────────

def _ascii_bar(value: float, max_abs: float, width: int = 40) -> str:
    if max_abs == 0:
        return " " * width
    filled = int(abs(value) / max_abs * width)
    bar = ("█" * filled).ljust(width)
    return bar


def _print_ascii_chart(groups: dict, target_pct: float) -> None:
    print()
    print("=" * 72)
    print("  📊 TIME STOP IMPACT — ASCII CHART")
    print(f"     (Potential = if every trade hit +{target_pct:.0f}% target)")
    print("=" * 72)

    rows = [
        ("TIME_STOP  actual",    groups["time_stop"]["actual"],    "🟠"),
        ("TIME_STOP  potential", groups["time_stop"]["potential"],  "🟢"),
        ("TARGET_HIT actual",    groups["target_hit"]["actual"],    "🟢"),
        ("SL_HIT     actual",    groups["sl_hit"]["actual"],        "🔴"),
    ]

    max_abs = max(abs(r[1]) for r in rows) or 1.0

    print()
    for label, value, icon in rows:
        bar  = _ascii_bar(value, max_abs)
        sign = "+" if value >= 0 else ""
        print(f"  {icon} {label:<24} {bar}  ₹{sign}{value:,.2f}")

    print()
    ts_a = groups["time_stop"]["actual"]
    ts_p = groups["time_stop"]["potential"]
    opp  = ts_p - ts_a
    if opp > 0:
        pct = opp / ts_p * 100 if ts_p else 0
        print(f"  ⚠️  Time stop cost ₹{opp:,.2f} ({pct:.1f}% of potential profit)")
    else:
        print("  ✅  Time stop had minimal impact on this dataset")
    print()
    print("=" * 72)


# ─────────────────────────────────────────────────────────────────────────────
# Matplotlib charts
# ─────────────────────────────────────────────────────────────────────────────

def _plot_charts(groups: dict, target_pct: float, save_only: bool = False) -> None:
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        print("\n⚠️  matplotlib not installed — falling back to ASCII output.")
        print("   Install with:  pip install matplotlib\n")
        _print_ascii_chart(groups, target_pct)
        return

    ts_actual    = groups["time_stop"]["actual"]
    ts_potential = groups["time_stop"]["potential"]
    tgt_actual   = groups["target_hit"]["actual"]
    sl_actual    = groups["sl_hit"]["actual"]
    opp_cost     = ts_potential - ts_actual

    ts_count  = len(groups["time_stop"]["trades"])
    tgt_count = len(groups["target_hit"]["trades"])
    sl_count  = len(groups["sl_hit"]["trades"])

    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    fig.suptitle("Paper Trade Time-Stop Impact Analysis", fontsize=15, fontweight="bold", y=1.01)

    ORANGE = "#f59e0b"
    GREEN  = "#22c55e"
    RED    = "#ef4444"
    GREY   = "#94a3b8"

    # ── Chart 1: Total P&L by exit type ───────────────────────────────────────
    ax1 = axes[0]
    labels1  = ["TIME_STOP\n(actual)", "TIME_STOP\n(potential)", "TARGET_HIT", "SL_HIT"]
    values1  = [ts_actual, ts_potential, tgt_actual, sl_actual]
    colors1  = [ORANGE, GREEN, GREEN, RED]
    bars1    = ax1.bar(labels1, values1, color=colors1, alpha=0.82, edgecolor="white", linewidth=1.2)
    ax1.axhline(0, color="#475569", linewidth=0.8, linestyle="--")
    ax1.set_title("Total P&L by Exit Type", fontweight="bold")
    ax1.set_ylabel("P&L (₹)")
    ax1.grid(axis="y", alpha=0.3, linestyle=":")
    for bar, val in zip(bars1, values1):
        h = bar.get_height()
        offset = max(abs(h) * 0.03, 5)
        sign = "+" if val >= 0 else ""
        ax1.text(bar.get_x() + bar.get_width() / 2, h + offset if h >= 0 else h - offset * 3,
                 f"₹{sign}{val:,.0f}", ha="center", va="bottom" if h >= 0 else "top",
                 fontsize=9, fontweight="bold")
    orange_patch = mpatches.Patch(color=ORANGE, alpha=0.82, label="Time Stop (actual exit)")
    green_patch  = mpatches.Patch(color=GREEN,  alpha=0.82, label="If held to target / Target Hit")
    red_patch    = mpatches.Patch(color=RED,    alpha=0.82, label="SL Hit")
    ax1.legend(handles=[orange_patch, green_patch, red_patch], fontsize=8, loc="best")

    # ── Chart 2: Per-trade average comparison ─────────────────────────────────
    ax2 = axes[1]
    avg_ts_actual    = ts_actual    / ts_count  if ts_count  else 0
    avg_ts_potential = ts_potential / ts_count  if ts_count  else 0
    avg_tgt          = tgt_actual   / tgt_count if tgt_count else 0

    labels2 = [
        f"Time Stop\nactual\n(n={ts_count})",
        f"Time Stop\npotential\n(n={ts_count})",
        f"Target Hit\nactual\n(n={tgt_count})",
    ]
    values2 = [avg_ts_actual, avg_ts_potential, avg_tgt]
    colors2 = [ORANGE, GREEN, GREEN]
    bars2   = ax2.bar(labels2, values2, color=colors2, alpha=0.82, edgecolor="white", linewidth=1.2)
    ax2.axhline(0, color="#475569", linewidth=0.8, linestyle="--")
    ax2.set_title("Average P&L per Trade", fontweight="bold")
    ax2.set_ylabel("Avg P&L (₹)")
    ax2.grid(axis="y", alpha=0.3, linestyle=":")
    for bar, val in zip(bars2, values2):
        h = bar.get_height()
        offset = max(abs(h) * 0.03, 2)
        sign = "+" if val >= 0 else ""
        ax2.text(bar.get_x() + bar.get_width() / 2, h + offset if h >= 0 else h - offset * 3,
                 f"₹{sign}{val:,.0f}", ha="center", va="bottom" if h >= 0 else "top",
                 fontsize=9, fontweight="bold")

    # ── Chart 3: Opportunity cost waterfall ───────────────────────────────────
    ax3 = axes[2]
    wf_labels = ["Actual\n(time stop)", "Opportunity\nCost", "Potential\n(no stop)"]
    wf_values = [ts_actual, opp_cost, ts_potential]
    wf_bottoms = [0, ts_actual, 0]
    wf_colors  = [ORANGE, RED if opp_cost > 0 else GREY, GREEN]

    for i, (lbl, val, bot, col) in enumerate(zip(wf_labels, wf_values, wf_bottoms, wf_colors)):
        ax3.bar(lbl, val, bottom=bot, color=col, alpha=0.82, edgecolor="white", linewidth=1.2)
        top = bot + val
        sign = "+" if val >= 0 else ""
        ax3.text(i, top + max(abs(ts_potential) * 0.02, 3),
                 f"₹{sign}{val:,.0f}", ha="center", va="bottom",
                 fontsize=9, fontweight="bold")

    ax3.axhline(0, color="#475569", linewidth=0.8, linestyle="--")
    ax3.set_title("Opportunity-Cost Waterfall", fontweight="bold")
    ax3.set_ylabel("P&L (₹)")
    ax3.grid(axis="y", alpha=0.3, linestyle=":")

    if opp_cost > 0 and ts_potential > 0:
        pct = opp_cost / ts_potential * 100
        ax3.set_xlabel(
            f"Time stop cost you {pct:.1f}% of potential profit",
            fontsize=9, color=RED, style="italic"
        )

    plt.tight_layout()

    # Save
    fig.savefig(_PNG_FILE, dpi=180, bbox_inches="tight")
    print(f"\n✅  Chart saved → {_PNG_FILE}")

    if not save_only:
        plt.show()
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    force_ascii = "--ascii" in sys.argv
    save_only   = "--save"  in sys.argv

    trades     = _load_trades()
    target_pct = _detect_target_pct(trades)
    groups     = _build_groups(trades, target_pct)

    ts_count = len(groups["time_stop"]["trades"])
    if ts_count == 0:
        print("\n✅  No TIME_STOP trades found — nothing to visualise.")
        print(f"   Total trades in file: {len(trades)}")
        return

    ts_a = groups["time_stop"]["actual"]
    ts_p = groups["time_stop"]["potential"]
    opp  = ts_p - ts_a

    print()
    print("=" * 72)
    print("  📊 TIME STOP IMPACT — VISUALISER")
    print("=" * 72)
    print(f"\n  Analysing {ts_count} TIME_STOP trade(s)…")
    print(f"  Actual P&L:     ₹{ts_a:+.2f}")
    print(f"  Potential P&L:  ₹{ts_p:+.2f}  (if each hit +{target_pct:.0f}% target)")
    print(f"  Opportunity:    ₹{opp:+.2f}")
    if opp > 0 and ts_p > 0:
        print(f"\n  ⚠️  {opp / ts_p * 100:.1f}% of potential profit was left on the table!")
    print()

    if force_ascii:
        _print_ascii_chart(groups, target_pct)
    else:
        _plot_charts(groups, target_pct, save_only=save_only)
        # Always also print ASCII so terminal shows something useful
        _print_ascii_chart(groups, target_pct)


if __name__ == "__main__":
    main()
