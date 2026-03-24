"""
Paper Trading Tracker
=====================
Automatically tracks paper trade entries and exits — no real orders placed.

How it works:
  - When a 📋 PAPER signal fires, paper_buy() records the entry with:
      • index price at signal time
      • estimated ATM option premium (≈0.8% of index price)
      • option type (CE / PE) and strategy name
  - Every analysis cycle, check_exits() checks whether SL / Target / Time‑stop
    was reached by comparing the CURRENT index price against the entry.
  - P&L is calculated using a ~2.5× leverage factor (ATM option delta + gamma).
  - Each closed trade is appended to paper_trades.json so results survive restarts.

Win/Loss definition (for go‑live decision):
  - WIN  → option P&L estimate ≥ +1% (profitable, even if small)
  - LOSS → option P&L estimate ≤ −1%
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime
from typing import Awaitable, Callable, Dict, List, Optional

from loguru import logger

# Type alias for the async Telegram alert callback injected by engine.py
_AlertCallback = Optional[Callable[[str, str], Awaitable[None]]]


# ---------------------------------------------------------------------------
# Default thresholds (overridden by configure() from .env settings)
# ---------------------------------------------------------------------------
_DEFAULT_SL_PCT      = 20.0   # matches TRADING_STOP_LOSS_PERCENTAGE
_DEFAULT_TARGET_PCT  = 50.0   # matches TRADING_TARGET_PERCENTAGE
# ATM option leverage: delta(≈0.5) / premium_ratio(0.8%) = 62.5.
# We use 50 as a slightly conservative estimate for weekly options to account
# for time-decay friction.  A 1% underlying move → ~50% option P&L change,
# a 2% move → ~100%.  SL at 20% ≈ 0.4% adverse index move (≈90 NIFTY pts).
_OPTION_LEVERAGE     = 50.0
_PREMIUM_RATIO       = 0.008  # ATM premium ≈ 0.8% of index (NIFTY 22500 → ~180 pts)
_SAVE_PATH           = "paper_trades.json"
_CSV_PATH            = "paper_trades_export.csv"

# Hard time‑stop defaults (overridden per‑instance via configure())
_HARD_EXIT_HOUR   = 13   # positions entered before this hour are closed when we reach it
_MARKET_CLOSE_H   = 15
_MARKET_CLOSE_M   = 27


class PaperTrader:
    """Tracks paper trade entries, monitors exits, and calculates P&L."""

    def __init__(self) -> None:
        self._positions: List[dict] = []   # currently open paper positions
        self._trades:    List[dict] = []   # all closed trades (in-memory)
        self._total_pnl: float      = 0.0

        # Risk config — will be overridden by configure()
        self._sl_pct:          float = _DEFAULT_SL_PCT
        self._target_pct:      float = _DEFAULT_TARGET_PCT

        # Smart time-exit config (overridden by configure())
        self._hard_exit_hour:    int   = _HARD_EXIT_HOUR
        self._exit_only_losers:  bool  = False   # if True, profitable trades skip soft exit
        self._exit_min_profit:   float = 50.0    # ₹ threshold to classify a trade as "winning"

        # Optional async callback injected by engine: async def alert(msg, type)
        self._alert_cb: _AlertCallback = None

        # Load any trades from a previous session
        self._load_from_file()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure(
        self,
        sl_pct: float,
        target_pct: float,
        alert_cb: _AlertCallback = None,
        hard_time_exit_hour: int = _HARD_EXIT_HOUR,
        time_exit_only_losers: bool = False,
        time_exit_min_profit: float = 50.0,
    ) -> None:
        """Sync SL / target / time-exit settings from .env at startup.
        Optionally inject the engine's _send_telegram_alert coroutine."""
        self._sl_pct            = sl_pct
        self._target_pct        = target_pct
        self._alert_cb          = alert_cb
        self._hard_exit_hour    = hard_time_exit_hour
        self._exit_only_losers  = time_exit_only_losers
        self._exit_min_profit   = time_exit_min_profit
        logger.debug(
            f"PaperTrader configured: SL={sl_pct}%  Target={target_pct}%  "
            f"HardExit={hard_time_exit_hour}:00  "
            f"OnlyLosers={time_exit_only_losers}  MinProfit=₹{time_exit_min_profit:.0f}"
        )

    def paper_buy(
        self,
        index_name:        str,
        index_price:       float,
        option_type:       str,   # "CE" or "PE"
        strategy:          str,   # "ORB" / "VWAP" / "TREND" / "EOD" / "GAP"
        estimated_premium: float = 0.0,
    ) -> Optional[dict]:
        """
        Record a paper entry.

        Returns the position dict (or None if a duplicate is detected for the same
        index + strategy + option_type on the same day, to avoid double-counting).
        """
        today = datetime.now().date()

        # Deduplicate: one entry per (index, strategy, option_type) per day
        for p in self._positions:
            if (
                p["index_name"] == index_name
                and p["strategy"] == strategy
                and p["option_type"] == option_type
                and datetime.fromisoformat(p["entry_time"]).date() == today
            ):
                logger.info(
                    f"PaperTrader: {index_name} {strategy} {option_type} "
                    "already open today — skipping duplicate entry"
                )
                return None

        if estimated_premium <= 0:
            estimated_premium = round(index_price * _PREMIUM_RATIO)

        trade = {
            "id":            len(self._trades) + len(self._positions) + 1,
            "index_name":    index_name,
            "option_type":   option_type,
            "strategy":      strategy,
            "index_entry":   index_price,
            "premium_entry": estimated_premium,
            "entry_time":    datetime.now().isoformat(),
            "status":        "OPEN",
            "pnl":           0.0,
            "pnl_pct":       0.0,
            "exit_reason":   None,
        }
        self._positions.append(trade)
        logger.info(
            f"📋 PAPER ENTRY #{trade['id']}: {index_name} {option_type} "
            f"[{strategy}] | index={index_price:,.0f} | est.premium=₹{estimated_premium:.0f}"
        )
        if self._alert_cb:
            import asyncio
            msg = (
                f"📋 *PAPER ENTRY* #{trade['id']}\n"
                f"Index: {index_name} {option_type}\n"
                f"Strategy: {strategy}\n"
                f"Index @ {index_price:,.0f} | Est premium \u20b9{estimated_premium:.0f}\n"
                f"SL: -{self._sl_pct}% | Target: +{self._target_pct}%"
            )
            asyncio.create_task(self._alert_cb(msg, "paper_entry"))
        return trade

    def check_exits(self, index_prices: Dict[str, float]) -> None:
        """
        Called every analysis cycle with a mapping of {index_name: current_price}.
        Closes positions that have hit SL, target, or a time‑stop.
        """
        if not self._positions:
            return

        now = datetime.now().time()
        now_str = datetime.now().strftime("%H:%M:%S")
        market_close = dtime(_MARKET_CLOSE_H, _MARKET_CLOSE_M)
        hard_exit    = dtime(self._hard_exit_hour, 0)

        for pos in list(self._positions):
            idx_name  = pos["index_name"]
            idx_price = index_prices.get(idx_name)
            if idx_price is None or idx_price <= 0:
                logger.warning(
                    f"📋 PAPER HOLD [{now_str}] #{pos['id']}: {idx_name} {pos['option_type']} "
                    f"[{pos['strategy']}] — no index price in cache, skipping exit check "
                    f"(trend analyzer returned no signal this cycle?)"
                )
                continue

            # Estimated option gain/loss based on index move
            if pos["option_type"] == "CE":
                index_move_pct = (idx_price - pos["index_entry"]) / pos["index_entry"] * 100
            else:  # PE profits when index falls
                index_move_pct = (pos["index_entry"] - idx_price) / pos["index_entry"] * 100

            option_change_pct = index_move_pct * _OPTION_LEVERAGE
            # Cap: can't lose more than 100%, can gain up to 300%
            option_change_pct = max(-100.0, min(option_change_pct, 300.0))

            # Store live unrealised P&L in the position dict so /api/pnl and
            # /api/trades can serve current values without waiting for a close event.
            pos["pnl"]     = round(pos["premium_entry"] * option_change_pct / 100, 2)
            pos["pnl_pct"] = round(option_change_pct, 2)

            # Verbose live P&L trace — always shown while position is open
            logger.info(
                f"📋 PAPER LIVE [{now_str}] #{pos['id']}: "
                f"{idx_name} {pos['option_type']} [{pos['strategy']}] "
                f"| entry={pos['index_entry']:,.0f} now={idx_price:,.0f} "
                f"| move={index_move_pct:+.2f}% → option≈{option_change_pct:+.1f}% "
                f"| SL≤-{self._sl_pct:.0f}% Target≥+{self._target_pct:.0f}%"
            )

            # Determine exit reason
            reason: Optional[str] = None
            if now >= market_close:
                reason = "MARKET_CLOSE"
            elif option_change_pct <= -self._sl_pct:
                reason = "SL_HIT"
            elif option_change_pct >= self._target_pct:
                reason = "TARGET_HIT"
            elif now >= hard_exit and pos["strategy"] not in ("EOD",):
                entry_t = datetime.fromisoformat(pos["entry_time"]).time()
                if entry_t < hard_exit:
                    # Smart time exit: let profitable trades run toward target
                    if self._exit_only_losers and pos["pnl"] >= self._exit_min_profit:
                        logger.info(
                            f"📋 PAPER HOLD [{now_str}] #{pos['id']}: "
                            f"{pos['index_name']} {pos['option_type']} [{pos['strategy']}] "
                            f"— profitable (₹{pos['pnl']:+.0f} ≥ ₹{self._exit_min_profit:.0f}), "
                            f"skipping {self._hard_exit_hour:02d}:00 time stop — letting winner run 🏃"
                        )
                    else:
                        reason = f"TIME_STOP_{self._hard_exit_hour:02d}:00"

            if reason:
                self._close_position(pos, idx_price, option_change_pct, reason)

    def has_open_positions(self) -> bool:
        return len(self._positions) > 0

    def get_summary(self) -> dict:
        all_trades = self._trades
        wins   = [t for t in all_trades if t["pnl"] > 0]
        losses = [t for t in all_trades if t["pnl"] <= 0]
        total  = len(all_trades)
        unrealized_pnl = round(sum(p.get("pnl", 0.0) for p in self._positions), 2)
        return {
            "total_trades":   total,
            "open_positions": len(self._positions),
            "total_pnl":      round(self._total_pnl + unrealized_pnl, 2),
            "realized_pnl":   round(self._total_pnl, 2),
            "unrealized_pnl": unrealized_pnl,
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       f"{len(wins) / total * 100:.1f}%" if total else "0%",
            "best_trade":     max(all_trades, key=lambda x: x["pnl"]) if all_trades else None,
            "worst_trade":    min(all_trades, key=lambda x: x["pnl"]) if all_trades else None,
            "open_list":      list(self._positions),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _close_position(
        self,
        pos:       dict,
        idx_price: float,
        pnl_pct:   float,
        reason:    str,
    ) -> float:
        pnl = pos["premium_entry"] * pnl_pct / 100

        pos["index_exit"]  = idx_price
        pos["exit_time"]   = datetime.now().isoformat()
        pos["exit_reason"] = reason
        pos["pnl"]         = round(pnl, 2)
        pos["pnl_pct"]     = round(pnl_pct, 2)
        pos["status"]      = "CLOSED"

        self._total_pnl += pnl
        self._trades.append(dict(pos))
        self._positions.remove(pos)

        result = "WIN ✅" if pnl > 0 else "LOSS ❌"
        logger.info(
            f"📋 PAPER EXIT [{reason}] #{pos['id']}: "
            f"{pos['index_name']} {pos['option_type']} [{pos['strategy']}] "
            f"| index {pos['index_entry']:,.0f} → {idx_price:,.0f} "
            f"| est. P&L: ₹{pnl:+.0f} ({pnl_pct:+.1f}%) {result}"
            f"  [Running total: ₹{self._total_pnl:+.0f}]"
        )
        if self._alert_cb:
            import asyncio
            msg = (
                f"📋 *PAPER EXIT* [{reason}] #{pos['id']}\n"
                f"{pos['index_name']} {pos['option_type']} [{pos['strategy']}]\n"
                f"Index {pos['index_entry']:,.0f} \u2192 {idx_price:,.0f}\n"
                f"Est P&L: \u20b9{pnl:+.0f} ({pnl_pct:+.1f}%) {result}\n"
                f"Running total: \u20b9{self._total_pnl:+.0f}"
            )
            asyncio.create_task(self._alert_cb(msg, "paper_exit"))
        self._save_to_file()
        return pnl

    def _save_to_file(self) -> None:
        try:
            data = {
                "summary":        self.get_summary(),
                "trades":         self._trades,
                "open_positions": self._positions,
            }
            with open(_SAVE_PATH, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"PaperTrader: could not save to {_SAVE_PATH}: {e}")
        # Keep CSV in sync after every close
        self.export_to_csv()

    def export_to_csv(self, csv_path: str = _CSV_PATH) -> None:
        """Export all closed trades to CSV for external analysis / spreadsheet review."""
        import csv as _csv
        if not self._trades:
            return
        fieldnames = [
            "id", "date", "entry_time", "exit_time", "duration_mins",
            "strategy", "index_name", "option_type",
            "index_entry", "premium_entry", "index_exit",
            "pnl", "pnl_pct", "exit_reason",
        ]
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for t in self._trades:
                    try:
                        entry_dt = datetime.fromisoformat(t.get("entry_time", ""))
                    except Exception:
                        entry_dt = datetime.now()
                    exit_str = t.get("exit_time", "")
                    try:
                        exit_dt: Optional[datetime] = datetime.fromisoformat(exit_str) if exit_str else None
                    except Exception:
                        exit_dt = None
                    duration = round(
                        (exit_dt - entry_dt).total_seconds() / 60, 1
                    ) if exit_dt else 0.0
                    w.writerow({
                        "id":            t.get("id"),
                        "date":          entry_dt.strftime("%Y-%m-%d"),
                        "entry_time":    entry_dt.strftime("%H:%M:%S"),
                        "exit_time":     exit_dt.strftime("%H:%M:%S") if exit_dt else "",
                        "duration_mins": duration,
                        "strategy":      t.get("strategy", ""),
                        "index_name":    t.get("index_name", ""),
                        "option_type":   t.get("option_type", ""),
                        "index_entry":   t.get("index_entry", 0),
                        "premium_entry": t.get("premium_entry", 0),
                        "index_exit":    t.get("index_exit", 0),
                        "pnl":           t.get("pnl", 0),
                        "pnl_pct":       t.get("pnl_pct", 0),
                        "exit_reason":   t.get("exit_reason", ""),
                    })
            logger.debug(f"PaperTrader: CSV updated → {csv_path} ({len(self._trades)} trades)")
        except Exception as e:
            logger.warning(f"PaperTrader: could not export CSV: {e}")

    def _load_from_file(self) -> None:
        """Reload closed trades from a previous session so totals are cumulative."""
        if not os.path.exists(_SAVE_PATH):
            return
        try:
            with open(_SAVE_PATH) as f:
                data = json.load(f)
            self._trades    = data.get("trades", [])
            self._total_pnl = sum(t.get("pnl", 0) for t in self._trades)
            logger.info(
                f"PaperTrader: loaded {len(self._trades)} past trades "
                f"| running total ₹{self._total_pnl:+.0f}"
            )
        except Exception as e:
            logger.warning(f"PaperTrader: could not load {_SAVE_PATH}: {e}")
