"""
Exit Manager — Complete Sophisticated Exit System

Components:
  DynamicStopLoss   — strategy-aware stop placement (range-based / ATR-based)
  MultiTargetExit   — staged partial exits: 40 % / 30 % / 20 % + 10 % runner
  TrailingStop      — profit-protecting trail activating after +20 % option gain
  TimeBasedExit     — strategy-specific and profit-aware time rules
  VolatilityExit    — VIX spike / ATR expansion exits
  ExitManager       — master coordinator (one instance per open position)

Priority order (highest = checked first):
  1. Stop-loss         — capital protection
  2. Multi-target      — take profits in stages
  3. Trailing stop     — protect open profits
  4. Time-based exit   — max hold time / end-of-day rules
  5. Volatility exit   — market conditions changed

Usage:
    mgr = ExitManager(
        strategy="ORB", entry_price=24000, stop_loss=23700,
        position_size=65, direction="CE",
    )
    # In each monitoring tick:
    instr = mgr.evaluate_exit(position_dict, market_data_dict)
    if instr:
        # e.g. {"exit_type": "PARTIAL_1", "exit_qty": 26, "remaining_qty": 39, ...}
        execute_exit(instr)
"""

from __future__ import annotations

import math
from datetime import datetime, time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger


# ────────────────────────────────────────────────────────────────────────────
# Dynamic Stop Loss
# ────────────────────────────────────────────────────────────────────────────

class DynamicStopLoss:
    """
    Calculate a strategy-appropriate stop-loss price.

    ORB             → beyond the opening range boundary (not a fixed %)
    VWAP            → 2.0 × ATR from entry (reversion stop)
    TREND           → 1.5 × ATR (wider for trend runners)
    GAP_FADE        → 0.8 × ATR (tight — extension means the fade is wrong)
    GAP_CONTINUATION→ 1.5 × ATR (wider — gap volatility)
    EOD             → 1.0 × ATR (short hold, moderate)
    LATE_DAY        → fixed-pct (scalp — taken from config)
    default         → 1.5 × ATR
    """

    _ATR_MULT: Dict[str, Optional[float]] = {
        "ORB":               None,    # handled by opening range
        "VWAP":              2.0,
        "TREND":             1.5,
        "GAP_FADE":          0.8,
        "GAP_CONTINUATION":  1.5,
        "EOD":               1.0,
        "LATE_DAY":          None,    # handled by fixed_pct
    }

    def calculate_stop(
        self,
        signal_type: str,
        entry_price: float,
        direction: str,                # "CE" (long), "PE" (short)
        atr: float = 0.0,
        opening_range_low: float = 0.0,
        opening_range_high: float = 0.0,
        fixed_pct: float = 0.0,
    ) -> float:
        """
        Returns an absolute stop-loss price level.
        For CE: stop < entry_price.  For PE: stop > entry_price.
        """
        stype = signal_type.upper()
        mult  = self._ATR_MULT.get(stype, 1.5)

        if stype == "ORB":
            if direction == "CE":
                raw = opening_range_low  * 0.999   # just below range low
            else:
                raw = opening_range_high * 1.001   # just above range high

        elif mult is None or fixed_pct > 0:
            # Fixed-percentage mode (LATE_DAY or explicit override)
            pct = fixed_pct if fixed_pct > 0 else 0.15
            if direction == "CE":
                raw = entry_price * (1 - pct / 100)
            else:
                raw = entry_price * (1 + pct / 100)

        else:
            # ATR-based
            effective_atr = atr if atr > 0 else entry_price * 0.005   # 0.5% fallback
            distance = mult * effective_atr
            if direction == "CE":
                raw = entry_price - distance
            else:
                raw = entry_price + distance

        raw = round(raw, 2)
        logger.debug(
            f"[DynamicSL] {stype} {direction}: entry={entry_price:.2f}  "
            f"stop={raw:.2f}  atr={atr:.2f}  mult={mult}"
        )
        return raw


