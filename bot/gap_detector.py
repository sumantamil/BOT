"""
Gap Detector — complete gap trading system.

Gap categories (legacy, kept for engine backward compat)
─────────────────────────────────────────────────────────
  STRONG_UP   : gap > strong_gap_pct  (>1.5%)  → CE aggressively
  MODERATE_UP : gap > min_gap_pct     (>0.75%) → wait for ORB confirmation
  NEUTRAL     : |gap| < min_gap_pct             → normal ORB/trend strategies
  MODERATE_DOWN: gap < -min_gap_pct             → wait for ORB confirmation
  STRONG_DOWN  : gap < -strong_gap_pct          → PE aggressively

Enhanced gap categories (used by GapPlaybook)
──────────────────────────────────────────────
  COMMON     : 0.3 – 0.8%   → skip or very small position
  BREAKAWAY  : 0.8 – 1.5%   → continuation trade (trend direction)
  RUNAWAY    : 1.5 – 2.5%   → continuation trade (strong momentum)
  EXHAUSTION : > 2.5%        → fade trade (bet on gap fill)

Strategies
──────────
  GapFadeStrategy        — bet the gap fills (exhaustion / weak volume)
  GapContinuationStrategy — bet the gap extends (breakaway / high volume)
  GapPlaybook            — auto-selects fade vs continuation based on context
  AdvancedGapAnalyzer    — S/R context, fill probability, gap sequences

Chat command:  gap  — show today's gap status
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum
from loguru import logger

import sys
sys.path.append("..")
from config import settings
from bot.index_config import IndexConfig, NIFTY


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class GapType(Enum):
    """Legacy gap classification — kept for engine backward compat."""
    STRONG_UP     = "STRONG GAP UP"
    MODERATE_UP   = "MODERATE GAP UP"
    NEUTRAL       = "NEUTRAL"
    MODERATE_DOWN = "MODERATE GAP DOWN"
    STRONG_DOWN   = "STRONG GAP DOWN"


class GapCategory(Enum):
    """Enhanced 4-tier classification used by GapPlaybook."""
    COMMON     = "COMMON"      # 0.3 – 0.8 %
    BREAKAWAY  = "BREAKAWAY"   # 0.8 – 1.5 %
    RUNAWAY    = "RUNAWAY"     # 1.5 – 2.5 %
    EXHAUSTION = "EXHAUSTION"  # > 2.5 %
    NONE       = "NONE"        # below min threshold


class GapStrength(Enum):
    WEAK    = "WEAK"
    MEDIUM  = "MEDIUM"
    STRONG  = "STRONG"
    EXTREME = "EXTREME"


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GapAnalysis:
    """Legacy result object — returned by GapDetector.analyze()."""
    gap_type: GapType
    prev_close: float
    open_price: float
    gap_points: float
    gap_pct: float
    trade_direction: str        # "CE", "PE", or "SKIP"
    wait_for_confirmation: bool # True = wait for ORB, False = enter now
    note: str
    timestamp: datetime


@dataclass
class GapInfo:
    """Rich gap descriptor used by GapFadeStrategy / GapContinuationStrategy / GapPlaybook."""
    gap_pct: float
    gap_points: float
    direction: str          # "UP" or "DOWN"
    category: GapCategory
    strength: GapStrength
    prev_close: float
    open_price: float
    timestamp: datetime = field(default_factory=datetime.now)



# ─────────────────────────────────────────────────────────────────────────────
# Module-level helper functions (usable without instantiating classes)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_gap_size(today_open: float, yesterday_close: float) -> dict:
    """Calculate gap size, direction, category and strength.

    Returns:
        {
            'gap_pct': 1.2,
            'gap_points': 280.0,
            'direction': 'UP',      # 'UP' | 'DOWN'
            'category': 'BREAKAWAY',# 'NONE' | 'COMMON' | 'BREAKAWAY' | 'RUNAWAY' | 'EXHAUSTION'
            'strength': 'STRONG',   # 'WEAK' | 'MEDIUM' | 'STRONG' | 'EXTREME'
        }
    """
    if yesterday_close <= 0:
        return {
            "gap_pct": 0.0, "gap_points": 0.0,
            "direction": "UP", "category": "NONE", "strength": "WEAK",
        }
    gap_points = today_open - yesterday_close
    gap_pct    = (gap_points / yesterday_close) * 100
    abs_pct    = abs(gap_pct)
    direction  = "UP" if gap_pct >= 0 else "DOWN"
    cat, strength = classify_gap(abs_pct)
    return {
        "gap_pct":    round(gap_pct,    4),
        "gap_points": round(gap_points, 2),
        "direction":  direction,
        "category":   cat.value,
        "strength":   strength.value,
    }


def classify_gap(gap_pct: float) -> tuple:
    """Classify a gap magnitude into (GapCategory, GapStrength).

    Thresholds (configurable via GapConfig):
        < 0.3%  → NONE   / WEAK
        0.3–0.8%  → COMMON    / WEAK or MEDIUM
        0.8–1.5%  → BREAKAWAY / MEDIUM or STRONG
        1.5–2.5%  → RUNAWAY   / STRONG
        > 2.5%  → EXHAUSTION / EXTREME
    """
    cfg = settings.gap
    common_t    = getattr(cfg, "common_threshold",    0.3)
    breakaway_t = getattr(cfg, "breakaway_threshold", 0.8)
    runaway_t   = getattr(cfg, "runaway_threshold",   1.5)
    exhaust_t   = getattr(cfg, "exhaustion_threshold", 2.5)

    abs_pct = abs(gap_pct)
    if abs_pct < common_t:
        return GapCategory.NONE, GapStrength.WEAK
    if abs_pct < breakaway_t:
        strength = GapStrength.WEAK if abs_pct < (common_t + breakaway_t) / 2 else GapStrength.MEDIUM
        return GapCategory.COMMON, strength
    if abs_pct < runaway_t:
        strength = GapStrength.MEDIUM if abs_pct < (breakaway_t + runaway_t) / 2 else GapStrength.STRONG
        return GapCategory.BREAKAWAY, strength
    if abs_pct < exhaust_t:
        return GapCategory.RUNAWAY, GapStrength.STRONG
    return GapCategory.EXHAUSTION, GapStrength.EXTREME


def get_gap_signal(
    gap_info: dict,
    current_price: float,
    time_since_open: int,
) -> Optional[dict]:
    """Generate a trading signal from gap information.

    Args:
        gap_info:         Output of calculate_gap_size()
        current_price:    Latest index price
        time_since_open:  Minutes since 9:15 AM open

    Returns signal dict or None when no trade is warranted.
    """
    cat       = gap_info.get("category", "NONE")
    direction = gap_info.get("direction", "UP")
    gap_pct   = abs(gap_info.get("gap_pct", 0.0))

    if cat == "NONE" or cat == "COMMON":
        return None

    cfg = settings.gap

    if cat == "EXHAUSTION":
        # Fade: bet on gap fill
        opt_type   = "PE" if direction == "UP" else "CE"
        strategy   = "GAP_FADE"
        confidence = min(90, 60 + int((gap_pct - 2.5) * 8))
        prev_close = gap_info.get("prev_close", current_price)
        target     = prev_close
        sl_pts     = abs(current_price - gap_info.get("open_price", current_price)) * 1.1
        stop_loss  = current_price + sl_pts if direction == "UP" else current_price - sl_pts
        reasoning  = f"Exhaustion gap ({gap_pct:.1f}%) — high probability fill to {target:,.0f}"
    else:
        # BREAKAWAY / RUNAWAY: ride continuation
        opt_type   = "CE" if direction == "UP" else "PE"
        strategy   = "GAP_CONTINUATION"
        confidence = min(85, 50 + int(gap_pct * 12))
        open_price = gap_info.get("open_price", current_price)
        gap_pts    = abs(gap_info.get("gap_points", 0.0))
        target     = current_price + gap_pts if direction == "UP" else current_price - gap_pts
        prev_close = gap_info.get("prev_close", current_price)
        stop_loss  = prev_close
        reasoning  = f"{cat.title()} gap — continuation expected, target = {target:,.0f}"

    # Entry timing guard
    max_entry = getattr(cfg, "fade_max_entry_time", 35) if strategy == "GAP_FADE" else 75
    if time_since_open > max_entry:
        logger.debug(f"get_gap_signal: time_since_open={time_since_open} > max_entry={max_entry} — skipping")
        return None

    return {
        "direction":   opt_type,
        "strategy":    strategy,
        "confidence":  confidence,
        "entry_price": current_price,
        "stop_loss":   round(stop_loss, 1),
        "target":      round(target,    1),
        "reasoning":   reasoning,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Legacy GapDetector (kept for engine backward compat)
# ─────────────────────────────────────────────────────────────────────────────

class GapDetector:
    """Detects opening gaps and recommends an option direction.

    Called once per trading day shortly after 9:15 AM IST.
    Also exposes rich GapInfo for use by GapPlaybook.
    """

    def __init__(self, index_config: IndexConfig = None):
        self._index = index_config or NIFTY
        self._cached_by_index: dict = {}   # {index_name: GapAnalysis}
        self._rich_by_index:   dict = {}   # {index_name: GapInfo}
        self._last_date: Optional[date] = None

    def set_index(self, idx: IndexConfig):
        self._index = idx

    # ─────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────

    def analyze(self) -> Optional[GapAnalysis]:
        """Fetch today's open and previous close, classify the gap.
        Returns cached result if already computed today."""
        today = date.today()
        if self._last_date != today:
            self._cached_by_index = {}
            self._rich_by_index   = {}
            self._last_date = today
        if self._index.name in self._cached_by_index:
            return self._cached_by_index[self._index.name]

        try:
            ticker = yf.Ticker(self._index.yahoo_symbol)
            hist   = ticker.history(period="5d", interval="1d")
            if hist.empty or len(hist) < 2:
                logger.warning("Gap detector: insufficient daily data")
                return None
            prev_close = float(hist["Close"].iloc[-2])

            intraday = ticker.history(period="1d", interval="1m")
            if intraday.empty:
                logger.warning("Gap detector: no intraday data")
                return None
            open_price = float(intraday["Open"].iloc[0])
        except Exception as e:
            logger.error(f"Gap detector data fetch failed: {e}")
            return None

        info     = calculate_gap_size(open_price, prev_close)
        gap_pct  = info["gap_pct"]
        gap_pts  = info["gap_points"]
        cfg      = settings.gap
        strong_pct   = cfg.strong_gap_pct
        moderate_pct = cfg.min_gap_pct

        if gap_pct >= strong_pct:
            gap_type = GapType.STRONG_UP
            trade_direction = "CE"
            wait_for_confirmation = False
            note = (
                f"Strong gap-up of {gap_pct:.2f}% ({gap_pts:+.1f} pts). "
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
                f"Strong gap-down of {gap_pct:.2f}% ({gap_pts:+.1f} pts). "
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
            gap_points=gap_pts,
            gap_pct=gap_pct,
            trade_direction=trade_direction,
            wait_for_confirmation=wait_for_confirmation,
            note=note,
            timestamp=datetime.now(),
        )
        cat, strength = classify_gap(abs(gap_pct))
        self._rich_by_index[self._index.name] = GapInfo(
            gap_pct=gap_pct,
            gap_points=gap_pts,
            direction=info["direction"],
            category=cat,
            strength=strength,
            prev_close=prev_close,
            open_price=open_price,
        )
        self._cached_by_index[self._index.name] = analysis
        logger.info(
            f"Gap detected: {gap_type.value} | {gap_pct:+.2f}% | "
            f"prev={prev_close:.1f} open={open_price:.1f}"
        )
        return analysis

    def get_gap_info(self) -> Optional[GapInfo]:
        """Return rich GapInfo for use by GapPlaybook (call after analyze())."""
        return self._rich_by_index.get(self._index.name)

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
        a = self._cached_by_index.get(self._index.name)
        return a is not None and a.gap_type in (GapType.STRONG_UP, GapType.STRONG_DOWN)

    def should_trade_immediately(self) -> bool:
        a = self._cached_by_index.get(self._index.name)
        return (
            a is not None
            and a.trade_direction != "SKIP"
            and not a.wait_for_confirmation
        )


# ─────────────────────────────────────────────────────────────────────────────
# Gap Fade Strategy
# ─────────────────────────────────────────────────────────────────────────────

class GapFadeStrategy:
    """Trade gap fills — bet the gap will be erased (price returns to prev close).

    Best cases:
    • Exhaustion gaps (> 2.5%) — over-extended overnight move
    • Low-volume gaps (volume ratio < 0.8) — weak institutional conviction
    • Gap into strong S/R level — natural reversal zone
    """

    def __init__(self):
        cfg = settings.gap
        self.min_gap_pct            = getattr(cfg, "fade_min_size",            1.5)
        self.max_gap_pct            = getattr(cfg, "max_size_pct",             3.0)
        self.entry_delay_minutes    = getattr(cfg, "fade_entry_delay",         5)
        self.max_entry_time         = getattr(cfg, "fade_max_entry_time",      35)
        self.require_confirmation   = getattr(cfg, "fade_require_confirmation", True)

    # ── Main decision ─────────────────────────────────────────────────────────

    def should_fade_gap(self, gap_info: dict, market_data: dict) -> bool:
        """Return True if this gap satisfies fade entry conditions.

        Args:
            gap_info:    Output of calculate_gap_size()
            market_data: {current_price, open_price, high_15min, low_15min,
                          volume_ratio, time_since_open}
        """
        abs_pct          = abs(gap_info.get("gap_pct", 0))
        time_since_open  = market_data.get("time_since_open", 0)
        volume_ratio     = market_data.get("volume_ratio", 1.0)
        cat              = gap_info.get("category", "NONE")

        # Size guard
        if abs_pct < self.min_gap_pct:
            logger.debug(f"fade: gap {abs_pct:.2f}% < min {self.min_gap_pct}% — skip")
            return False
        if abs_pct > self.max_gap_pct:
            logger.debug(f"fade: gap {abs_pct:.2f}% > max {self.max_gap_pct}% — skip (extreme)")
            return False

        # Only fade exhaustion or weak-volume breakaway
        if cat not in ("EXHAUSTION", "RUNAWAY"):
            if not (cat == "BREAKAWAY" and volume_ratio < 0.8):
                logger.debug(f"fade: category {cat} not faded at this volume")
                return False

        # Timing guards
        if time_since_open < self.entry_delay_minutes:
            logger.debug(f"fade: too early ({time_since_open} min < {self.entry_delay_minutes} min delay)")
            return False
        if time_since_open > self.max_entry_time:
            logger.debug(f"fade: entry window expired ({time_since_open} min > {self.max_entry_time} min)")
            return False

        # Reversal confirmation — price must have already started moving back
        if self.require_confirmation:
            cur   = market_data.get("current_price", 0)
            open_ = market_data.get("open_price", 0)
            if open_ <= 0:
                return False
            direction = gap_info.get("direction", "UP")
            if direction == "UP" and cur >= open_:
                logger.debug("fade: NO confirmation — price still at/above open")
                return False
            if direction == "DOWN" and cur <= open_:
                logger.debug("fade: NO confirmation — price still at/below open")
                return False

        return True

    def generate_fade_signal(self, gap_info: dict, market_data: dict) -> Optional[dict]:
        """Return a fade signal dict or None."""
        if not self.should_fade_gap(gap_info, market_data):
            return None

        direction = gap_info.get("direction", "UP")
        opt_type  = "PE" if direction == "UP" else "CE"
        abs_pct   = abs(gap_info.get("gap_pct", 0))
        confidence = min(90, 55 + int(abs_pct * 10))

        cur      = market_data.get("current_price", 0)
        h15      = market_data.get("high_15min",    cur * 1.003)
        l15      = market_data.get("low_15min",     cur * 0.997)
        targets  = self.calculate_fade_targets(gap_info, market_data)

        stop_loss = h15 if direction == "UP" else l15

        logger.info(
            f"GapFadeStrategy: {opt_type} signal | gap={abs_pct:.2f}% | "
            f"confidence={confidence}% | SL={stop_loss:.0f}"
        )
        return {
            "direction":   opt_type,
            "strategy":    "GAP_FADE",
            "confidence":  confidence,
            "entry_price": cur,
            "stop_loss":   round(stop_loss, 1),
            "target_25":   targets["target_25"],
            "target_50":   targets["target_50"],
            "target_full": targets["target_full"],
            "reasoning":   (
                f"{gap_info.get('category','').title()} gap {abs_pct:.1f}% — "
                f"fade to prev close {targets['target_full']:,.0f}"
            ),
        }

    def calculate_fade_targets(self, gap_info: dict, market_data: dict) -> dict:
        """Calculate tiered fade targets: 25% / 50% / full fill."""
        prev_close = gap_info.get("prev_close", 0)
        open_price = gap_info.get("open_price", market_data.get("open_price", 0))
        gap_pts    = abs(open_price - prev_close) if (open_price and prev_close) else 0
        cur        = market_data.get("current_price", open_price)
        direction  = gap_info.get("direction", "UP")

        if direction == "UP":
            t25  = cur  - gap_pts * 0.25
            t50  = cur  - gap_pts * 0.50
            full = prev_close
        else:
            t25  = cur  + gap_pts * 0.25
            t50  = cur  + gap_pts * 0.50
            full = prev_close

        return {
            "target_25":   round(t25,  1),
            "target_50":   round(t50,  1),
            "target_full": round(full, 1),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Gap Continuation Strategy
# ─────────────────────────────────────────────────────────────────────────────

class GapContinuationStrategy:
    """Trade gap extensions — bet the gap will widen further.

    Best cases:
    • Breakaway gaps (0.8–1.5%) with trend alignment
    • High-volume gaps (volume ratio > 1.2) — institutional conviction
    • Gap after consolidation — new trend beginning
    """

    def __init__(self):
        cfg = settings.gap
        self.min_gap_pct           = getattr(cfg, "continuation_min_size",      0.8)
        self.max_gap_pct           = getattr(cfg, "continuation_max_size",      2.5)
        self.min_pullback_pct      = getattr(cfg, "continuation_min_pullback",  0.2)
        self.gap_hold_threshold    = getattr(cfg, "continuation_gap_hold_pct",  0.8)
        self.require_pullback      = getattr(cfg, "continuation_require_pullback", True)

    # ── Main decision ─────────────────────────────────────────────────────────

    def should_trade_continuation(
        self,
        gap_info: dict,
        market_data: dict,
        trend_info: Optional[dict] = None,
    ) -> bool:
        """Return True if gap continuation conditions are met."""
        abs_pct      = abs(gap_info.get("gap_pct", 0))
        cat          = gap_info.get("category", "NONE")
        volume_ratio = market_data.get("volume_ratio", 1.0)

        if abs_pct < self.min_gap_pct or abs_pct > self.max_gap_pct:
            return False
        if cat not in ("BREAKAWAY", "RUNAWAY"):
            return False

        # Trend alignment — counter-trend gaps are not good for continuation
        if trend_info:
            direction    = gap_info.get("direction", "UP")
            trend_dir    = trend_info.get("direction", "NEUTRAL")
            if direction == "UP"   and trend_dir == "BEARISH":
                logger.debug("continuation: gap UP but trend BEARISH — skip")
                return False
            if direction == "DOWN" and trend_dir == "BULLISH":
                logger.debug("continuation: gap DOWN but trend BULLISH — skip")
                return False

        # Volume confirmation — weak-volume gaps don't continue
        if volume_ratio < 0.8:
            logger.debug(f"continuation: volume_ratio={volume_ratio:.2f} too low — skip")
            return False

        # Gap hold check — price should still be away from prev close
        cur        = market_data.get("current_price", 0)
        prev_close = gap_info.get("prev_close", 0)
        open_price = gap_info.get("open_price", cur)
        if prev_close > 0 and open_price != prev_close:
            gap_pts     = abs(open_price - prev_close)
            held_pts    = abs(cur - prev_close)
            hold_ratio  = held_pts / gap_pts if gap_pts > 0 else 0
            if hold_ratio < self.gap_hold_threshold:
                logger.debug(
                    f"continuation: gap only {hold_ratio:.0%} held "
                    f"(need {self.gap_hold_threshold:.0%}) — may be filling"
                )
                return False

        return True

    def detect_pullback_entry(
        self,
        gap_info: dict,
        candles: "pd.DataFrame",
    ) -> Optional[dict]:
        """Detect optimal pullback entry after a gap.

        Returns entry signal dict or None if no valid pullback found.
        """
        if candles is None or len(candles) < 2:
            return None

        direction  = gap_info.get("direction", "UP")
        prev_close = gap_info.get("prev_close", 0)
        open_price = gap_info.get("open_price", float(candles["Open"].iloc[0]))
        gap_pts    = abs(open_price - prev_close)
        min_pb_pts = gap_pts * self.min_pullback_pct / 100

        closes = candles["Close"].values
        highs  = candles["High"].values
        lows   = candles["Low"].values

        for i in range(1, len(closes)):
            cur = closes[i]
            if direction == "UP":
                # Look for dip that holds above prev_close (gap not filled)
                if cur < prev_close:
                    break  # gap filled — continuation invalid
                pullback_from_open = open_price - cur
                if pullback_from_open >= min_pb_pts and closes[i] > lows[i - 1]:
                    return {
                        "candle_index": i,
                        "entry_price":  float(cur),
                        "pullback_pts": float(pullback_from_open),
                        "direction":    "CE",
                        "gap_hold":     True,
                    }
            else:
                if cur > prev_close:
                    break
                pullback_from_open = cur - open_price
                if pullback_from_open >= min_pb_pts and closes[i] < highs[i - 1]:
                    return {
                        "candle_index": i,
                        "entry_price":  float(cur),
                        "pullback_pts": float(pullback_from_open),
                        "direction":    "PE",
                        "gap_hold":     True,
                    }
        return None

    def generate_continuation_signal(
        self,
        gap_info: dict,
        market_data: dict,
        trend_info: Optional[dict] = None,
    ) -> Optional[dict]:
        """Return a continuation signal dict or None."""
        if not self.should_trade_continuation(gap_info, market_data, trend_info):
            return None

        direction  = gap_info.get("direction", "UP")
        opt_type   = "CE" if direction == "UP" else "PE"
        abs_pct    = abs(gap_info.get("gap_pct", 0))
        confidence = min(85, 45 + int(abs_pct * 12))
        cur        = market_data.get("current_price", 0)
        targets    = self.calculate_continuation_targets(gap_info, cur)

        logger.info(
            f"GapContinuationStrategy: {opt_type} | gap={abs_pct:.2f}% | "
            f"confidence={confidence}% | SL={targets['stop_loss']:.0f}"
        )
        return {
            "direction":   opt_type,
            "strategy":    "GAP_CONTINUATION",
            "confidence":  confidence,
            "entry_price": cur,
            "stop_loss":   targets["stop_loss"],
            "target_1x":   targets["target_1x"],
            "target_2x":   targets["target_2x"],
            "trail_stop":  True,
            "reasoning":   (
                f"{gap_info.get('category','').title()} gap {abs_pct:.1f}% "
                f"— continuation to {targets['target_1x']:,.0f}"
            ),
        }

    def calculate_continuation_targets(self, gap_info: dict, entry_price: float) -> dict:
        """Calculate targets based on gap-extension logic (1× and 2× gap size)."""
        direction  = gap_info.get("direction", "UP")
        prev_close = gap_info.get("prev_close", entry_price)
        open_price = gap_info.get("open_price", entry_price)
        gap_pts    = abs(open_price - prev_close)
        stop_loss  = prev_close  # below/above prev_close = strategy failure

        if direction == "UP":
            t1x = entry_price + gap_pts * 1.0
            t2x = entry_price + gap_pts * 2.0
        else:
            t1x = entry_price - gap_pts * 1.0
            t2x = entry_price - gap_pts * 2.0

        return {
            "entry":     round(entry_price, 1),
            "stop_loss": round(stop_loss,   1),
            "target_1x": round(t1x,         1),
            "target_2x": round(t2x,         1),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Advanced Gap Analyzer
# ─────────────────────────────────────────────────────────────────────────────

class AdvancedGapAnalyzer:
    """Context-enriched gap analysis — S/R, fill probability, gap sequence."""

    # A gap is considered "large" above 2.5% — often reversal territory
    EXHAUSTION_LEVEL = 2.5

    def __init__(self, historical_data: "pd.DataFrame"):
        self.data              = historical_data
        self._sr_levels: list  = []
        if not historical_data.empty:
            try:
                self._sr_levels = self._calculate_sr_levels()
            except Exception as e:
                logger.debug(f"AdvancedGapAnalyzer: S/R calc failed: {e}")

    def analyze_gap_context(self, gap_info: dict, current_price: float) -> dict:
        """Return enriched context for a gap."""
        abs_pct = abs(gap_info.get("gap_pct", 0))
        seq     = self.detect_gap_sequence(gap_info)
        vol_p   = gap_info.get("volume_profile", "NORMAL")

        near_sr, sr_dist = self._nearest_sr(current_price)
        at_extreme       = self._at_extreme(current_price)

        fill_prob  = self.calculate_fill_probability(gap_info, {
            "near_sr": near_sr, "volume_profile": vol_p,
            "gap_sequence": seq, "at_extreme": at_extreme,
        })
        cont_prob = 100 - fill_prob

        trend_align = "WITH"
        if not self.data.empty and len(self.data) >= 20:
            trend = self._simple_trend()
            direction = gap_info.get("direction", "UP")
            if (direction == "UP" and trend == "DOWN") or (direction == "DOWN" and trend == "UP"):
                trend_align = "AGAINST"

        return {
            "near_sr":                  near_sr,
            "sr_distance":              round(sr_dist, 1),
            "trend_alignment":          trend_align,
            "at_extreme":               at_extreme,
            "gap_sequence":             seq,
            "volume_profile":           vol_p,
            "fill_probability":         round(fill_prob, 1),
            "continuation_probability": round(cont_prob, 1),
        }

    def calculate_fill_probability(self, gap_info: dict, context: dict) -> float:
        """Estimate probability (0–100) that gap will fill within the session."""
        abs_pct  = abs(gap_info.get("gap_pct", 0))
        category = gap_info.get("category", "NONE")

        # Base probability by category
        base = {
            "COMMON": 75.0, "BREAKAWAY": 55.0,
            "RUNAWAY": 40.0, "EXHAUSTION": 70.0,
        }.get(category, 50.0)

        # Volume adjustment — strong institutional volume = less likely to fill
        vol = context.get("volume_profile", "NORMAL")
        if vol == "HIGH":
            base -= 15
        elif vol == "LOW":
            base += 15

        # Trend alignment
        if context.get("trend_alignment") == "AGAINST":
            base += 10
        else:
            base -= 5

        # Near S/R level → more likely to fill
        if context.get("near_sr"):
            base += 10

        # Gap sequence
        seq = context.get("gap_sequence", "SINGLE")
        if seq == "RUNAWAY":
            base -= 10  # runaway sequence = high momentum, won't fill easily
        elif seq == "CLUSTER":
            base += 5   # cluster of small gaps = choppiness, tend to fill

        return min(95.0, max(5.0, base))

    def detect_gap_sequence(self, current_gap: dict, lookback_days: int = 10) -> str:
        """Classify this gap as part of a pattern: SINGLE, SERIES, RUNAWAY, CLUSTER."""
        if self.data.empty or len(self.data) < 3:
            return "SINGLE"

        df = self.data.tail(lookback_days + 1).copy()
        df["prev_close"] = df["Close"].shift(1)
        df["gap_pct"]    = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100
        df               = df.dropna(subset=["prev_close"])

        recent_gaps = df["gap_pct"].values[:-1]  # exclude today
        direction   = current_gap.get("direction", "UP")
        sign        = 1 if direction == "UP" else -1
        same_dir    = [g for g in recent_gaps if sign * g > 0.3]

        if len(same_dir) >= 4:
            return "RUNAWAY"
        if len(same_dir) >= 2:
            return "SERIES"
        small_gaps = [g for g in recent_gaps if abs(g) > 0.1]
        if len(small_gaps) >= 4:
            return "CLUSTER"
        return "SINGLE"

    def get_historical_gap_stats(self, gap_type: str) -> dict:
        """Return aggregate stats for gaps of the same category in history."""
        if self.data.empty or len(self.data) < 10:
            return {"sample_size": 0, "fill_rate": 60.0, "avg_fill_time": 90, "avg_extension": 0.3}

        df = self.data.copy()
        df["prev_close"] = df["Close"].shift(1)
        df["gap_pct"]    = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100
        df               = df.dropna(subset=["prev_close"])

        cat_filter = {
            "COMMON":    (0.3, 0.8),
            "BREAKAWAY": (0.8, 1.5),
            "RUNAWAY":   (1.5, 2.5),
            "EXHAUSTION":(2.5, 999),
        }.get(gap_type, (0, 999))

        mask     = (df["gap_pct"].abs() >= cat_filter[0]) & (df["gap_pct"].abs() < cat_filter[1])
        subset   = df[mask]
        n        = len(subset)

        if n == 0:
            return {"sample_size": 0, "fill_rate": 50.0, "avg_fill_time": 120, "avg_extension": 0.3}

        # Approximate fill via same-day high/low touching prev_close
        fills = 0
        for _, row in subset.iterrows():
            pc = row["prev_close"]
            if row["gap_pct"] > 0:            # gap-up → fill if low ≤ prev_close
                if row["Low"] <= pc:
                    fills += 1
            else:                              # gap-down → fill if high ≥ prev_close
                if row["High"] >= pc:
                    fills += 1

        fill_rate    = fills / n * 100
        avg_ext      = float(subset["gap_pct"].abs().mean())

        return {
            "sample_size":        n,
            "fill_rate":          round(fill_rate,  1),
            "avg_fill_time":      90,   # rough estimate (intraday data needed for exact)
            "avg_extension":      round(avg_ext, 3),
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _calculate_sr_levels(self) -> list:
        """Identify support/resistance levels as local pivot highs/lows."""
        if self.data.empty or len(self.data) < 5:
            return []
        highs  = self.data["High"].values
        lows   = self.data["Low"].values
        levels = []
        for i in range(2, len(highs) - 2):
            if highs[i] >= max(highs[i-2:i]) and highs[i] >= max(highs[i+1:i+3]):
                levels.append(("R", float(highs[i])))
            if lows[i]  <= min(lows[i-2:i])  and lows[i]  <= min(lows[i+1:i+3]):
                levels.append(("S", float(lows[i])))
        return levels

    def _nearest_sr(self, price: float) -> tuple:
        """Return (near_sr: bool, distance_pts: float) to nearest S/R level."""
        if not self._sr_levels:
            return False, 999.0
        distances = [abs(price - lvl[1]) for lvl in self._sr_levels]
        min_dist  = min(distances)
        threshold = price * 0.005   # within 0.5% = "near"
        return (min_dist <= threshold), min_dist

    def _at_extreme(self, price: float) -> bool:
        """Return True if price is near 52-week high or low."""
        if self.data.empty:
            return False
        hi52 = float(self.data["High"].max())
        lo52 = float(self.data["Low"].min())
        return (price >= hi52 * 0.99) or (price <= lo52 * 1.01)

    def _simple_trend(self) -> str:
        """Rough 20-day trend direction."""
        if len(self.data) < 20:
            return "NEUTRAL"
        sma20  = float(self.data["Close"].tail(20).mean())
        latest = float(self.data["Close"].iloc[-1])
        if latest > sma20 * 1.005:
            return "UP"
        if latest < sma20 * 0.995:
            return "DOWN"
        return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# Gap Playbook — automated strategy selector
# ─────────────────────────────────────────────────────────────────────────────

class GapPlaybook:
    """Auto-select between fade, continuation, and skip based on gap context.

    Decision algorithm
    ──────────────────
    • Score each approach 0–100 independently
    • Whichever score is higher wins (if both below 50 → SKIP)
    • Scores incorporate: gap size, volume, trend alignment, S/R proximity,
      exhaustion classification, historical win-rates

    Integration in engine._check_gap_signal():
        playbook = GapPlaybook()
        rec = playbook.select_strategy(gap_info_dict, context_dict)
        if rec['strategy'] == 'FADE':   → use GapFadeStrategy
        if rec['strategy'] == 'CONTINUATION': → use GapContinuationStrategy
        if rec['strategy'] == 'SKIP':   → no gap trade
    """

    def __init__(self):
        self._fade        = GapFadeStrategy()
        self._cont        = GapContinuationStrategy()
        self._skip_monday = getattr(settings.gap, "block_on_monday", True)
        self._max_vix     = getattr(settings.gap, "max_vix",         25.0)

    def select_strategy(self, gap_info: dict, context: dict) -> dict:
        """Choose optimal strategy for this gap.

        Args:
            gap_info: Output of calculate_gap_size() plus 'prev_close'/'open_price'
            context:  {
                'volume_ratio': 1.2, 'trend_direction': 'BULLISH',
                'vix': 14.5, 'weekday': 0,  # 0=Mon
                'is_near_sr': False, 'sr_level': 0,
                'trend_alignment': 'WITH', 'fill_probability': 65,
              }

        Returns:
            {
                'strategy': 'FADE' | 'CONTINUATION' | 'SKIP',
                'confidence': 0–100,
                'reasoning': [...],
                'risk_reward': float,
                'historical_win_rate': float,
            }
        """
        # Pre-flight blocks
        preflight, reason = self._preflight(gap_info, context)
        if not preflight:
            return {"strategy": "SKIP", "confidence": 0, "reasoning": [reason],
                    "risk_reward": 0.0, "historical_win_rate": 0.0}

        fade_score, fade_notes = self._score_fade_setup(gap_info, context)
        cont_score, cont_notes = self._score_continuation_setup(gap_info, context)

        logger.debug(f"GapPlaybook: fade={fade_score:.0f} cont={cont_score:.0f}")

        if max(fade_score, cont_score) < 40:
            return {
                "strategy": "SKIP", "confidence": 0,
                "reasoning": ["Neither fade nor continuation scored ≥ 40"],
                "risk_reward": 0.0, "historical_win_rate": 0.0,
            }

        if fade_score >= cont_score:
            return self._build_fade_recommendation(gap_info, context, fade_score, fade_notes)
        return self._build_continuation_recommendation(gap_info, context, cont_score, cont_notes)

    # ── Pre-flight ────────────────────────────────────────────────────────────

    def _preflight(self, gap_info: dict, context: dict) -> tuple:
        cfg    = settings.gap
        cat    = gap_info.get("category", "NONE")
        weekday = context.get("weekday", -1)
        vix     = context.get("vix", 0.0)
        abs_pct = abs(gap_info.get("gap_pct", 0))

        if not getattr(cfg, "enabled", True):
            return False, "GAP_ENABLED=false"
        if cat == "NONE":
            return False, f"Gap {abs_pct:.2f}% below minimum threshold — not tradeable"
        if cat == "COMMON":
            return False, f"COMMON gap ({abs_pct:.2f}%) — too unreliable"
        if self._skip_monday and weekday == 0:
            return False, "Monday — weekend gaps have different dynamics, skipping"
        if vix > 0 and vix > self._max_vix:
            return False, f"VIX={vix:.1f} > {self._max_vix} — gap trades skipped"
        max_pct = getattr(cfg, "max_size_pct", 3.5)
        if abs_pct > max_pct:
            return False, f"Gap size {abs_pct:.2f}% > max {max_pct}% — extreme gap, skip"
        return True, ""

    # ── Fade scoring ─────────────────────────────────────────────────────────

    def _score_fade_setup(self, gap_info: dict, context: dict) -> tuple:
        score = 0.0
        notes = []
        cat     = gap_info.get("category", "NONE")
        abs_pct = abs(gap_info.get("gap_pct", 0))
        vol     = context.get("volume_ratio", 1.0)

        # Gap size
        if cat == "EXHAUSTION":
            score += 30
            notes.append(f"Exhaustion gap ({abs_pct:.1f}%) strongly favours fade")
        elif cat == "RUNAWAY":
            score += 15
            notes.append(f"Runaway gap ({abs_pct:.1f}%) may fade if volume weak")

        # Volume
        if vol < 0.8:
            score += 20
            notes.append(f"Low volume ({vol:.2f}×) — weak gap momentum")
        elif vol < 1.0:
            score += 10

        # S/R
        if context.get("is_near_sr"):
            score += 25
            notes.append(f"Near S/R at {context.get('sr_level', 0):,.0f}")

        # Trend — against trend = more likely to fade
        if context.get("trend_alignment") == "AGAINST":
            score += 15
            notes.append("Gap against prevailing trend direction")

        # Fill probability
        fp = context.get("fill_probability", 50)
        if fp >= 65:
            score += 10
            notes.append(f"Historical fill probability {fp:.0f}%")

        return score, notes

    # ── Continuation scoring ──────────────────────────────────────────────────

    def _score_continuation_setup(self, gap_info: dict, context: dict) -> tuple:
        score = 0.0
        notes = []
        cat     = gap_info.get("category", "NONE")
        abs_pct = abs(gap_info.get("gap_pct", 0))
        vol     = context.get("volume_ratio", 1.0)

        # Gap type
        if cat == "BREAKAWAY":
            score += 25
            notes.append(f"Breakaway gap ({abs_pct:.1f}%) — trend continuation likely")
        elif cat == "RUNAWAY":
            score += 30
            notes.append(f"Runaway gap ({abs_pct:.1f}%) — strong momentum")

        # Volume
        if vol >= 1.5:
            score += 25
            notes.append(f"Very high volume ({vol:.2f}×) — strong institutional conviction")
        elif vol >= 1.2:
            score += 15
            notes.append(f"Above-avg volume ({vol:.2f}×) — confirms momentum")

        # Trend alignment
        if context.get("trend_alignment") == "WITH":
            score += 20
            notes.append("Gap aligns with prevailing trend")

        # ADX
        adx = context.get("adx", 0)
        if adx >= 25:
            score += 10
            notes.append(f"ADX={adx:.0f} — trending market")

        return score, notes

    # ── Recommendation builders ───────────────────────────────────────────────

    def _build_fade_recommendation(self, gap_info, context, score, notes) -> dict:
        md = {
            "current_price": context.get("current_price", gap_info.get("open_price", 0)),
            "open_price":    gap_info.get("open_price", 0),
            "high_15min":    context.get("high_15min",  0),
            "low_15min":     context.get("low_15min",   0),
            "volume_ratio":  context.get("volume_ratio", 1.0),
            "time_since_open": context.get("time_since_open", 10),
        }
        targets = self._fade.calculate_fade_targets(gap_info, md)
        entry   = md["current_price"]
        target  = targets["target_full"]
        stop    = md["high_15min"] or md["open_price"] * 1.003
        rr      = abs(entry - target) / max(abs(entry - stop), 1)
        hw      = 65.0  # historical win rate approximation for exhaustion gaps

        return {
            "strategy":            "FADE",
            "confidence":          min(90, int(score)),
            "reasoning":           notes,
            "risk_reward":         round(rr, 2),
            "historical_win_rate": hw,
            "entry_criteria":      targets,
        }

    def _build_continuation_recommendation(self, gap_info, context, score, notes) -> dict:
        entry   = context.get("current_price", gap_info.get("open_price", 0))
        targets = self._cont.calculate_continuation_targets(gap_info, entry)
        stop    = targets["stop_loss"]
        t1      = targets["target_1x"]
        rr      = abs(entry - t1) / max(abs(entry - stop), 1)
        hw      = 58.0  # historical win rate approximation for breakaway continuation

        return {
            "strategy":            "CONTINUATION",
            "confidence":          min(85, int(score)),
            "reasoning":           notes,
            "risk_reward":         round(rr, 2),
            "historical_win_rate": hw,
            "entry_criteria":      targets,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Module-level GapDetector singleton (imported by engine.py)
# ─────────────────────────────────────────────────────────────────────────────


        """
        Fetch today's open and previous close, classify the gap.
        Returns cached result if already computed today.
        """
        today = date.today()
        # Reset per-index cache on a new calendar day
        if self._last_date != today:
            self._cached_by_index = {}
            self._last_date = today
        if self._index.name in self._cached_by_index:
            return self._cached_by_index[self._index.name]

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

        self._cached_by_index[self._index.name] = analysis
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
        a = self._cached_by_index.get(self._index.name)
        return a is not None and a.gap_type in (GapType.STRONG_UP, GapType.STRONG_DOWN)

    def should_trade_immediately(self) -> bool:
        a = self._cached_by_index.get(self._index.name)
        return (
            a is not None
            and a.trade_direction != "SKIP"
            and not a.wait_for_confirmation
        )


# Singleton for use across the bot
gap_detector = GapDetector()
