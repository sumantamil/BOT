"""
Entry Filters — Universal High-Probability Trade Filter

Provides a scoring system (0–100) that evaluates any trade signal
against 8 confirmation criteria before it reaches the order manager:

  1. Volume confirmation     (15 pts) — is volume supporting the move?
  2. Trend alignment         (20 pts) — with or against the prevailing trend?
  3. Indicator confluence    (15 pts) — RSI, MACD, MA, BB, ADX agreement
  4. Support / Resistance    (15 pts) — trade in right relationship to S/R
  5. Historical win rate     (10 pts) — does this pattern historically work?
  6. Time of day             (10 pts) — is this the optimal window?
  7. Volatility / VIX        (10 pts) — is vol environment appropriate?
  8. Events / news           (5 pts)  — basic heuristic (no live API)

Minimum passing score: 70/100 (configurable via FILTER_MIN_SCORE env var).

Usage:
    from bot.entry_filters import HighProbabilityFilter, build_market_data
    filt  = HighProbabilityFilter()
    is_hp, score, confirmations = filt.evaluate_signal(signal, market_data)
    if is_hp:
        # proceed to position sizing / execution

market_data dict keys (all optional — missing keys score neutrally):
    current_price, vwap, rsi, volume, avg_volume, trend (BULLISH/BEARISH/NEUTRAL),
    macd, sma_20, bb_upper, bb_lower, adx, vix, atr,
    prev_day_high, prev_day_low, entry_atr, current_atr
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

from loguru import logger

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class HighProbabilityFilter:
    """
    Universal pre-trade entry filter for all strategies.

    Each confirmation check is scored and logged.  The calling code receives:
        (is_high_probability: bool, score: int, confirmations: list[str])
    """

    # Point values for each gate
    _VOLUME_PTS    = 15
    _TREND_PTS     = 20
    _INDICATOR_PTS = 15
    _SR_PTS        = 15
    _PATTERN_PTS   = 10
    _TIME_PTS      = 10
    _VIX_PTS       = 10
    _EVENT_PTS     = 5

    # Optimal trading windows per strategy (IST)
    _OPTIMAL_WINDOWS: Dict[str, Tuple[time, time]] = {
        "ORB":               (time(9, 30),  time(11, 30)),
        "VWAP":              (time(10, 30), time(14, 30)),
        "GAP":               (time(9, 15),  time(10, 30)),
        "GAP_FADE":          (time(9, 20),  time(10, 15)),
        "GAP_CONTINUATION":  (time(9, 15),  time(10, 30)),
        "TREND":             (time(9, 30),  time(14, 30)),
        "EOD":               (time(14, 30), time(15, 15)),
        "LATE_DAY":          (time(14, 30), time(15, 25)),
    }

    # Reversal strategies prefer low ADX (choppy); trend strategies prefer high
    _REVERSAL_STRATEGIES = {"VWAP", "EOD", "LATE_DAY", "GAP_FADE"}

    def __init__(self, min_score: int = 70):
        self.min_score = min_score

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        signal: Dict,
        market_data: Dict,
        historical_stats: Optional[Dict] = None,
    ) -> Tuple[bool, int, List[str]]:
        """
        Evaluate a trading signal against all 8 confirmation criteria.

        Args:
            signal:          dict – required keys: direction (CE/PE), strategy (str),
                             optional: confidence (int 0–100), strength (float)
            market_data:     dict – price/indicator snapshot (see module docstring)
            historical_stats: optional dict – {win_rate: float, sample_size: int}

        Returns:
            (is_high_probability, score_0_to_100, confirmations_list)
        """
        score: int = 0
        confirmations: List[str] = []

        direction = signal.get("direction", "").upper()   # "CE" or "PE"
        strategy  = signal.get("strategy",  "").upper()
        now       = datetime.now().time()

        # ── 1. Volume (15 pts) ────────────────────────────────────────────
        score, confirmations = self._check_volume(
            score, confirmations, market_data
        )

        # ── 2. Trend alignment (20 pts) ──────────────────────────────────
        score, confirmations = self._check_trend(
            score, confirmations, direction, market_data
        )

        # ── 3. Indicator confluence (15 pts) ─────────────────────────────
        score, confirmations = self._check_indicators(
            score, confirmations, direction, strategy, market_data
        )

        # ── 4. Support / Resistance (15 pts) ─────────────────────────────
        score, confirmations = self._check_sr(
            score, confirmations, direction, market_data
        )

        # ── 5. Historical pattern (10 pts) ────────────────────────────────
        score, confirmations = self._check_history(
            score, confirmations, historical_stats
        )

        # ── 6. Time of day (10 pts) ──────────────────────────────────────
        score, confirmations = self._check_time(
            score, confirmations, strategy, now
        )

        # ── 7. Volatility / VIX (10 pts) ─────────────────────────────────
        score, confirmations = self._check_volatility(
            score, confirmations, strategy, market_data
        )

        # ── 8. Events / news (5 pts) ─────────────────────────────────────
        score, confirmations = self._check_events(score, confirmations)

        is_hp = score >= self.min_score
        green = sum(1 for c in confirmations if c.startswith("✅"))

        logger.info(
            f"[EntryFilter] {strategy} {direction}: score={score}/100 "
            f"({'PASS ✅' if is_hp else 'FAIL ❌'})  "
            f"green={green}/{len(confirmations)}"
        )
        if not is_hp:
            fails = [c for c in confirmations if c.startswith("❌")]
            for f in fails:
                logger.debug(f"  {f}")

        return is_hp, score, confirmations

    # ------------------------------------------------------------------
    # Gate implementations
    # ------------------------------------------------------------------

    def _check_volume(
        self, score: int, conf: List[str], md: Dict
    ) -> Tuple[int, List[str]]:
        volume     = float(md.get("volume", 0) or 0)
        avg_volume = float(md.get("avg_volume", 0) or 0)

        if avg_volume <= 0:
            score += 8   # neutral — can't measure
            conf.append("⚠️ Volume data unavailable (scoring neutral)")
            return score, conf

        ratio = volume / avg_volume
        if ratio >= 1.2:
            score += self._VOLUME_PTS
            conf.append(f"✅ High volume ({ratio:.1f}× average)")
        elif ratio >= 1.0:
            score += 10
            conf.append(f"⚠️ Normal volume ({ratio:.1f}× average)")
        elif ratio >= 0.8:
            score += 5
            conf.append(f"⚠️ Below-average volume ({ratio:.1f}×)")
        else:
            conf.append(f"❌ Low volume ({ratio:.1f}× — < 80% of average)")

        return score, conf

    def _check_trend(
        self, score: int, conf: List[str], direction: str, md: Dict
    ) -> Tuple[int, List[str]]:
        trend = (md.get("trend") or "NEUTRAL").upper()

        if (direction == "CE" and trend == "BULLISH") or \
           (direction == "PE" and trend == "BEARISH"):
            score += self._TREND_PTS
            conf.append(f"✅ Trade with trend ({trend})")
        elif trend == "NEUTRAL":
            score += 10
            conf.append("⚠️ Neutral trend — mean-reversion acceptable")
        else:
            conf.append(
                f"❌ Counter-trend trade ({direction} into {trend} market)"
            )

        return score, conf

    def _check_indicators(
        self, score: int, conf: List[str],
        direction: str, strategy: str, md: Dict
    ) -> Tuple[int, List[str]]:
        n_aligned = self._count_indicator_confluence(direction, strategy, md)

        if n_aligned >= 3:
            score += self._INDICATOR_PTS
            conf.append(f"✅ {n_aligned}/5 indicators aligned")
        elif n_aligned >= 2:
            score += 10
            conf.append(f"⚠️ {n_aligned}/5 indicators aligned")
        elif n_aligned >= 1:
            score += 5
            conf.append(f"⚠️ Only {n_aligned}/5 indicator aligned")
        else:
            conf.append("❌ No indicator confluence")

        return score, conf

    def _check_sr(
        self, score: int, conf: List[str], direction: str, md: Dict
    ) -> Tuple[int, List[str]]:
        near, sr_type = self._proximity_to_sr(direction, md)

        if not near:
            score += self._SR_PTS
            conf.append("✅ Away from major S/R — clear path")
        elif sr_type == "SUPPORT" and direction == "CE":
            score += 12
            conf.append("✅ Buying at support")
        elif sr_type == "RESISTANCE" and direction == "PE":
            score += 12
            conf.append("✅ Selling at resistance")
        elif sr_type == "VWAP":
            score += 8
            conf.append("⚠️ Near VWAP (acts as dynamic S/R)")
        else:
            conf.append(f"❌ Unfavourable S/R: {direction} near {sr_type}")

        return score, conf

    def _check_history(
        self, score: int, conf: List[str], historical_stats: Optional[Dict]
    ) -> Tuple[int, List[str]]:
        if historical_stats and historical_stats.get("sample_size", 0) >= 20:
            wr = float(historical_stats["win_rate"])
            n  = int(historical_stats["sample_size"])
            if wr >= 65:
                score += self._PATTERN_PTS
                conf.append(f"✅ Pattern wins {wr:.0f}% in {n} historical trades")
            elif wr >= 55:
                score += 5
                conf.append(f"⚠️ Pattern wins {wr:.0f}% historically ({n} trades)")
            else:
                conf.append(f"❌ Pattern wins only {wr:.0f}% historically ({n} trades)")
        else:
            score += 5   # insufficient data → neutral, not penalised
            conf.append("⚠️ Insufficient historical data (< 20 trades) — scoring neutral")

        return score, conf

    def _check_time(
        self, score: int, conf: List[str], strategy: str, t: time
    ) -> Tuple[int, List[str]]:
        if self._is_optimal_time(strategy, t):
            score += self._TIME_PTS
            conf.append(f"✅ Optimal time window for {strategy}")
        elif self._is_avoid_time(t):
            conf.append("❌ Lunch lull (11:30–12:30) — poor liquidity")
        else:
            score += 5
            conf.append("⚠️ Off-peak time window")

        return score, conf

    def _check_volatility(
        self, score: int, conf: List[str], strategy: str, md: Dict
    ) -> Tuple[int, List[str]]:
        vix = float(md.get("vix", 0) or 0)

        if vix <= 0:
            score += 5   # unknown → neutral
            conf.append("⚠️ VIX unavailable — scoring neutral")
            return score, conf

        stype = strategy.upper()

        if stype in ("ORB", "BREAKOUT", "GAP_CONTINUATION") and 14.0 <= vix <= 20.0:
            score += self._VIX_PTS
            conf.append(f"✅ VIX {vix:.1f} — optimal for breakout strategies")
        elif stype in ("VWAP", "EOD", "LATE_DAY") and vix <= 20.0:
            score += self._VIX_PTS
            conf.append(f"✅ VIX {vix:.1f} — acceptable for mean-reversion / EOD")
        elif stype == "GAP_FADE" and vix <= 22.0:
            score += self._VIX_PTS
            conf.append(f"✅ VIX {vix:.1f} — acceptable for gap fade")
        elif stype == "TREND" and vix <= 18.0:
            score += self._VIX_PTS
            conf.append(f"✅ VIX {vix:.1f} — calm market, trend trades viable")
        elif vix > 25.0:
            conf.append(f"❌ VIX {vix:.1f} — too high, option premiums expensive")
        else:
            score += 5
            conf.append(f"⚠️ VIX {vix:.1f} — borderline (acceptable but not ideal)")

        return score, conf

    def _check_events(
        self, score: int, conf: List[str]
    ) -> Tuple[int, List[str]]:
        if self._no_major_events_today():
            score += self._EVENT_PTS
            conf.append("✅ No known major scheduled events today")
        else:
            conf.append("❌ Potential major event today (expiry / RBI / earnings) — caution")

        return score, conf

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _count_indicator_confluence(
        self, direction: str, strategy: str, md: Dict
    ) -> int:
        """Count how many of 5 indicators agree with the signal direction (0–5)."""
        aligned = 0
        is_reversal = strategy.upper() in self._REVERSAL_STRATEGIES

        price    = float(md.get("current_price", 0) or 0)
        rsi      = float(md.get("rsi", 50) or 50)
        macd     = float(md.get("macd", 0) or 0)
        sma_20   = float(md.get("sma_20", price) or price)
        bb_upper = float(md.get("bb_upper", price * 1.01) or price * 1.01)
        bb_lower = float(md.get("bb_lower", price * 0.99) or price * 0.99)
        adx      = float(md.get("adx", 0) or 0)

        # RSI extreme confirms the direction
        if direction == "CE" and rsi < 40:
            aligned += 1
        elif direction == "PE" and rsi > 60:
            aligned += 1

        # MACD direction
        if (direction == "CE" and macd > 0) or (direction == "PE" and macd < 0):
            aligned += 1

        # Price vs SMA-20
        if price > 0 and sma_20 > 0:
            if (direction == "CE" and price > sma_20) or \
               (direction == "PE" and price < sma_20):
                aligned += 1

        # Bollinger Band extreme (price at band edge = momentum confirmation)
        if bb_lower > 0 and bb_upper > 0:
            if (direction == "CE" and price <= bb_lower) or \
               (direction == "PE" and price >= bb_upper):
                aligned += 1

        # ADX — reversal strategies want ADX < 20; trend strategies want ADX > 25
        if adx > 0:
            if is_reversal and adx < 20:
                aligned += 1
            elif not is_reversal and adx > 25:
                aligned += 1

        return aligned

    def _proximity_to_sr(
        self, direction: str, md: Dict
    ) -> Tuple[bool, str]:
        """
        Returns (near_sr, label).  Uses VWAP, BB bands, and prev-day H/L as S/R.
        Tolerance: 0.2% of price.
        """
        price     = float(md.get("current_price", 0) or 0)
        vwap      = float(md.get("vwap", 0) or 0)
        bb_upper  = float(md.get("bb_upper", 0) or 0)
        bb_lower  = float(md.get("bb_lower", 0) or 0)
        prev_high = float(md.get("prev_day_high", 0) or 0)
        prev_low  = float(md.get("prev_day_low", 0) or 0)

        if price <= 0:
            return False, ""

        tol = 0.002  # 0.2% proximity band

        # Prev-day high acts as resistance
        if prev_high > 0 and abs(price - prev_high) / price < tol:
            if direction == "CE":
                return True, "RESISTANCE"
            return True, "PREV_HIGH"

        # Prev-day low acts as support
        if prev_low > 0 and abs(price - prev_low) / price < tol:
            if direction == "PE":
                return True, "SUPPORT"
            return True, "PREV_LOW"

        # BB upper = resistance zone
        if bb_upper > 0 and price >= bb_upper * 0.999:
            if direction == "CE":
                return True, "RESISTANCE"
            return True, "BB_UPPER"

        # BB lower = support zone
        if bb_lower > 0 and price <= bb_lower * 1.001:
            if direction == "PE":
                return True, "SUPPORT"
            return True, "BB_LOWER"

        # Near VWAP (dynamic S/R)
        if vwap > 0 and abs(price - vwap) / price < tol:
            return True, "VWAP"

        return False, ""

    def _is_optimal_time(self, strategy: str, t: time) -> bool:
        window = self._OPTIMAL_WINDOWS.get(strategy.upper())
        if window is None:
            window = (time(9, 30), time(14, 30))
        return window[0] <= t <= window[1]

    def _is_avoid_time(self, t: time) -> bool:
        """11:30–12:30 lunch hour has poor liquidity on Indian exchanges."""
        return time(11, 30) <= t <= time(12, 30)

    def _no_major_events_today(self) -> bool:
        """
        Basic heuristic: flag Thursdays (NIFTY weekly expiry) as potential
        high-volatility event days.  A full implementation would query
        NSE / RBI event calendars via API.
        """
        return datetime.now().weekday() != 3   # Thursday = expiry day


# ---------------------------------------------------------------------------
# Convenience builder
# ---------------------------------------------------------------------------

def build_market_data(
    *,
    current_price: float = 0.0,
    vwap: float = 0.0,
    rsi: float = 50.0,
    volume: float = 0.0,
    avg_volume: float = 0.0,
    trend: str = "NEUTRAL",
    macd: float = 0.0,
    sma_20: float = 0.0,
    bb_upper: float = 0.0,
    bb_lower: float = 0.0,
    adx: float = 0.0,
    vix: float = 0.0,
    atr: float = 0.0,
    prev_day_high: float = 0.0,
    prev_day_low: float = 0.0,
) -> Dict:
    """Helper to construct a market_data dict from keyword arguments."""
    return {
        "current_price": current_price,
        "vwap":          vwap,
        "rsi":           rsi,
        "volume":        volume,
        "avg_volume":    avg_volume,
        "trend":         trend,
        "macd":          macd,
        "sma_20":        sma_20,
        "bb_upper":      bb_upper,
        "bb_lower":      bb_lower,
        "adx":           adx,
        "vix":           vix,
        "atr":           atr,
        "prev_day_high": prev_day_high,
        "prev_day_low":  prev_day_low,
    }
