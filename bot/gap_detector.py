"""
Gap Detector

Detects gap-up / gap-down at market open (9:15 AM IST) by comparing
the opening price of the current session against the previous day's close.

Gap categories
──────────────
  STRONG_UP   : gap > strong_gap_pct  (e.g. >1.5%)  → Buy CE aggressively
  MODERATE_UP : gap > min_gap_pct     (e.g. >0.75%) → Confirm with ORB then buy CE
  NEUTRAL     : |gap| < min_gap_pct                  → Normal ORB / trend strategy
  MODERATE_DOWN: gap < -min_gap_pct                  → Confirm with ORB then buy PE
  STRONG_DOWN  : gap < -strong_gap_pct               → Buy PE aggressively

Strategy
────────
  Strong gap  → enter immediately (gap-and-go, higher confidence)
  Moderate gap→ wait for ORB confirmation in the right direction
  Neutral     → no special handling; regular strategies proceed normally

Chat commands:
  gap          — Show today's gap status
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, date
from dataclasses import dataclass
from typing import Optional
from enum import Enum
from loguru import logger

import sys
sys.path.append("..")
from config import settings
from bot.index_config import IndexConfig, NIFTY


class GapType(Enum):
    STRONG_UP    = "STRONG GAP UP"
    MODERATE_UP  = "MODERATE GAP UP"
    NEUTRAL      = "NEUTRAL"
    MODERATE_DOWN = "MODERATE GAP DOWN"
    STRONG_DOWN  = "STRONG GAP DOWN"


@dataclass
class GapAnalysis:
    gap_type: GapType
    prev_close: float
    open_price: float
    gap_points: float
    gap_pct: float
    trade_direction: str        # "CE", "PE", or "SKIP"
    wait_for_confirmation: bool # True = wait for ORB, False = enter now
    note: str
    timestamp: datetime


class GapDetector:
    """
    Detects opening gaps and recommends an option direction.

    Integrates with the engine's analysis loop — called once per trading day
    shortly after 9:15 AM IST.
    """

    def __init__(self, index_config: IndexConfig = None):
        self._index = index_config or NIFTY
        self._today_analysis: Optional[GapAnalysis] = None
        self._last_date: Optional[date] = None

    def set_index(self, idx: IndexConfig):
        self._index = idx

    # ──────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────

    def analyze(self) -> Optional[GapAnalysis]:
        """
        Fetch today's open and previous close, classify the gap.
        Returns cached result if already computed today.
        """
        today = date.today()
        if self._last_date == today and self._today_analysis:
            return self._today_analysis

        try:
            ticker = yf.Ticker(self._index.yahoo_symbol)
            # Fetch last 5 days of 1-day candles to get prev close reliably
            hist = ticker.history(period="5d", interval="1d")
            if hist.empty or len(hist) < 2:
                logger.warning("Gap detector: insufficient daily data")
                return None

            prev_close = float(hist["Close"].iloc[-2])

            # Fetch today's 1-minute data for the actual open
            intraday = ticker.history(period="1d", interval="1m")
            if intraday.empty:
                logger.warning("Gap detector: no intraday data")
                return None

            open_price = float(intraday["Open"].iloc[0])

        except Exception as e:
            logger.error(f"Gap detector data fetch failed: {e}")
            return None

        cfg = settings.gap
        gap_points = open_price - prev_close
        gap_pct = (gap_points / prev_close) * 100

        strong_pct = cfg.strong_gap_pct
        moderate_pct = cfg.min_gap_pct

        if gap_pct >= strong_pct:
            gap_type = GapType.STRONG_UP
            trade_direction = "CE"
            wait_for_confirmation = False
            note = (
                f"Strong gap-up of {gap_pct:.2f}% ({gap_points:+.1f} pts). "
                f"High-confidence bullish — enter CE immediately."
            )
        elif gap_pct >= moderate_pct:
            gap_type = GapType.MODERATE_UP
            trade_direction = "CE"
            wait_for_confirmation = True
            note = (
                f"Moderate gap-up of {gap_pct:.2f}%. "
                f"Wait for ORB confirmation before buying CE."
            )
        elif gap_pct <= -strong_pct:
            gap_type = GapType.STRONG_DOWN
            trade_direction = "PE"
            wait_for_confirmation = False
            note = (
                f"Strong gap-down of {gap_pct:.2f}% ({gap_points:+.1f} pts). "
                f"High-confidence bearish — enter PE immediately."
            )
        elif gap_pct <= -moderate_pct:
            gap_type = GapType.MODERATE_DOWN
            trade_direction = "PE"
            wait_for_confirmation = True
            note = (
                f"Moderate gap-down of {gap_pct:.2f}%. "
                f"Wait for ORB confirmation before buying PE."
            )
        else:
            gap_type = GapType.NEUTRAL
            trade_direction = "SKIP"
            wait_for_confirmation = False
            note = (
                f"No significant gap ({gap_pct:+.2f}%). "
                f"Proceeding with normal ORB / trend strategy."
            )

        analysis = GapAnalysis(
            gap_type=gap_type,
            prev_close=prev_close,
            open_price=open_price,
            gap_points=gap_points,
            gap_pct=gap_pct,
            trade_direction=trade_direction,
            wait_for_confirmation=wait_for_confirmation,
            note=note,
            timestamp=datetime.now(),
        )

        self._today_analysis = analysis
        self._last_date = today
        logger.info(
            f"Gap detected: {gap_type.value} | {gap_pct:+.2f}% | "
            f"prev={prev_close:.1f} open={open_price:.1f}"
        )
        return analysis

    def format_report(self, analysis: GapAnalysis) -> str:
        """Return a human-readable gap report for the chat UI."""
        emoji = {
            GapType.STRONG_UP:    "🚀",
            GapType.MODERATE_UP:  "📈",
            GapType.NEUTRAL:      "➡️",
            GapType.MODERATE_DOWN:"📉",
            GapType.STRONG_DOWN:  "💥",
        }.get(analysis.gap_type, "")

        action = (
            "Wait for ORB confirmation" if analysis.wait_for_confirmation
            else ("Enter immediately" if analysis.trade_direction != "SKIP" else "No gap trade")
        )

        lines = [
            f"{emoji} **Gap Analysis — {self._index.display_name}**",
            f"  Previous Close : ₹{analysis.prev_close:,.2f}",
            f"  Today's Open   : ₹{analysis.open_price:,.2f}",
            f"  Gap            : {analysis.gap_pct:+.2f}%  ({analysis.gap_points:+.1f} pts)",
            f"  Gap Type       : {analysis.gap_type.value}",
            f"  Direction      : {analysis.trade_direction}",
            f"  Action         : {action}",
            f"  Note           : {analysis.note}",
        ]
        return "\n".join(lines)

    def is_strong_gap(self) -> bool:
        a = self._today_analysis
        return a is not None and a.gap_type in (GapType.STRONG_UP, GapType.STRONG_DOWN)

    def should_trade_immediately(self) -> bool:
        a = self._today_analysis
        return (
            a is not None
            and a.trade_direction != "SKIP"
            and not a.wait_for_confirmation
        )


# Singleton for use across the bot
gap_detector = GapDetector()