# ────────────────────────────────────────────────────────────────────────────
# Multi-Target Exit
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class _Target:
    qty_pct: float      # fraction of original position to exit here
    price:   float      # trigger price (0.0 for the trailing runner)
    label:   str        # PARTIAL_1 / PARTIAL_2 / PARTIAL_3 / RUNNER
    hit:     bool = False


class MultiTargetExit:
    """
    Staged exit in four tiers based on risk-reward multiples:

      Tier 1 (40 % of qty)  at  1 × risk  — lock first profit, move SL → BE
      Tier 2 (30 % of qty)  at  2 × risk  — capture meaningful gain
      Tier 3 (20 % of qty)  at  3 × risk  — capture extended move
      Runner (10 % of qty)  → managed by TrailingStop (no fixed target)

    After Tier-1 triggers:  stop_loss property moves to entry_price (breakeven).
    """

    def __init__(
        self,
        entry_price:     float,
        stop_loss_price: float,
        position_size:   int,
        direction:       str = "CE",
    ):
        self.entry_price   = entry_price
        self.stop_loss     = stop_loss_price
        self.original_qty  = position_size
        self.remaining_qty = position_size
        self.direction     = direction.upper()
        self._initial_risk = abs(entry_price - stop_loss_price)
        self._targets      = self._build_targets()
        self._be_set       = False

    def _build_targets(self) -> List[_Target]:
        risk = max(self._initial_risk, 1.0)
        sign = +1 if self.direction == "CE" else -1
        return [
            _Target(0.40, self.entry_price + sign * 1.0 * risk, "PARTIAL_1"),
            _Target(0.30, self.entry_price + sign * 2.0 * risk, "PARTIAL_2"),
            _Target(0.20, self.entry_price + sign * 3.0 * risk, "PARTIAL_3"),
            _Target(0.10, 0.0,                                   "RUNNER"),
        ]

    @property
    def all_fixed_targets_hit(self) -> bool:
        return all(t.hit for t in self._targets if t.label != "RUNNER")

    def check_targets(self, current_price: float) -> Optional[Dict]:
        """
        Returns exit-instruction dict if any target is reached, else None.
        Marks targets as hit and updates stop_loss to BE after Tier-1.
        """
        for target in self._targets:
            if target.hit or target.label == "RUNNER":
                continue

            triggered = (
                current_price >= target.price
                if self.direction == "CE"
                else current_price <= target.price
            )

            if not triggered:
                continue

            target.hit = True
            exit_qty   = max(1, round(self.original_qty * target.qty_pct))
            exit_qty   = min(exit_qty, self.remaining_qty)
            self.remaining_qty = max(0, self.remaining_qty - exit_qty)

            if target.label == "PARTIAL_1" and not self._be_set:
                self.stop_loss = self.entry_price
                self._be_set   = True
                logger.info(f"[MultiTarget] SL moved to breakeven @ {self.stop_loss:.2f}")

            logger.info(
                f"[MultiTarget] {target.label} hit @ {current_price:.2f}  "
                f"exit_qty={exit_qty}  remaining={self.remaining_qty}"
            )
            return {
                "exit_type":     target.label,
                "exit_qty":      exit_qty,
                "remaining_qty": self.remaining_qty,
                "reason":        f"{target.label} target hit @ ₹{current_price:,.2f}",
                "new_stop_loss": self.stop_loss,
            }

        return None


# ────────────────────────────────────────────────────────────────────────────
# Trailing Stop
# ────────────────────────────────────────────────────────────────────────────

