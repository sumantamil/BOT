"""
Performance Optimizer

Aggregates trade history from:
  1. journal_YYYY-MM-DD.json files in the journals/ directory
  2. In-memory TradeRecord objects (call add_trade() after each close)

Produces:
  • Per-strategy stats compatible with PositionSizer.update_stats()
  • Per-hour win-rate heatmap
  • Prioritised actionable recommendations
  • Full formatted report (for console / Telegram)

Usage:
    from bot.performance_optimizer import PerformanceOptimizer

    opt = PerformanceOptimizer()
    opt.load_journals()                     # load historical journal files

    # After each trade closes:
    opt.add_trade(trade_record)

    # Feed Kelly data to the position sizer:
    sizer.update_stats(opt.get_strategy_stats())

    # Print full report:
    print(opt.generate_report())
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, time
from typing import Dict, List, Optional

import numpy as np
from loguru import logger

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────────────
# Internal trade snapshot
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeSnapshot:
    """Normalised representation of one completed trade."""
    trade_id:    str
    strategy:    str        # ORB / VWAP / GAP_FADE / GAP_CONTINUATION / TREND / EOD / LATE_DAY
    direction:   str        # CE / PE
    entry_time:  datetime
    pnl:         float      # realised ₹ P&L
    pnl_pct:     float      # option % P&L (if available)
    win:         bool
    exit_reason: str  = ""
    confidence:  int  = 0   # entry filter score at time of trade


# ─────────────────────────────────────────────────────────────────────────────
# Optimiser
# ─────────────────────────────────────────────────────────────────────────────

class PerformanceOptimizer:
    """
    Aggregates trade history, computes per-strategy stats, and generates
    actionable recommendations.
    """

    _JOURNALS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "journals"
    )
    _MIN_TRADES_FOR_REC = 10     # need this many before making a recommendation
    _MIN_TRADES_FOR_KELLY = 20   # need this many for Kelly to apply

    def __init__(self):
        self._trades:         List[TradeSnapshot] = []
        self._strategy_stats: Dict[str, Dict]     = {}

    # -------------------------------------------------------------------------
    # Data ingestion
    # -------------------------------------------------------------------------

    def load_journals(self, directory: Optional[str] = None) -> int:
        """
        Read all journal_YYYY-MM-DD.json files in the journals directory.
        Returns the number of trade records successfully loaded.
        """
        journals_dir = directory or self._JOURNALS_DIR
        if not os.path.isdir(journals_dir):
            logger.warning(f"[PerfOpt] journals directory not found: {journals_dir}")
            return 0

        loaded = 0
        for fname in sorted(os.listdir(journals_dir)):
            if not (fname.startswith("journal_") and fname.endswith(".json")):
                continue
            fpath = os.path.join(journals_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                entries = raw if isinstance(raw, list) else raw.get("trades", [])
                for entry in entries:
                    snap = self._parse_entry(entry)
                    if snap:
                        self._trades.append(snap)
                        loaded += 1
            except Exception as exc:
                logger.warning(f"[PerfOpt] cannot read {fname}: {exc}")

        logger.info(f"[PerfOpt] Loaded {loaded} trades from {journals_dir}")
        self._rebuild_stats()
        return loaded

    def add_trade(self, trade_record) -> None:
        """
        Accept a TradeRecord-like object and register it.
        Call this immediately after a position closes.
        """
        try:
            pnl_val = getattr(trade_record, "pnl", None)
            if pnl_val is None:
                pnl_val = 0.0
            snap = TradeSnapshot(
                trade_id   = str(getattr(trade_record, "trade_id", "")),
                strategy   = str(getattr(trade_record, "source", "UNKNOWN")).upper(),
                direction  = str(getattr(trade_record, "option_type", "")).upper(),
                entry_time = getattr(trade_record, "timestamp", datetime.now()),
                pnl        = float(pnl_val),
                pnl_pct    = 0.0,
                win        = float(pnl_val) > 0,
            )
            self._trades.append(snap)
            self._rebuild_stats()
        except Exception as exc:
            logger.debug(f"[PerfOpt] add_trade() failed: {exc}")

    # -------------------------------------------------------------------------
    # Stats query
    # -------------------------------------------------------------------------

    def get_strategy_stats(self) -> Dict[str, Dict]:
        """
        Return per-strategy stats in the format expected by PositionSizer.

        Dict format:
            {
              "ORB": {
                  "total_trades": 30,
                  "win_rate":     62.0,     # %
                  "avg_win":      18.5,     # ₹ average winning trade
                  "avg_loss":    -9.2,      # ₹ average losing trade (negative)
                  "profit_factor": 1.8,
                  "total_pnl":   850.0,
              },
              ...
            }
        """
        return dict(self._strategy_stats)

    def total_trades(self) -> int:
        return len(self._trades)

    # -------------------------------------------------------------------------
    # Analysis
    # -------------------------------------------------------------------------

    def analyze_by_strategy(self) -> Dict[str, Dict]:
        """Per-strategy breakdown with recommendation label."""
        result: Dict[str, Dict] = {}
        for strategy, stats in self._strategy_stats.items():
            wr  = stats["win_rate"]
            pf  = stats["profit_factor"]
            tp  = stats["total_pnl"]
            n   = stats["total_trades"]

            if wr >= 60 and pf >= 1.5:
                rec = "✅ INCREASE — high performing"
            elif wr >= 50 and pf >= 1.2:
                rec = "✅ CONTINUE — performing well"
            elif wr >= 45:
                rec = "⚠️ MONITOR  — underperforming, review filters"
            else:
                rec = "❌ REDUCE/DISABLE — losing money"

            result[strategy] = {**stats, "recommendation": rec}
        return result

    def analyze_by_hour(self) -> Dict[int, Dict]:
        """Win-rate heatmap bucketed by entry hour."""
        by_hour: Dict[int, List[TradeSnapshot]] = defaultdict(list)
        for t in self._trades:
            by_hour[t.entry_time.hour].append(t)

        result: Dict[int, Dict] = {}
        for hour, trades in sorted(by_hour.items()):
            wins = [t for t in trades if t.win]
            wr   = len(wins) / len(trades) * 100 if trades else 0
            avg_p = sum(t.pnl for t in trades) / len(trades) if trades else 0
            result[hour] = {
                "total":    len(trades),
                "win_rate": round(wr, 1),
                "avg_pnl":  round(avg_p, 2),
                "verdict":  (
                    "✅ GOOD"   if wr >= 55 else
                    "⚠️ OK"     if wr >= 45 else
                    "❌ POOR"
                ),
            }
        return result

    def analyze_by_exit_reason(self) -> Dict[str, Dict]:
        """Break down win rate by exit type (SL / T1 / T2 / TIME / etc.)."""
        by_exit: Dict[str, List[TradeSnapshot]] = defaultdict(list)
        for t in self._trades:
            by_exit[t.exit_reason or "UNKNOWN"].append(t)

        result: Dict[str, Dict] = {}
        for reason, trades in sorted(by_exit.items()):
            wins = [t for t in trades if t.win]
            wr   = len(wins) / len(trades) * 100 if trades else 0
            result[reason] = {
                "total":    len(trades),
                "win_rate": round(wr, 1),
                "avg_pnl":  round(sum(t.pnl for t in trades) / len(trades), 2) if trades else 0,
            }
        return result

    def analyze_sharpe(self) -> Dict[str, float]:
        """Annualised Sharpe ratio per strategy (requires pnl_pct data)."""
        result: Dict[str, float] = {}
        by_strat: Dict[str, List[float]] = defaultdict(list)
        for t in self._trades:
            if t.pnl_pct != 0:
                by_strat[t.strategy].append(t.pnl_pct)
        for strat, pnls in by_strat.items():
            if len(pnls) < 5:
                continue
            arr = np.array(pnls)
            mu  = arr.mean()
            sig = arr.std()
            sharpe = (mu / sig) * math.sqrt(250) if sig > 0 else 0.0
            result[strat] = round(sharpe, 3)
        return result

    # -------------------------------------------------------------------------
    # Recommendations
    # -------------------------------------------------------------------------

    def generate_recommendations(self) -> List[Dict]:
        """
        Produce prioritised, actionable recommendations.

        Returns list of dicts:
            {priority, action, item, reason, impact}
        Sorted HIGH first, then MEDIUM.
        """
        recs: List[Dict] = []
        strat_analysis = self.analyze_by_strategy()

        for strategy, stats in strat_analysis.items():
            n   = stats["total_trades"]
            wr  = stats["win_rate"]
            pf  = stats["profit_factor"]
            tp  = stats["total_pnl"]

            if n < self._MIN_TRADES_FOR_REC:
                continue

            if wr < 45:
                recs.append({
                    "priority": "HIGH",
                    "action":   "DISABLE",
                    "item":     f"{strategy} strategy",
                    "reason":   f"{wr:.0f}% win rate over {n} trades, "
                                f"total P&L = ₹{tp:+,.0f}",
                    "impact":   f"Stop ≈ ₹{abs(tp)/n:,.0f}/trade average loss",
                })
            elif wr >= 65 and pf >= 1.8:
                recs.append({
                    "priority": "HIGH",
                    "action":   "INCREASE",
                    "item":     f"{strategy} position size",
                    "reason":   f"{wr:.0f}% win rate, profit factor {pf:.1f}×",
                    "impact":   f"Could increase returns by {(pf - 1) * 20:.0f}%+ with larger size",
                })

        # Time-of-day recommendations
        for hour, h_stats in self.analyze_by_hour().items():
            if h_stats["total"] >= 5 and h_stats["win_rate"] < 40:
                recs.append({
                    "priority": "MEDIUM",
                    "action":   "AVOID",
                    "item":     f"Trades at {hour:02d}:00–{hour+1:02d}:00 IST",
                    "reason":   f"Only {h_stats['win_rate']:.0f}% win rate during this hour",
                    "impact":   "Improve overall win rate ~3–8%",
                })

        # Exit-reason diagnostics
        for reason, e_stats in self.analyze_by_exit_reason().items():
            if reason == "STOP_LOSS" and e_stats["total"] >= 5:
                sl_wr = e_stats["win_rate"]
                if sl_wr > 20:  # hitting too many SLs that then reverse
                    recs.append({
                        "priority": "MEDIUM",
                        "action":   "WIDEN_SL",
                        "item":     "Stop-loss distance",
                        "reason":   f"{sl_wr:.0f}% of SL exits are profitable within the same day",
                        "impact":   "Wider ATR-based SL may improve win rate",
                    })

        return sorted(recs, key=lambda r: 0 if r["priority"] == "HIGH" else 1)

    # -------------------------------------------------------------------------
    # Report generation
    # -------------------------------------------------------------------------

    def generate_report(self) -> str:
        """Return a multi-line formatted performance report."""
        if not self._trades:
            return (
                "No trade history yet.\n"
                "Run for a few live sessions or load journal files first.\n"
                "Then call: perf  or  performance  in the bot console."
            )

        total    = len(self._trades)
        wins     = sum(1 for t in self._trades if t.win)
        total_pnl = sum(t.pnl for t in self._trades)
        overall_wr = wins / total * 100 if total > 0 else 0

        lines = [
            "╔═══════════════════════════════════════════════════╗",
            "║         PERFORMANCE OPTIMISER REPORT             ║",
            "╚═══════════════════════════════════════════════════╝",
            "",
            f"  Total trades       : {total}",
            f"  Overall win rate   : {overall_wr:.1f}%",
            f"  Total realised P&L : ₹{total_pnl:+,.2f}",
            f"  Date range         : {self._date_range()}",
            "",
            "─── By Strategy ─────────────────────────────────────",
        ]

        for strategy, stats in self.analyze_by_strategy().items():
            lines.append(
                f"  {strategy:<20}  WR={stats['win_rate']:.0f}%  "
                f"PF={stats['profit_factor']:.2f}  "
                f"n={stats['total_trades']:<5}  "
                f"₹{stats['total_pnl']:+,.0f}"
            )
            lines.append(f"    → {stats['recommendation']}")

        sharpe = self.analyze_sharpe()
        if sharpe:
            lines += ["", "─── Sharpe Ratio (annualised) ──────────────────────"]
            for strat, s in sharpe.items():
                label = "✅ Good" if s > 1.0 else ("⚠️ OK" if s > 0.5 else "❌ Poor")
                lines.append(f"  {strat:<20}  Sharpe={s:.2f}  {label}")

        hour_data = self.analyze_by_hour()
        if hour_data:
            lines += ["", "─── By Hour of Entry (IST) ──────────────────────────"]
            for hour, h in hour_data.items():
                if h["total"] >= 3:
                    lines.append(
                        f"  {hour:02d}:00  WR={h['win_rate']:.0f}%  "
                        f"avg=₹{h['avg_pnl']:+.0f}  n={h['total']}  {h['verdict']}"
                    )

        recs = self.generate_recommendations()
        if recs:
            lines += ["", "─── Recommendations ─────────────────────────────────"]
            for r in recs[:8]:    # top 8
                lines.append(f"  [{r['priority']}] {r['action']} {r['item']}")
                lines.append(f"       Reason : {r['reason']}")
                lines.append(f"       Impact : {r['impact']}")

        lines.append("\n═══════════════════════════════════════════════════════")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _parse_entry(self, entry: Dict) -> Optional[TradeSnapshot]:
        """Parse one raw journal dict → TradeSnapshot, or None on error."""
        try:
            strategy = (
                entry.get("source")
                or entry.get("strategy")
                or "UNKNOWN"
            )
            strategy = str(strategy).upper()

            # Normalise strategy aliases
            if strategy in ("GAP", "AUTO"):
                strategy = "GAP_CONTINUATION"

            pnl = float(
                entry.get("pnl")
                or entry.get("realized_pnl")
                or entry.get("realised_pnl")
                or 0
            )
            ts_raw = (
                entry.get("timestamp")
                or entry.get("entry_time")
                or ""
            )
            try:
                entry_time = datetime.fromisoformat(str(ts_raw))
            except Exception:
                entry_time = datetime.now()

            return TradeSnapshot(
                trade_id    = str(entry.get("trade_id", "")),
                strategy    = strategy,
                direction   = str(entry.get("option_type", "") or "").upper(),
                entry_time  = entry_time,
                pnl         = pnl,
                pnl_pct     = float(entry.get("pnl_pct", 0) or 0),
                win         = pnl > 0,
                exit_reason = str(entry.get("exit_reason", "") or ""),
                confidence  = int(entry.get("confidence", 0) or 0),
            )
        except Exception as exc:
            logger.debug(f"[PerfOpt] _parse_entry failed: {exc}")
            return None

    def _rebuild_stats(self) -> None:
        """Recompute per-strategy aggregates from self._trades."""
        by_strat: Dict[str, List[TradeSnapshot]] = defaultdict(list)
        for t in self._trades:
            by_strat[t.strategy].append(t)

        self._strategy_stats = {}
        for strategy, trades in by_strat.items():
            wins   = [t for t in trades if t.win]
            losses = [t for t in trades if not t.win]
            n      = len(trades)
            wr     = len(wins) / n * 100 if n > 0 else 0

            avg_w  = sum(t.pnl for t in wins)   / len(wins)   if wins   else 0.0
            avg_l  = sum(t.pnl for t in losses) / len(losses) if losses else 0.0

            gross_w = sum(t.pnl for t in wins)                        if wins   else 0.0
            gross_l = max(abs(sum(t.pnl for t in losses)), 1e-9)   if losses else 1e-9
            pf      = gross_w / gross_l

            self._strategy_stats[strategy] = {
                "total_trades":   n,
                "win_rate":       round(wr, 1),
                "avg_win":        round(avg_w,  2),
                "avg_loss":       round(avg_l,  2),
                "profit_factor":  round(pf,     3),
                "total_pnl":      round(sum(t.pnl for t in trades), 2),
            }

    def _date_range(self) -> str:
        if not self._trades:
            return "N/A"
        dates = [t.entry_time.date() for t in self._trades]
        return f"{min(dates)} → {max(dates)}"


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton (lazily initialised)
# ─────────────────────────────────────────────────────────────────────────────

_perf_optimizer: Optional[PerformanceOptimizer] = None


def get_performance_optimizer() -> PerformanceOptimizer:
    """Return the module-level PerformanceOptimizer, loading journals on first call."""
    global _perf_optimizer
    if _perf_optimizer is None:
        _perf_optimizer = PerformanceOptimizer()
        _perf_optimizer.load_journals()
    return _perf_optimizer
