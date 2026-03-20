"""
Position Sizer — Kelly Criterion + Confidence-Adjusted Sizing

Calculates the optimal trade quantity based on:
  1. Account balance and max risk per trade (default 2%)
  2. Stop-loss distance (risk per unit = entry - stop)
  3. Signal confidence score (0–100) → confidence_factor
  4. Historical strategy win-rate + avg win/loss → Kelly Criterion factor

Formula:
  max_risk_₹  =  account × max_risk_pct / 100
  base_qty    =  max_risk_₹ / risk_per_unit
  final_qty   =  base_qty × confidence_factor × kelly_factor
                 (rounded to lot boundary, clamped so implied risk ≤ 1.5× max)

Safety invariants:
  • Never risks more than 1.5× max_risk_₹ on any single trade
  • kelly_factor always in [0.5, 2.0]
  • confidence_factor always in [0.5, 1.3]
  • Requires ≥ 20 historical trades before Kelly adjustments apply

Usage:
    from bot.position_sizer import PositionSizer

    sizer = PositionSizer(account_balance=100_000)
    qty = sizer.calculate_position_size(
        signal={"strategy": "ORB", "direction": "CE", "confidence": 82},
        entry_price=24_000,
        stop_loss_price=23_700,
        lot_size=75,
    )
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from loguru import logger

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings


class PositionSizer:
    """
    Dynamic position sizing with Kelly Criterion and confidence scaling.

    strategy_stats dict format (comes from PerformanceOptimizer.get_strategy_stats()):
        {
            "ORB":  {"total_trades": 30, "win_rate": 62.0,
                     "avg_win": 18.5, "avg_loss": -9.2},
            "VWAP": {...},
        }

    These are loaded automatically from PerformanceOptimizer if not supplied.
    Call update_stats() after each session to keep Kelly factors current.
    """

    # Confidence score → position-size multiplier
    _CONFIDENCE_TABLE = [
        (90, 1.3),
        (80, 1.1),
        (70, 1.0),
        (60, 0.8),
        (0,  0.5),
    ]

    def __init__(
        self,
        account_balance:        float,
        max_risk_per_trade_pct: float          = None,
        kelly_fraction:         float          = None,
        strategy_stats:         Optional[Dict] = None,
    ):
        cfg = settings.position_sizer
        self.account_balance        = account_balance
        self.max_risk_per_trade_pct = (
            max_risk_per_trade_pct
            if max_risk_per_trade_pct is not None
            else cfg.max_risk_per_trade_pct
        )
        self.kelly_fraction = (
            kelly_fraction
            if kelly_fraction is not None
            else cfg.kelly_fraction
        )
        self._strategy_stats: Dict[str, Dict] = strategy_stats or {}

    # -------------------------------------------------------------------------
    # Stats update
    # -------------------------------------------------------------------------

    def update_stats(self, strategy_stats: Dict) -> None:
        """Refresh per-strategy stats (call after new trades close)."""
        self._strategy_stats = strategy_stats

    def update_account_balance(self, balance: float) -> None:
        """Update account balance (call after capital changes)."""
        self.account_balance = balance

    # -------------------------------------------------------------------------
    # Main interface
    # -------------------------------------------------------------------------

    def calculate_position_size(
        self,
        signal:          Dict,
        entry_price:     float,
        stop_loss_price: float,
        lot_size:        int = 1,
    ) -> int:
        """
        Compute final order quantity.

        Args:
            signal:          dict – strategy (str), direction (CE/PE), confidence (int 0–100)
            entry_price:     entry level as absolute price (e.g. 24000)
            stop_loss_price: absolute stop-loss price (e.g. 23700)
            lot_size:        minimum lot/contract size (e.g. 75 for NIFTY)

        Returns:
            Integer quantity (always a multiple of lot_size if lot_size > 1,
            minimum 1 lot).
        """
        strategy   = signal.get("strategy", "TREND").upper()
        confidence = max(0, min(100, int(signal.get("confidence", 70))))

        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit < 0.01:
            logger.warning(
                "[PositionSizer] Risk per unit near-zero — "
                "using 0.5% of entry as fallback"
            )
            risk_per_unit = entry_price * 0.005

        max_risk_amount = self.account_balance * (self.max_risk_per_trade_pct / 100)

        # Base quantity from pure risk arithmetic
        base_qty = max_risk_amount / risk_per_unit

        # Adjustments
        conf_factor  = self._confidence_factor(confidence)
        kelly_factor = self._kelly_factor(strategy)

        adjusted_qty = base_qty * conf_factor * kelly_factor

        # Round to nearest lot boundary
        if lot_size > 1:
            lots      = max(1, round(adjusted_qty / lot_size))
            final_qty = lots * lot_size
        else:
            final_qty = max(1, round(adjusted_qty))

        # Hard cap: implied risk must not exceed 1.5× max_risk_amount
        implied_risk = risk_per_unit * final_qty
        if implied_risk > max_risk_amount * 1.5:
            capped = max_risk_amount * 1.5 / risk_per_unit
            if lot_size > 1:
                capped = max(lot_size, (int(capped) // lot_size) * lot_size)
            else:
                capped = max(1, int(capped))
            final_qty = min(final_qty, int(capped))

        logger.info(
            f"[PositionSizer] {strategy} {signal.get('direction', '?')}  "
            f"confidence={confidence}  "
            f"account=₹{self.account_balance:,.0f}  max_risk=₹{max_risk_amount:,.0f}  "
            f"rpu={risk_per_unit:.1f}  base={base_qty:.1f}  "
            f"conf_f={conf_factor:.2f}  kelly_f={kelly_factor:.2f}  "
            f"final={final_qty}  implied_risk=₹{risk_per_unit * final_qty:,.0f}"
        )
        return int(final_qty)

    # -------------------------------------------------------------------------
    # Confidence factor
    # -------------------------------------------------------------------------

    def _confidence_factor(self, confidence: int) -> float:
        """
        Map signal confidence (0–100) to a position-size multiplier.

        90–100  → 1.3×  (very high conviction, size up)
        80–89   → 1.1×
        70–79   → 1.0×  (baseline)
        60–69   → 0.8×
        < 60    → 0.5×  (low confidence, half size or skip)
        """
        for threshold, factor in self._CONFIDENCE_TABLE:
            if confidence >= threshold:
                return factor
        return 0.5

    # -------------------------------------------------------------------------
    # Kelly factor
    # -------------------------------------------------------------------------

    def _kelly_factor(self, strategy: str) -> float:
        """
        Compute fractional Kelly multiplier from historical strategy stats.

        Full Kelly:  f = (b·p − q) / b
            b = avg_win / |avg_loss|
            p = win_rate (decimal)
            q = 1 − p

        We use fractional Kelly (default 25%) for safety.
        The result is normalised so that f=0.10 → factor=1.0.
        Clamped to [0.5, 2.0].

        Falls back to 1.0 if fewer than 20 trades or stats missing.
        """
        stats = self._strategy_stats.get(strategy)
        if not stats or stats.get("total_trades", 0) < 20:
            logger.debug(
                f"[PositionSizer] {strategy}: < 20 trades or no stats — kelly_factor=1.0"
            )
            return 1.0

        win_rate = stats["win_rate"] / 100.0
        avg_win  = abs(float(stats.get("avg_win",  0) or 0))
        avg_loss = abs(float(stats.get("avg_loss", 0) or 0))

        if avg_win <= 0 or avg_loss <= 0 or win_rate <= 0:
            return 1.0

        b = avg_win / avg_loss
        p = win_rate
        q = 1.0 - p

        full_kelly = (b * p - q) / b
        frac_kelly = full_kelly * self.kelly_fraction   # typically 0.25×

        # Relative modifier: 0.10 full Kelly → 1.0× sizing
        factor = frac_kelly / 0.10
        factor = max(0.5, min(factor, 2.0))

        logger.debug(
            f"[PositionSizer] Kelly[{strategy}]: wr={p:.2f}  b={b:.2f}  "
            f"full_kelly={full_kelly:.3f}  frac={frac_kelly:.3f}  factor={factor:.2f}"
        )
        return round(factor, 3)

    # -------------------------------------------------------------------------
    # Convenience
    # -------------------------------------------------------------------------

    def explain(
        self,
        signal:          Dict,
        entry_price:     float,
        stop_loss_price: float,
        lot_size:        int = 1,
    ) -> str:
        """Return a formatted human-readable sizing explanation string."""
        strategy    = signal.get("strategy", "?").upper()
        direction   = signal.get("direction", "?")
        confidence  = int(signal.get("confidence", 70))
        rpu         = abs(entry_price - stop_loss_price)
        max_risk    = self.account_balance * self.max_risk_per_trade_pct / 100
        final_qty   = self.calculate_position_size(signal, entry_price, stop_loss_price, lot_size)
        conf_factor = self._confidence_factor(confidence)
        kelly_f     = self._kelly_factor(strategy)

        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  POSITION SIZER — {strategy} {direction}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  Account balance : ₹{self.account_balance:>12,.0f}\n"
            f"  Max risk (%)    : {self.max_risk_per_trade_pct:>10.1f}%\n"
            f"  Max risk (₹)    : ₹{max_risk:>11,.0f}\n"
            f"  Entry price     : ₹{entry_price:>11,.2f}\n"
            f"  Stop price      : ₹{stop_loss_price:>11,.2f}\n"
            f"  Risk per unit   : ₹{rpu:>11,.2f}\n"
            f"  Base qty        : {max_risk/rpu:>12.1f}\n"
            f"  Confidence ({confidence:3d}%): {conf_factor:>11.2f}×\n"
            f"  Kelly factor    : {kelly_f:>11.2f}×\n"
            f"  ──────────────────────────\n"
            f"  Final qty       : {final_qty:>12}\n"
            f"  Implied risk    : ₹{rpu * final_qty:>11,.0f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