class TrailingStop:
    """
    Profit-protecting trailing stop.

    Logic:
      • Activates once option P&L ≥ 20 %.  On activation: SL → breakeven.
      • Tracks high-water mark (CE) or low-water mark (PE).
      • Trail percentage tightens as profit grows:
            20 – 30 % profit  →  give back 50 % of move
            30 – 50 % profit  →  give back 40 % of move
            > 50 %   profit   →  give back 30 % of move (protect runner)
      • Stop only moves in profit direction (never relaxes).
    """

    def __init__(
        self,
        entry_price:  float,
        initial_stop: float,
        direction:    str = "CE",
    ):
        self.entry_price  = entry_price
        self.stop_loss    = initial_stop
        self.direction    = direction.upper()
        self._best_price  = entry_price   # high-water (CE) / low-water (PE)
        self._active      = False
        self._activation_pct = 20.0

    @property
    def is_active(self) -> bool:
        return self._active

    def update(self, current_price: float, profit_pct: float) -> bool:
        """
        Progress the trail.  Returns True if the trailing stop was hit
        (position should be closed).
        """
        # Arm the trail once minimum profit is reached
        if not self._active and profit_pct >= self._activation_pct:
            self._active   = True
            self.stop_loss = self.entry_price   # move to breakeven
            logger.info(
                f"[TrailingStop] Activated at profit={profit_pct:.1f}%  "
                f"SL → breakeven={self.entry_price:.2f}"
            )

        if not self._active:
            return False

        # Update high/low water
        if self.direction == "CE":
            if current_price > self._best_price:
                self._best_price = current_price
        else:
            if current_price < self._best_price:
                self._best_price = current_price

        # Determine give-back fraction
        if profit_pct < 30:
            give_back_pct = 0.50
        elif profit_pct < 50:
            give_back_pct = 0.40
        else:
            give_back_pct = 0.30

        total_move = abs(self._best_price - self.entry_price)
        give_back  = total_move * give_back_pct

        if self.direction == "CE":
            new_stop = self._best_price - give_back
            if new_stop > self.stop_loss:
                logger.debug(
                    f"[TrailingStop] CE stop ↑ {self.stop_loss:.2f} → {new_stop:.2f}  "
                    f"(best={self._best_price:.2f})"
                )
                self.stop_loss = new_stop
            return current_price <= self.stop_loss

        else:  # PE
            new_stop = self._best_price + give_back
            if new_stop < self.stop_loss:
                logger.debug(
                    f"[TrailingStop] PE stop ↓ {self.stop_loss:.2f} → {new_stop:.2f}  "
                    f"(best={self._best_price:.2f})"
                )
                self.stop_loss = new_stop
            return current_price >= self.stop_loss


# ────────────────────────────────────────────────────────────────────────────
# Time-Based Exit
# ────────────────────────────────────────────────────────────────────────────

class TimeBasedExit:
    """
    Smart time-based exit rules — tailored per strategy type and P&L state.

    Rules (priority order):
      1. Strategy max hold time exceeded
      2. Losing trade at 14:30 → cut loss before EOD theta erosion
      3. < 10 % profit at 15:00 → book small gain with 15 min left
      4. Hard force-exit at 15:15 IST
    """

    _MAX_HOLD_MINUTES: Dict[str, int] = {
        "ORB":               120,
        "VWAP":               60,
        "TREND":             300,
        "GAP_FADE":           45,
        "GAP_CONTINUATION":   90,
        "EOD":                60,
        "LATE_DAY":           12,
    }

    def should_exit(
        self,
        strategy:     str,
        entry_time:   datetime,
        profit_pct:   float,
        current_time: Optional[time] = None,
    ) -> Tuple[bool, str]:
        """Returns (should_exit, reason_string)."""
        now      = current_time or datetime.now().time()
        elapsed  = (datetime.now() - entry_time).total_seconds() / 60
        max_hold = self._MAX_HOLD_MINUTES.get(strategy.upper(), 120)

        if elapsed >= max_hold:
            return True, f"{strategy} max hold ({max_hold} min) reached"

        if profit_pct < 0 and now >= time(14, 30):
            return True, "Losing trade — exiting before 14:30 to avoid EOD theta drag"

        if 0 <= profit_pct < 10 and now >= time(15, 0):
            return True, "< 10 % profit with < 15 min remaining — booking small gain"

        if now >= time(15, 15):
            return True, "Force-exit 15:15 IST — no overnight risk"

        return False, ""


# ────────────────────────────────────────────────────────────────────────────
# Volatility Exit
# ────────────────────────────────────────────────────────────────────────────

