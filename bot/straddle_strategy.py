"""
Long Straddle / Long Strangle Strategy
=======================================
Buys one ATM CE + one ATM PE simultaneously when the market is likely to make
a big directional move but the direction is uncertain.

Entry conditions (all must be met):
  1. Time between 9:30 AM and 11:00 AM IST
  2. India VIX between 13 and 22 (not too cheap, not too expensive to buy)
  3. Market opened relatively flat — gap < 0.5% (otherwise directional trade is better)
  4. Regime is RANGING or VOLATILE (trending conditions → use Gap/ORB instead)
  5. Not already an open straddle today for this index

Strike selection:
  - Straddle  → both legs at ATM (nearest 50-point strike)
  - Strangle  → CE 200 pts above ATM, PE 200 pts below ATM

Leg management (independent per-leg tracking):
  - Each leg has its own entry premium, SL (−20%), and profit target (+50%)
  - check_legs() evaluates each leg independently — profitable leg is held while
    the losing leg is exited early
  - Combined P&L logged after both legs close

Paper mode support:
  - Works identically without a real broker connection
  - paper_entry() records both legs; paper_check_exits() monitors them
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, date
from typing import Dict, List, Optional, Tuple

from loguru import logger

from config import settings

# ---------------------------------------------------------------------------
# Entry conditions
# ---------------------------------------------------------------------------
_ENTRY_START = dtime(9, 30)
_ENTRY_END   = dtime(11, 0)
_VIX_MIN     = 13.0
_VIX_MAX     = 22.0
_MAX_GAP_PCT = 0.5        # flat open required
_STRIKE_STEP = 50         # NIFTY/BANKNIFTY rounds to nearest 50; override in strategy call if needed
_STRANGLE_OFFSET = 200    # pts above/below ATM for strangle legs

# Exit thresholds (% of premium entry)
_LEG_SL_PCT     = 25.0   # exit individual leg at −25%
_LEG_TARGET_PCT = 60.0   # exit individual leg at +60%
_COMBINED_STOP  = -35.0  # if combined P&L% ever hits −35% → exit both immediately


@dataclass
class StraddleLeg:
    """One leg (CE or PE) of a straddle/strangle position."""
    option_type:    str           # "CE" or "PE"
    strike:         int
    premium_entry:  float
    index_entry:    float
    entry_time:     datetime = field(default_factory=datetime.now)
    premium_exit:   float    = 0.0
    exit_time:      Optional[datetime] = None
    exit_reason:    str      = ""
    pnl:            float    = 0.0
    pnl_pct:        float    = 0.0
    status:         str      = "OPEN"   # OPEN | CLOSED

    def close(self, exit_premium: float, reason: str) -> None:
        self.premium_exit = exit_premium
        self.exit_time    = datetime.now()
        self.exit_reason  = reason
        self.pnl_pct     = (exit_premium - self.premium_entry) / self.premium_entry * 100
        self.pnl         = exit_premium - self.premium_entry
        self.status       = "CLOSED"


@dataclass
class StraddlePosition:
    """One open straddle/strangle trade covering both legs."""
    index_name:     str
    strategy_type:  str          # "STRADDLE" or "STRANGLE"
    ce_leg:         StraddleLeg
    pe_leg:         StraddleLeg
    entry_time:     datetime = field(default_factory=datetime.now)
    status:         str      = "OPEN"   # OPEN | CLOSED

    @property
    def total_premium_entry(self) -> float:
        return self.ce_leg.premium_entry + self.pe_leg.premium_entry

    @property
    def combined_pnl(self) -> float:
        return self.ce_leg.pnl + self.pe_leg.pnl

    @property
    def combined_pnl_pct(self) -> float:
        base = self.total_premium_entry
        return self.combined_pnl / base * 100 if base > 0 else 0.0

    def is_fully_closed(self) -> bool:
        return self.ce_leg.status == "CLOSED" and self.pe_leg.status == "CLOSED"


class StraddleStrategy:
    """Manages Long Straddle / Strangle entries and independent leg exits."""

    def __init__(self) -> None:
        self._positions: List[StraddlePosition]  = []   # open straddles
        self._history:   List[StraddlePosition]  = []   # closed
        # Guard: max 1 straddle per index per day
        self._triggered_today: Dict[str, Optional[date]] = {}

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def should_enter(
        self,
        index_name: str,
        nifty_level: float,
        vix: float,
        gap_pct: float,
        regime: Optional[str],
    ) -> bool:
        """
        Return True when all entry conditions for a straddle are met.
        Regime should be one of: RANGING, VOLATILE, TRENDING_UP, TRENDING_DOWN, or None.
        """
        now = datetime.now().time()

        if not (self._entry_window(now)):
            logger.debug(f"Straddle/{index_name}: outside entry window ({now})")
            return False

        if self._triggered_today.get(index_name) == datetime.now().date():
            logger.debug(f"Straddle/{index_name}: already entered today")
            return False

        if not (_VIX_MIN <= vix <= _VIX_MAX):
            logger.debug(f"Straddle/{index_name}: VIX {vix:.1f} outside [{_VIX_MIN}–{_VIX_MAX}]")
            return False

        if abs(gap_pct) > _MAX_GAP_PCT:
            logger.debug(
                f"Straddle/{index_name}: market not flat — gap {gap_pct:+.2f}% "
                f"(max ±{_MAX_GAP_PCT}%)"
            )
            return False

        if regime and regime.upper() in ("TRENDING_UP", "TRENDING_DOWN"):
            logger.debug(f"Straddle/{index_name}: regime={regime} is trending → skip straddle")
            return False

        logger.info(
            f"✅ Straddle/{index_name}: all conditions met | "
            f"VIX={vix:.1f} | gap={gap_pct:+.2f}% | regime={regime}"
        )
        return True

    def get_strikes(
        self,
        nifty_level: float,
        strategy_type: str = "STRADDLE",
        strike_step: int   = _STRIKE_STEP,
    ) -> Tuple[int, int]:
        """
        Returns (ce_strike, pe_strike).
        Both ATM for straddle; ±200 pts for strangle.
        """
        atm = round(nifty_level / strike_step) * strike_step
        if strategy_type.upper() == "STRANGLE":
            return atm + _STRANGLE_OFFSET, atm - _STRANGLE_OFFSET
        return atm, atm  # straddle

    def enter_paper(
        self,
        index_name:    str,
        index_level:   float,
        vix:           float,
        gap_pct:       float,
        regime:        Optional[str],
        strategy_type: str = "STRADDLE",
        strike_step:   int = _STRIKE_STEP,
    ) -> Optional[StraddlePosition]:
        """
        Record a paper straddle entry. Returns the StraddlePosition or None if
        conditions are not met.
        """
        if not self.should_enter(index_name, index_level, vix, gap_pct, regime):
            return None

        ce_strike, pe_strike = self.get_strikes(index_level, strategy_type, strike_step)
        # ATM premium ≈ 0.8% of index for each leg (rough estimate)
        ce_prem = round(index_level * 0.008)
        pe_prem = round(index_level * 0.008)

        ce_leg = StraddleLeg("CE", ce_strike, ce_prem, index_level)
        pe_leg = StraddleLeg("PE", pe_strike, pe_prem, index_level)
        pos = StraddlePosition(index_name, strategy_type, ce_leg, pe_leg)

        self._positions.append(pos)
        self._triggered_today[index_name] = datetime.now().date()

        logger.info(
            f"📋 PAPER STRADDLE ENTRY: {index_name} {strategy_type} | "
            f"CE {ce_strike} @₹{ce_prem} | PE {pe_strike} @₹{pe_prem} | "
            f"Total cost ₹{ce_prem + pe_prem} | index={index_level:,.0f}"
        )
        return pos

    # ------------------------------------------------------------------
    # Leg management (called every analysis cycle)
    # ------------------------------------------------------------------

    def check_exits(self, index_prices: Dict[str, float]) -> None:
        """
        Evaluate each open straddle leg independently.
        Close legs that hit SL or target; close both at market close or combined stop.
        """
        now = datetime.now().time()
        market_close = dtime(15, 27)
        hard_exit    = dtime(13, 0)

        for pos in list(self._positions):
            if pos.is_fully_closed():
                self._finalize(pos)
                continue

            idx_price = index_prices.get(pos.index_name, 0.0)
            if idx_price <= 0:
                continue

            force_close = (now >= market_close)

            # ── CE leg ──
            if pos.ce_leg.status == "OPEN":
                ce_index_move = (idx_price - pos.ce_leg.index_entry) / pos.ce_leg.index_entry * 100
                ce_option_pct = max(-100.0, min(ce_index_move * 2.5, 300.0))  # ~2.5× leverage
                ce_exit_prem  = round(pos.ce_leg.premium_entry * (1 + ce_option_pct / 100))
                reason = self._leg_exit_reason(ce_option_pct, now, force_close)
                if reason:
                    pos.ce_leg.close(max(0, ce_exit_prem), reason)
                    logger.info(
                        f"📋 STRADDLE CE EXIT [{reason}]: {pos.index_name} CE {pos.ce_leg.strike} "
                        f"₹{pos.ce_leg.premium_entry} → ₹{max(0, ce_exit_prem)} "
                        f"({ce_option_pct:+.1f}%)"
                    )

            # ── PE leg ──
            if pos.pe_leg.status == "OPEN":
                pe_index_move = (pos.pe_leg.index_entry - idx_price) / pos.pe_leg.index_entry * 100
                pe_option_pct = max(-100.0, min(pe_index_move * 2.5, 300.0))
                pe_exit_prem  = round(pos.pe_leg.premium_entry * (1 + pe_option_pct / 100))
                reason = self._leg_exit_reason(pe_option_pct, now, force_close)
                if reason:
                    pos.pe_leg.close(max(0, pe_exit_prem), reason)
                    logger.info(
                        f"📋 STRADDLE PE EXIT [{reason}]: {pos.index_name} PE {pos.pe_leg.strike} "
                        f"₹{pos.pe_leg.premium_entry} → ₹{max(0, pe_exit_prem)} "
                        f"({pe_option_pct:+.1f}%)"
                    )

            # ── Combined stop: exit both if total loss is extreme ──
            ce_live = (idx_price - pos.ce_leg.index_entry) / pos.ce_leg.index_entry * 100 * 2.5
            pe_live = (pos.pe_leg.index_entry - idx_price) / pos.pe_leg.index_entry * 100 * 2.5
            combined_live = (
                pos.ce_leg.pnl_pct if pos.ce_leg.status == "CLOSED" else ce_live
                +
                pos.pe_leg.pnl_pct if pos.pe_leg.status == "CLOSED" else pe_live
            ) / 2
            if combined_live <= _COMBINED_STOP:
                if pos.ce_leg.status == "OPEN":
                    ce_exit = round(pos.ce_leg.premium_entry * (1 + max(-100, ce_live) / 100))
                    pos.ce_leg.close(max(0, ce_exit), "COMBINED_STOP")
                if pos.pe_leg.status == "OPEN":
                    pe_exit = round(pos.pe_leg.premium_entry * (1 + max(-100, pe_live) / 100))
                    pos.pe_leg.close(max(0, pe_exit), "COMBINED_STOP")
                logger.warning(
                    f"📋 STRADDLE COMBINED STOP HIT: {pos.index_name} | "
                    f"combined P&L {combined_live:.1f}%"
                )

            if pos.is_fully_closed():
                self._finalize(pos)

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------

    def get_open_positions(self) -> List[dict]:
        return [self._pos_to_dict(p) for p in self._positions]

    def get_summary(self) -> dict:
        all_closed = self._history
        total = len(all_closed)
        wins  = [p for p in all_closed if p.combined_pnl > 0]
        return {
            "total":      total,
            "open":       len(self._positions),
            "wins":       len(wins),
            "losses":     total - len(wins),
            "win_rate":   f"{len(wins)/total*100:.1f}%" if total else "0%",
            "total_pnl":  sum(p.combined_pnl for p in all_closed),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_window(now: dtime) -> bool:
        return _ENTRY_START <= now <= _ENTRY_END

    @staticmethod
    def _leg_exit_reason(pnl_pct: float, now: dtime, force_close: bool) -> Optional[str]:
        if force_close:
            return "MARKET_CLOSE"
        if pnl_pct <= -_LEG_SL_PCT:
            return "LEG_SL"
        if pnl_pct >= _LEG_TARGET_PCT:
            return "LEG_TARGET"
        if now >= dtime(13, 0):
            return "TIME_STOP_13:00"
        return None

    def _finalize(self, pos: StraddlePosition) -> None:
        pos.status = "CLOSED"
        self._positions.remove(pos)
        self._history.append(pos)
        result = "WIN ✅" if pos.combined_pnl > 0 else "LOSS ❌"
        logger.info(
            f"📋 STRADDLE CLOSED: {pos.index_name} {pos.strategy_type} | "
            f"CE P&L ₹{pos.ce_leg.pnl:+.0f} ({pos.ce_leg.pnl_pct:+.1f}%) | "
            f"PE P&L ₹{pos.pe_leg.pnl:+.0f} ({pos.pe_leg.pnl_pct:+.1f}%) | "
            f"Combined ₹{pos.combined_pnl:+.0f} ({pos.combined_pnl_pct:+.1f}%) {result}"
        )

    @staticmethod
    def _pos_to_dict(pos: StraddlePosition) -> dict:
        return {
            "index_name":    pos.index_name,
            "strategy_type": pos.strategy_type,
            "entry_time":    pos.entry_time.isoformat(),
            "total_cost":    pos.total_premium_entry,
            "combined_pnl":  round(pos.combined_pnl, 2),
            "combined_pnl_pct": round(pos.combined_pnl_pct, 2),
            "ce": {
                "strike":  pos.ce_leg.strike,
                "premium": pos.ce_leg.premium_entry,
                "pnl_pct": round(pos.ce_leg.pnl_pct, 1),
                "status":  pos.ce_leg.status,
            },
            "pe": {
                "strike":  pos.pe_leg.strike,
                "premium": pos.pe_leg.premium_entry,
                "pnl_pct": round(pos.pe_leg.pnl_pct, 1),
                "status":  pos.pe_leg.status,
            },
        }
