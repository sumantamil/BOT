"""
bot/late_day_oscillation.py
────────────────────────────
Late-Day Oscillation Strategy (14:30–15:25 IST)

PATTERN: Between 14:30 and 15:30, NIFTY oscillates in ~15-minute cycles as
retail traders, algos, and institutions square off positions before close.
We detect the direction of each 15-min segment and trade the reversal.

USAGE (from engine):
    from bot.late_day_oscillation import LateDayOscillationStrategy
    strat = LateDayOscillationStrategy()

    # Each analysis tick:
    signal = strat.detect_cycle_signal(now, current_price, candles_df)
    if signal:
        # execute option trade matching signal.direction

    # In position monitor:
    exit_action = strat.manage_position(now, current_ltp)
    if exit_action == "EXIT":
        # close the open option position
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, time, date, timedelta
from typing import Optional, Set
from loguru import logger

from config import settings


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OscillationSignal:
    """Signal generated at a late-day cycle checkpoint."""
    direction: str          # "CE" (expect UP) or "PE" (expect DOWN)
    entry_price: float      # current index price when signal fires
    stop_loss_pct: float    # e.g. 0.15  (applied to option LTP)
    target_pct: float       # e.g. 0.25
    cycle: int              # 1=14:45, 2=15:00, 3=15:15
    confidence: int         # 0–100
    exit_time: str          # "14:58", "15:13", "15:25"
    reasoning: str
    last_move_direction: str = ""  # "UP" or "DOWN" (the move we're fading)


@dataclass
class OscillationPosition:
    """Tracks the currently open late-day position."""
    direction: str
    entry_price: float      # option entry LTP
    stop_loss: float
    target: float
    entry_time: datetime
    cycle: int
    force_exit_time: time   # hard stop


# ─────────────────────────────────────────────────────────────────────────────
# Strategy
# ─────────────────────────────────────────────────────────────────────────────

class LateDayOscillationStrategy:
    """
    Trades the mean-reversion oscillation pattern between 14:30–15:25 IST.

    Cycle checkpoints (where we look for entry signals):
        14:45  →  evaluate 14:30–14:45 move → trade reversal
        15:00  →  evaluate 14:45–15:00 move → trade reversal
        15:15  →  evaluate 15:00–15:15 move → trade reversal

    Risk controls:
        Target   : +0.25% on option LTP
        Stop     : -0.15% on option LTP
        Max time : 12 minutes
        Force exit: 15:25 IST

    Circuit breakers:
        Skip if: VIX > max_vix, intraday range > max_intraday_range,
                 consecutive_losses >= max_consecutive_losses
    """

    CHECKPOINTS  : tuple[time, ...] = (time(14, 45), time(15, 0), time(15, 15))
    START_TIME   : time = time(14, 30)
    FORCE_EXIT   : time = time(15, 25)

    # Checkpoint window: accept signal within N minutes AFTER checkpoint
    _CP_WINDOW_MIN: int = 3

    def __init__(self):
        self._last_date: Optional[date] = None
        self._reset_daily()

    # ── Daily reset (called automatically when date changes) ──────────────────

    def _reset_daily(self):
        """Reset all within-day state."""
        self.reference_price: Optional[float]    = None
        self.reference_time:  Optional[datetime] = None
        self.last_cycle_direction: Optional[str] = None
        self.cycle_count: int                    = 0
        self.active_position: Optional[OscillationPosition] = None
        self._traded_checkpoints: Set[time]      = set()

        # Stats
        self.today_cycles:       int   = 0
        self.today_wins:         int   = 0
        self.today_losses:       int   = 0
        self.today_pnl:          float = 0.0
        self.consecutive_losses: int   = 0

    def _ensure_fresh_day(self):
        today = datetime.now().date()
        if self._last_date != today:
            self._reset_daily()
            self._last_date = today

    # ── Public API ────────────────────────────────────────────────────────────

    def should_activate(self, now: datetime, market_data: dict) -> bool:
        """
        Return True if the strategy should scan for a signal at *now*.

        Filters checked:
        - enabled flag
        - time within 14:30–15:25
        - VIX ≤ max_vix
        - intraday range ≤ max_intraday_range (avoid strongly trending days)
        - consecutive losses < circuit-breaker limit
        """
        cfg = settings.late_day
        if not cfg.enabled:
            return False

        now_t = now.time()
        if not (self.START_TIME <= now_t <= self.FORCE_EXIT):
            return False

        # VIX filter
        vix = market_data.get("vix", 0.0)
        if vix > cfg.max_vix and vix > 0:
            logger.debug(f"Late-day SKIP: VIX {vix:.1f} > {cfg.max_vix}")
            return False

        # Trending day filter (intraday range too wide)
        intraday_range = market_data.get("intraday_range_pct", 0.0)
        if intraday_range > cfg.max_intraday_range and intraday_range > 0:
            logger.debug(f"Late-day SKIP: intraday range {intraday_range:.2f}% (trending day)")
            return False

        # Loss circuit-breaker
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            logger.info(
                f"Late-day PAUSED: {self.consecutive_losses} consecutive losses "
                f"(limit {cfg.max_consecutive_losses})"
            )
            return False

        return True

    def set_reference(self, price: float, at_time: datetime):
        """Record the 14:30 pivot price (call once at strategy activation)."""
        if self.reference_price is None:
            self.reference_price = price
            self.reference_time  = at_time
            logger.info(
                f"Late-day reference price set: ₹{price:,.2f} "
                f"at {at_time.strftime('%H:%M:%S')} IST"
            )

    def detect_cycle_signal(
        self,
        now: datetime,
        current_price: float,
        candles: pd.DataFrame,
    ) -> Optional[OscillationSignal]:
        """
        Evaluate whether to enter a trade at the current moment.

        Should be called every analysis tick (60-second loop).
        Returns an OscillationSignal or None.
        """
        self._ensure_fresh_day()
        cfg = settings.late_day

        now_t = now.time()

        # Capture reference price at strategy start
        if self.reference_price is None and now_t >= self.START_TIME:
            self.set_reference(current_price, now)

        # Find the active checkpoint we're currently in
        checkpoint = self._active_checkpoint(now_t)
        if checkpoint is None:
            return None

        # Don't re-trade the same checkpoint
        if checkpoint in self._traded_checkpoints:
            return None

        # Never open a new trade while one is already open
        if self.active_position is not None:
            return None

        # Enforce max cycles
        if self.today_cycles >= cfg.max_cycles:
            return None

        # Need a minimum candle history
        if candles is None or candles.empty or len(candles) < 4:
            return None

        # Determine last 15-min direction
        last_move = self._last_15m_direction(candles, now)
        if last_move is None:
            return None

        # Trade opposite direction
        expected_dir  = "UP"   if last_move == "DOWN" else "DOWN"
        option_type   = "CE"   if expected_dir == "UP" else "PE"

        # ── Confirmation filters ──────────────────────────────────────────────

        if cfg.require_rsi_extreme:
            if not self._rsi_confirms(candles, expected_dir):
                logger.debug(
                    f"Late-day [{checkpoint.strftime('%H:%M')}]: "
                    f"RSI doesn't confirm {expected_dir} at price {current_price:.2f}"
                )
                # Mark as scanned so we don't spam logs; the signal just doesn't fire
                self._traded_checkpoints.add(checkpoint)
                return None

        if cfg.require_volume_spike:
            if not self._volume_confirms(candles):
                logger.debug(f"Late-day [{checkpoint.strftime('%H:%M')}]: volume too low")
                self._traded_checkpoints.add(checkpoint)
                return None

        if cfg.require_confirmation:
            if not self._reversal_candle_confirms(candles, expected_dir):
                logger.debug(f"Late-day [{checkpoint.strftime('%H:%M')}]: no reversal candle yet")
                # DO NOT mark traded — let it retry next tick until window closes
                return None

        # ── Build signal ──────────────────────────────────────────────────────

        cycle_num  = self.today_cycles + 1
        confidence = self._score_confidence(candles, expected_dir, cycle_num)

        if confidence < cfg.min_pattern_confidence:
            logger.debug(
                f"Late-day [{checkpoint.strftime('%H:%M')}]: "
                f"confidence {confidence}% below threshold {cfg.min_pattern_confidence}%"
            )
            self._traded_checkpoints.add(checkpoint)
            return None

        exit_str = self._exit_time_str(checkpoint)

        reasoning = (
            f"Cycle {cycle_num}: last 15m moved {last_move} → "
            f"expect {expected_dir} reversal at {checkpoint.strftime('%H:%M')} IST. "
            f"RSI/vol confirmed. Exit by {exit_str}."
        )

        logger.info(
            f"Late-day signal: {option_type} at cycle {cycle_num} "
            f"({checkpoint.strftime('%H:%M')}) | confidence {confidence}% | {reasoning}"
        )

        self._traded_checkpoints.add(checkpoint)

        return OscillationSignal(
            direction           = option_type,
            entry_price         = current_price,
            stop_loss_pct       = cfg.stop_loss_pct,
            target_pct          = cfg.target_pct,
            cycle               = cycle_num,
            confidence          = confidence,
            exit_time           = exit_str,
            reasoning           = reasoning,
            last_move_direction = last_move,
        )

    def register_position(self, signal: OscillationSignal, option_ltp: float):
        """
        Call this once the order has been placed to start tracking the position.

        option_ltp: the actual option price at entry (used for P&L).
        """
        cfg = settings.late_day
        stop  = option_ltp * (1 - cfg.stop_loss_pct / 100)
        tgt   = option_ltp * (1 + cfg.target_pct   / 100)

        self.active_position = OscillationPosition(
            direction       = signal.direction,
            entry_price     = option_ltp,
            stop_loss       = stop,
            target          = tgt,
            entry_time      = datetime.now(),
            cycle           = signal.cycle,
            force_exit_time = self.FORCE_EXIT,
        )
        self.cycle_count   += 1
        self.today_cycles  += 1
        self.last_cycle_direction = signal.direction

        logger.info(
            f"Late-day position registered: {signal.direction} "
            f"option @ ₹{option_ltp:.2f} | SL ₹{stop:.2f} | T ₹{tgt:.2f} | "
            f"force-exit {self.FORCE_EXIT.strftime('%H:%M')}"
        )

    def manage_position(
        self,
        now: datetime,
        option_ltp: float,
    ) -> Optional[str]:
        """
        Check exit conditions for the open position.

        Returns:
            "EXIT"  – close the position now
            None    – hold
        """
        if self.active_position is None:
            return None

        cfg = settings.late_day
        pos = self.active_position
        now_t = now.time()

        # 1. Force exit at 15:25
        if now_t >= self.FORCE_EXIT:
            pnl_pct = self._option_pnl_pct(option_ltp)
            self._record_close(option_ltp, "FORCE_EXIT", pnl_pct)
            logger.warning(
                f"Late-day: force-closing position at {now_t.strftime('%H:%M')} "
                f"| P&L ≈ {pnl_pct:+.2f}%"
            )
            return "EXIT"

        # 2. Target hit
        if option_ltp >= pos.target:
            pnl_pct = self._option_pnl_pct(option_ltp)
            self._record_close(option_ltp, "TARGET_HIT", pnl_pct)
            logger.info(f"Late-day: target hit ₹{option_ltp:.2f} | P&L {pnl_pct:+.2f}%")
            return "EXIT"

        # 3. Stop loss hit
        if option_ltp <= pos.stop_loss:
            pnl_pct = self._option_pnl_pct(option_ltp)
            self._record_close(option_ltp, "STOP_HIT", pnl_pct)
            logger.warning(f"Late-day: stop hit ₹{option_ltp:.2f} | P&L {pnl_pct:+.2f}%")
            return "EXIT"

        # 4. Time-in-position limit
        minutes_in = (now - pos.entry_time).total_seconds() / 60
        if minutes_in >= cfg.max_time_minutes:
            pnl_pct = self._option_pnl_pct(option_ltp)
            self._record_close(option_ltp, "TIME_EXIT", pnl_pct)
            logger.info(
                f"Late-day: time-exit after {minutes_in:.0f} min | P&L {pnl_pct:+.2f}%"
            )
            return "EXIT"

        return None

    def skip_next_cycle(self) -> Optional[str]:
        """
        Manually skip the next unfired cycle checkpoint.
        Returns the time string of the skipped checkpoint, or None.
        """
        now_t = datetime.now().time()
        for cp in self.CHECKPOINTS:
            if cp > now_t and cp not in self._traded_checkpoints:
                self._traded_checkpoints.add(cp)
                logger.info(f"Late-day: manually skipped cycle at {cp.strftime('%H:%M')}")
                return cp.strftime("%H:%M")
        return None

    def next_cycle_time(self) -> Optional[str]:
        """Return HH:MM of the next unfired checkpoint, or None if all done."""
        now_t = datetime.now().time()
        for cp in self.CHECKPOINTS:
            if cp > now_t and cp not in self._traded_checkpoints:
                return cp.strftime("%H:%M")
        return None

    def format_status(self) -> str:
        """Full console status display for the 'lateday' command."""
        cfg = settings.late_day
        now = datetime.now()
        now_t = now.time()

        active_str = "✅ YES" if (self.START_TIME <= now_t <= self.FORCE_EXIT) else "❌ NO"
        paused_str = ""
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            paused_str = " ⚠️ PAUSED (loss streak)"

        pos_block = "  ⚪ No active position"
        if self.active_position:
            pos = self.active_position
            mins_in  = (now - pos.entry_time).total_seconds() / 60
            pos_block = (
                f"  ✅ ACTIVE\n"
                f"     Direction   : {pos.direction}\n"
                f"     Entry (LTP) : ₹{pos.entry_price:,.2f}\n"
                f"     SL / Target : ₹{pos.stop_loss:,.2f} / ₹{pos.target:,.2f}\n"
                f"     Time in trade: {mins_in:.0f} min\n"
                f"     Force exit  : {pos.force_exit_time.strftime('%H:%M')} IST"
            )

        ref_str  = f"₹{self.reference_price:,.2f}" if self.reference_price else "Not set (activates at 14:30)"
        next_cp  = self.next_cycle_time() or "All cycles done for today"

        win_rate_str = "N/A"
        done = self.today_wins + self.today_losses
        if done > 0:
            win_rate_str = f"{self.today_wins / done * 100:.0f}%"

        lines = [
            "",
            "╔════════════════════════════════════════════════╗",
            "║     LATE-DAY OSCILLATION STATUS               ║",
            "╚════════════════════════════════════════════════╝",
            "",
            f"  ⏰ Time       : {now.strftime('%H:%M:%S')} IST",
            f"  📊 Window Active: {active_str}{paused_str}",
            f"  ⚙️  Enabled   : {'Yes' if cfg.enabled else 'No — set LATE_DAY_ENABLED=true in .env'}",
            "",
            f"  🔄 Cycles     : {self.today_cycles}/{cfg.max_cycles} done today",
            f"  📍 Reference  : {ref_str}",
            f"  ↕️  Last dir  : {self.last_cycle_direction or 'N/A'}",
            "",
            "  📈 Current Position:",
            pos_block,
            "",
            f"  ⏭️  Next Cycle : {next_cp}",
            f"  🛑 Force Exit  : 15:25 IST",
            "",
            "  📊 Today's Stats:",
            f"     Cycles attempted : {self.today_cycles}",
            f"     Wins / Losses    : {self.today_wins} / {self.today_losses}",
            f"     Win rate         : {win_rate_str}",
            f"     Consec. losses   : {self.consecutive_losses}"
            + (" ⚠️ PAUSED" if self.consecutive_losses >= cfg.max_consecutive_losses else ""),
            "",
            "  [Commands: lateday skip | lateday on | lateday off | lateday history]",
        ]
        return "\n".join(lines)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _active_checkpoint(self, now_t: time) -> Optional[time]:
        """Return the checkpoint we are currently within (±CP_WINDOW_MIN minutes)."""
        now_min = now_t.hour * 60 + now_t.minute
        for cp in self.CHECKPOINTS:
            cp_min = cp.hour * 60 + cp.minute
            if 0 <= (now_min - cp_min) <= self._CP_WINDOW_MIN:
                return cp
        return None

    def _last_15m_direction(
        self, candles: pd.DataFrame, now: datetime
    ) -> Optional[str]:
        """
        Determine direction (UP/DOWN) of the 15-minute window just before *now*.

        Uses the last 3 × 5-min candles (= 15 minutes of data).
        Returns None if the move is too small to classify.
        """
        try:
            recent = candles.tail(3)
            if len(recent) < 2:
                return None
            open_  = float(recent.iloc[0]["Open"])
            close_ = float(recent.iloc[-1]["Close"])
            if open_ == 0:
                return None
            change_pct = (close_ - open_) / open_ * 100
            if abs(change_pct) < 0.05:   # indecision — too small
                return None
            return "UP" if change_pct > 0 else "DOWN"
        except Exception as e:
            logger.debug(f"Late-day direction calc error: {e}")
            return None

    def _rsi_confirms(self, candles: pd.DataFrame, expected_dir: str) -> bool:
        """RSI must be in an extreme zone for the expected reversal."""
        try:
            rsi = _calc_rsi(candles["Close"].values)
            if expected_dir == "UP":
                return rsi < 40       # oversold → expect bounce
            else:
                return rsi > 60       # overbought → expect drop
        except Exception:
            return True   # don't block on calculation error

    def _volume_confirms(self, candles: pd.DataFrame) -> bool:
        """Most recent candle volume ≥ avg volume × min_volume_ratio."""
        try:
            cfg = settings.late_day
            avg = candles["Volume"].mean()
            if avg <= 0:
                return True
            latest = float(candles["Volume"].iloc[-1])
            return latest >= avg * cfg.min_volume_ratio
        except Exception:
            return True

    def _reversal_candle_confirms(
        self, candles: pd.DataFrame, expected_dir: str
    ) -> bool:
        """Last completed candle must point in the expected direction."""
        try:
            last  = candles.iloc[-1]
            open_ = float(last["Open"])
            close = float(last["Close"])
            if expected_dir == "UP":
                return close > open_    # green candle
            else:
                return close < open_    # red candle
        except Exception:
            return True

    def _score_confidence(
        self, candles: pd.DataFrame, expected_dir: str, cycle: int
    ) -> int:
        """Score the setup from 0–100."""
        score = 50

        # RSI bonus
        try:
            rsi = _calc_rsi(candles["Close"].values)
            if expected_dir == "UP":
                if rsi < 35:
                    score += 15
                elif rsi < 40:
                    score += 8
            else:
                if rsi > 65:
                    score += 15
                elif rsi > 60:
                    score += 8
        except Exception:
            pass

        # Volume spike bonus
        try:
            avg = candles["Volume"].mean()
            if avg > 0:
                ratio = float(candles["Volume"].iloc[-1]) / avg
                if ratio > 1.5:
                    score += 10
                elif ratio > 1.2:
                    score += 5
        except Exception:
            pass

        # Cycle timing bonus/penalty
        if cycle == 2:
            score += 5    # 15:00 cycle most reliable historically
        elif cycle == 3:
            score -= 5    # 15:15 cycle less reliable

        # Oscillation from reference price (sweet-spot amplitude)
        if self.reference_price:
            try:
                price = float(candles["Close"].iloc[-1])
                deviation = abs(price - self.reference_price) / self.reference_price * 100
                if 0.2 <= deviation <= 0.5:
                    score += 5
            except Exception:
                pass

        return min(100, max(0, score))

    def _option_pnl_pct(self, current_ltp: float) -> float:
        """Return P&L % on the open option position."""
        if self.active_position is None or self.active_position.entry_price == 0:
            return 0.0
        return (current_ltp - self.active_position.entry_price) / self.active_position.entry_price * 100

    def _record_close(self, exit_price: float, reason: str, pnl_pct: float):
        """Record exit stats and clear active position."""
        if self.active_position is None:
            return
        is_win = pnl_pct > 0
        if is_win:
            self.today_wins += 1
            self.consecutive_losses = 0
        else:
            self.today_losses      += 1
            self.consecutive_losses += 1
        self.today_pnl += pnl_pct
        logger.info(
            f"Late-day closed [{reason}]: "
            f"{self.active_position.direction} exit @ ₹{exit_price:.2f} | "
            f"P&L ≈ {pnl_pct:+.2f}% | consec losses: {self.consecutive_losses}"
        )
        self.active_position = None

    @staticmethod
    def _exit_time_str(checkpoint: time) -> str:
        """Derive force-exit time string based on checkpoint."""
        exits = {
            time(14, 45): "14:58",
            time(15,  0): "15:13",
            time(15, 15): "15:25",
        }
        return exits.get(checkpoint, "15:25")


# ─────────────────────────────────────────────────────────────────────────────
# Module-level RSI helper
# ─────────────────────────────────────────────────────────────────────────────

def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Wilder's RSI from a close-price array."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes.astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[-period:]))
    avg_loss = float(np.mean(losses[-period:]))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