class VolatilityExit:
    """
    Exit when market volatility changes significantly from entry conditions.

    Rules:
      • VIX spike > 20 % while profitable → lock gains
      • VIX collapse < −20 % → mean-reversion edge disappears
      • ATR expansion > 1.5× entry ATR in scalp strategies → conditions changed
      • VIX > 25 with no profit → cut loss now
    """

    def check(
        self,
        strategy:          str,
        entry_vix:         float,
        current_vix:       float,
        profit_pct:        float,
        current_atr_ratio: float = 1.0,
    ) -> Tuple[bool, str]:
        """Returns (should_exit, reason_string)."""
        if entry_vix <= 0:
            return False, ""

        vix_change_pct = (current_vix - entry_vix) / entry_vix * 100
        stype          = strategy.upper()

        # VIX spiked while in profit → lock gains before reversal
        if vix_change_pct > 20 and profit_pct > 15:
            return (
                True,
                f"VIX spiked +{vix_change_pct:.1f}% — locking {profit_pct:.1f}% profit",
            )

        # VIX collapsed → mean-reversion trades lose edge
        if vix_change_pct < -20 and stype in ("VWAP", "EOD", "LATE_DAY"):
            return (
                True,
                f"VIX dropped {abs(vix_change_pct):.1f}% — mean-reversion edge reduced",
            )

        # ATR expansion in scalp strategies
        if current_atr_ratio > 1.5 and stype in ("LATE_DAY", "VWAP", "GAP_FADE"):
            return (
                True,
                f"ATR expanded {current_atr_ratio:.1f}× — scalp environment changed",
            )

        # VIX danger zone with no profit
        if current_vix > 25 and profit_pct <= 0:
            return True, f"VIX={current_vix:.1f} danger zone — cutting loss"

        return False, ""


# ────────────────────────────────────────────────────────────────────────────
# Master Exit Manager
# ────────────────────────────────────────────────────────────────────────────

class ExitManager:
    """
    Per-position exit coordinator.

    Create one instance per trade.  Call evaluate_exit() on each tick.

    Example:
        mgr = ExitManager("ORB", 24000, 23700, 65, "CE")
        ...
        instr = mgr.evaluate_exit({"qty": 65, "pnl_pct": 18.0}, {"current_price": 24180, "vix": 15.2})
        if instr:
            # instr["exit_type"] is one of: STOP_LOSS / PARTIAL_1 / PARTIAL_2 /
            #   PARTIAL_3 / TRAILING_STOP / TIME_EXIT / VOL_EXIT
    """

    def __init__(
        self,
        strategy:      str,
        entry_price:   float,
        stop_loss:     float,
        position_size: int,
        direction:     str      = "CE",
        entry_time:    Optional[datetime] = None,
        entry_vix:     float    = 0.0,
    ):
        self.strategy    = strategy.upper()
        self.entry_price = entry_price
        self.entry_time  = entry_time or datetime.now()
        self.entry_vix   = entry_vix
        self.direction   = direction.upper()

        # The "effective" SL is the tightest of initial SL and any automatic
        # SL adjustment (e.g., BE after Tier-1).
        self._initial_sl    = stop_loss
        self._multi_target  = MultiTargetExit(entry_price, stop_loss, position_size, direction)
        self._trail         = TrailingStop(entry_price, stop_loss, direction)
        self._time_exit     = TimeBasedExit()
        self._vol_exit      = VolatilityExit()

    # -- properties -----------------------------------------------------------

    @property
    def current_stop_loss(self) -> float:
        """Effective stop — tightest of initial SL and any automatic adjustments."""
        mt_sl = self._multi_target.stop_loss
        if self.direction == "CE":
            return max(self._initial_sl, mt_sl)
        return min(self._initial_sl, mt_sl)

    @property
    def is_trailing_active(self) -> bool:
        return self._trail.is_active

    # -- main evaluation ------------------------------------------------------

    def evaluate_exit(
        self,
        position:    Dict,
        market_data: Dict,
    ) -> Optional[Dict]:
        """
        Evaluate all exit conditions and return the first triggered one.

        position keys  : qty (int), pnl_pct (float option %)
        market_data keys: current_price, vix, current_atr (optional), entry_atr (optional)

        Returns dict with:
            exit_type, exit_qty, remaining_qty, reason
            optionally: new_stop_loss
        Or None if no exit is triggered.
        """
        current_price = float(market_data.get("current_price", self.entry_price))
        current_vix   = float(market_data.get("vix", self.entry_vix) or self.entry_vix)
        pnl_pct       = float(position.get("pnl_pct", 0.0))
        qty           = int(position.get("qty", self._multi_target.original_qty))
        entry_atr     = float(market_data.get("entry_atr", 0.0) or 0.0)
        current_atr   = float(market_data.get("current_atr", 0.0) or 0.0)
        atr_ratio     = (current_atr / entry_atr) if entry_atr > 0 else 1.0
        # Allow caller to override current wall-clock time (useful in tests)
        _current_time: Optional[time] = market_data.get("current_time")
        if isinstance(_current_time, str):
            try:
                from datetime import time as _t
                h, m = _current_time.split(":")[:2]
                _current_time = _t(int(h), int(m))
            except Exception:
                _current_time = None

        effective_sl  = self.current_stop_loss

        # ── 1. Hard stop loss ────────────────────────────────────────────────
        sl_hit = (
            current_price <= effective_sl
            if self.direction == "CE"
            else current_price >= effective_sl
        )
        if sl_hit:
            logger.warning(
                f"[ExitMgr] STOP_LOSS  price={current_price:.2f}  sl={effective_sl:.2f}  "
                f"pnl={pnl_pct:+.1f}%"
            )
            return {
                "exit_type":     "STOP_LOSS",
                "exit_qty":      qty,
                "remaining_qty": 0,
                "reason":        f"Stop loss hit @ ₹{current_price:,.2f}",
            }

        # ── 2. Multi-target (partial exits) ──────────────────────────────────
        target_instr = self._multi_target.check_targets(current_price)
        if target_instr:
            return target_instr

        # ── 3. Trailing stop ─────────────────────────────────────────────────
        if self._trail.update(current_price, pnl_pct):
            logger.info(
                f"[ExitMgr] TRAILING_STOP  price={current_price:.2f}  "
                f"trail_sl={self._trail.stop_loss:.2f}  pnl={pnl_pct:+.1f}%"
            )
            return {
                "exit_type":     "TRAILING_STOP",
                "exit_qty":      qty,
                "remaining_qty": 0,
                "reason": (
                    f"Trailing stop hit @ ₹{current_price:,.2f}  "
                    f"(trail SL was ₹{self._trail.stop_loss:,.2f})"
                ),
            }

        # ── 4. Time-based exit ───────────────────────────────────────────────
        t_exit, t_reason = self._time_exit.should_exit(
            self.strategy, self.entry_time, pnl_pct, current_time=_current_time
        )
        if t_exit:
            logger.info(f"[ExitMgr] TIME_EXIT: {t_reason}")
            return {
                "exit_type":     "TIME_EXIT",
                "exit_qty":      qty,
                "remaining_qty": 0,
                "reason":        t_reason,
            }

        # ── 5. Volatility exit ───────────────────────────────────────────────
        v_exit, v_reason = self._vol_exit.check(
            self.strategy, self.entry_vix, current_vix, pnl_pct, atr_ratio
        )
        if v_exit:
            logger.info(f"[ExitMgr] VOL_EXIT: {v_reason}")
            return {
                "exit_type":     "VOL_EXIT",
                "exit_qty":      qty,
                "remaining_qty": 0,
                "reason":        v_reason,
            }

        return None   # no exit signal

    def format_status(self) -> str:
        """One-line summary useful for console display."""
        return (
            f"ExitMgr[{self.strategy} {self.direction}]  "
            f"entry={self.entry_price:.2f}  sl={self.current_stop_loss:.2f}  "
            f"trail={'ACTIVE' if self._trail.is_active else 'armed@+20%'}  "
            f"targets_hit={sum(1 for t in self._multi_target._targets if t.hit)}/3"
        )
