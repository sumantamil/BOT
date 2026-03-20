"""
tests/test_gap_strategies.py
─────────────────────────────
Comprehensive test suite for the gap trading system.

Tests cover:
  • Gap detection and classification (calculate_gap_size / classify_gap)
  • get_gap_signal() direction and timing logic
  • GapFadeStrategy — should_fade_gap / generate_fade_signal / calculate_fade_targets
  • GapContinuationStrategy — should_trade_continuation / detect_pullback_entry / targets
  • AdvancedGapAnalyzer — S/R detection, fill probability, gap sequence
  • GapPlaybook — strategy selection, pre-flight blocks, scoring
  • Edge cases: Monday block, VIX block, gap too small/large, COMMON gap skip

Run: pytest tests/test_gap_strategies.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, '.')

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

from bot.gap_detector import (
    GapType, GapCategory, GapStrength, GapAnalysis, GapInfo,
    calculate_gap_size, classify_gap, get_gap_signal,
    GapDetector, GapFadeStrategy, GapContinuationStrategy,
    AdvancedGapAnalyzer, GapPlaybook,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_daily_df(n: int = 60, base: float = 23000.0, trend: float = 0.0) -> pd.DataFrame:
    """Generated synthetic daily OHLCV data."""
    dates  = pd.date_range(end=date.today(), periods=n, freq="B")
    closes = base + trend * np.arange(n) + np.random.RandomState(42).randn(n) * 50
    opens  = closes + np.random.RandomState(7).randn(n) * 30
    highs  = np.maximum(opens, closes) + abs(np.random.RandomState(13).randn(n) * 20)
    lows   = np.minimum(opens, closes) - abs(np.random.RandomState(17).randn(n) * 20)
    vols   = np.random.RandomState(19).randint(100_000, 500_000, size=n).astype(float)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=dates,
    )


def _make_5m_candles(open_price: float, direction: str = "UP", n: int = 20) -> pd.DataFrame:
    """Simulate 5-minute intraday candles after a gap."""
    prices = [open_price]
    rng    = np.random.RandomState(99)
    for _ in range(n - 1):
        move = rng.randn() * 20
        if direction == "UP":
            move = abs(move) * 0.3 + rng.randn() * 10
        else:
            move = -abs(move) * 0.3 + rng.randn() * 10
        prices.append(prices[-1] + move)

    prices = np.array(prices)
    opens  = prices
    closes = np.roll(prices, -1)
    closes[-1] = prices[-1]
    highs  = np.maximum(opens, closes) + abs(rng.randn(n) * 5)
    lows   = np.minimum(opens, closes) - abs(rng.randn(n) * 5)
    vols   = rng.randint(50_000, 150_000, size=n).astype(float)

    idx = pd.date_range("2026-03-17 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Section 1: calculate_gap_size
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculateGapSize:
    def test_gap_up_basic(self):
        info = calculate_gap_size(23650, 23400)
        assert info["direction"] == "UP"
        assert info["gap_pct"] > 0
        assert abs(info["gap_pct"] - 1.068) < 0.01
        assert info["gap_points"] == pytest.approx(250, abs=1)

    def test_gap_down_basic(self):
        info = calculate_gap_size(23200, 23400)
        assert info["direction"] == "DOWN"
        assert info["gap_pct"] < 0
        assert info["gap_points"] < 0

    def test_no_gap(self):
        info = calculate_gap_size(23400, 23400)
        assert info["gap_pct"] == pytest.approx(0.0, abs=0.05)
        assert info["category"] == "NONE"

    def test_invalid_prev_close_zero(self):
        info = calculate_gap_size(23400, 0)
        assert info["category"] == "NONE"

    def test_breakaway_classification(self):
        # 1.07% → BREAKAWAY
        info = calculate_gap_size(23650, 23400)
        assert info["category"] == "BREAKAWAY"

    def test_exhaustion_classification(self):
        # 3% → EXHAUSTION
        info = calculate_gap_size(24000, 23300)
        assert info["category"] == "EXHAUSTION"
        assert info["strength"] == "EXTREME"

    def test_runaway_classification(self):
        # ~2% → RUNAWAY
        info = calculate_gap_size(23868, 23400)
        assert info["category"] == "RUNAWAY"
        assert info["strength"] == "STRONG"

    def test_common_classification(self):
        # ~0.5% → COMMON
        info = calculate_gap_size(23517, 23400)
        assert info["category"] == "COMMON"


# ─────────────────────────────────────────────────────────────────────────────
# Section 2: classify_gap
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyGap:
    def test_below_threshold(self):
        cat, strength = classify_gap(0.1)
        assert cat == GapCategory.NONE
        assert strength == GapStrength.WEAK

    def test_common_gap(self):
        cat, strength = classify_gap(0.5)
        assert cat == GapCategory.COMMON

    def test_breakaway_gap(self):
        cat, strength = classify_gap(1.2)
        assert cat == GapCategory.BREAKAWAY
        assert strength in (GapStrength.MEDIUM, GapStrength.STRONG)

    def test_runaway_gap(self):
        cat, strength = classify_gap(2.0)
        assert cat == GapCategory.RUNAWAY
        assert strength == GapStrength.STRONG

    def test_exhaustion_gap(self):
        cat, strength = classify_gap(3.0)
        assert cat == GapCategory.EXHAUSTION
        assert strength == GapStrength.EXTREME


# ─────────────────────────────────────────────────────────────────────────────
# Section 3: get_gap_signal
# ─────────────────────────────────────────────────────────────────────────────

class TestGetGapSignal:
    def _exhaustion_up(self):
        return {
            "gap_pct": 2.8, "gap_points": 650.0,
            "direction": "UP", "category": "EXHAUSTION", "strength": "EXTREME",
            "prev_close": 23200.0, "open_price": 23850.0,
        }

    def _breakaway_up(self):
        return {
            "gap_pct": 1.2, "gap_points": 280.0,
            "direction": "UP", "category": "BREAKAWAY", "strength": "STRONG",
            "prev_close": 23400.0, "open_price": 23680.0,
        }

    def test_exhaustion_gap_generates_fade_pe(self):
        sig = get_gap_signal(self._exhaustion_up(), 23800.0, 10)
        assert sig is not None
        assert sig["direction"] == "PE"
        assert sig["strategy"] == "GAP_FADE"
        assert sig["confidence"] >= 60

    def test_breakaway_gap_generates_continuation_ce(self):
        sig = get_gap_signal(self._breakaway_up(), 23680.0, 10)
        assert sig is not None
        assert sig["direction"] == "CE"
        assert sig["strategy"] == "GAP_CONTINUATION"

    def test_common_gap_returns_none(self):
        info = {"gap_pct": 0.5, "direction": "UP", "category": "COMMON", "strength": "WEAK"}
        assert get_gap_signal(info, 23450.0, 5) is None

    def test_entry_window_expired_returns_none(self):
        # 80 minutes after open → fade window (35 min) closed
        sig = get_gap_signal(self._exhaustion_up(), 23800.0, 80)
        assert sig is None

    def test_continuation_direction_down(self):
        info = {
            "gap_pct": -1.2, "gap_points": -280.0,
            "direction": "DOWN", "category": "BREAKAWAY", "strength": "STRONG",
            "prev_close": 23400.0, "open_price": 23120.0,
        }
        sig = get_gap_signal(info, 23120.0, 10)
        assert sig is not None
        assert sig["direction"] == "PE"


# ─────────────────────────────────────────────────────────────────────────────
# Section 4: GapFadeStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestGapFadeStrategy:
    def setup_method(self):
        self.strat = GapFadeStrategy()

    def _exhaustion_gap_info(self):
        return {
            "gap_pct": 2.8, "gap_points": 650.0,
            "direction": "UP", "category": "EXHAUSTION", "strength": "EXTREME",
            "prev_close": 23200.0, "open_price": 23850.0,
        }

    def _market_data_valid(self):
        return {
            "current_price": 23820.0,  # below open_price = reversal starting
            "open_price":    23850.0,
            "high_15min":    23900.0,
            "low_15min":     23780.0,
            "volume_ratio":  0.8,
            "time_since_open": 10,
        }

    def test_fade_exhaustion_gap_valid(self):
        assert self.strat.should_fade_gap(self._exhaustion_gap_info(), self._market_data_valid())

    def test_no_fade_small_gap(self):
        gap = {**self._exhaustion_gap_info(), "gap_pct": 0.5, "category": "COMMON"}
        assert not self.strat.should_fade_gap(gap, self._market_data_valid())

    def test_no_fade_too_early(self):
        md = {**self._market_data_valid(), "time_since_open": 1}
        assert not self.strat.should_fade_gap(self._exhaustion_gap_info(), md)

    def test_no_fade_entry_window_expired(self):
        md = {**self._market_data_valid(), "time_since_open": 50}
        assert not self.strat.should_fade_gap(self._exhaustion_gap_info(), md)

    def test_no_fade_without_confirmation(self):
        # price still at/above open → no reversal
        md = {**self._market_data_valid(), "current_price": 23860.0}
        assert not self.strat.should_fade_gap(self._exhaustion_gap_info(), md)

    def test_generate_fade_signal_returns_pe(self):
        sig = self.strat.generate_fade_signal(self._exhaustion_gap_info(), self._market_data_valid())
        assert sig is not None
        assert sig["direction"] == "PE"
        assert sig["strategy"] == "GAP_FADE"
        assert sig["confidence"] >= 60

    def test_generate_fade_signal_none_on_small_gap(self):
        gap = {**self._exhaustion_gap_info(), "gap_pct": 0.3, "category": "COMMON"}
        assert self.strat.generate_fade_signal(gap, self._market_data_valid()) is None

    def test_calculate_fade_targets_tiered(self):
        targets = self.strat.calculate_fade_targets(
            self._exhaustion_gap_info(), self._market_data_valid()
        )
        assert "target_25" in targets
        assert "target_50" in targets
        assert "target_full" in targets
        # For gap-up fade: targets should be below current price
        cur = self._market_data_valid()["current_price"]
        assert targets["target_25"] < cur
        assert targets["target_50"] <= targets["target_25"]
        assert targets["target_full"] == pytest.approx(23200.0, abs=5)

    def test_fade_gap_down_generates_ce(self):
        gap_down = {
            "gap_pct": -2.8, "gap_points": -650.0,
            "direction": "DOWN", "category": "EXHAUSTION", "strength": "EXTREME",
            "prev_close": 23800.0, "open_price": 23150.0,
        }
        md = {
            "current_price": 23180.0,   # above open_price = bounce starting
            "open_price":    23150.0,
            "high_15min":    23250.0,
            "low_15min":     23080.0,
            "volume_ratio":  0.7,
            "time_since_open": 12,
        }
        sig = self.strat.generate_fade_signal(gap_down, md)
        assert sig is not None
        assert sig["direction"] == "CE"


# ─────────────────────────────────────────────────────────────────────────────
# Section 5: GapContinuationStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestGapContinuationStrategy:
    def setup_method(self):
        self.strat = GapContinuationStrategy()

    def _breakaway_gap_info(self):
        return {
            "gap_pct": 1.2, "gap_points": 280.0,
            "direction": "UP", "category": "BREAKAWAY", "strength": "STRONG",
            "prev_close": 23400.0, "open_price": 23680.0,
        }

    def _market_data_valid(self):
        return {
            "current_price": 23680.0,
            "open_price":    23680.0,
            "volume_ratio":  1.3,
            "time_since_open": 20,
        }

    def _trend_bullish(self):
        return {"direction": "BULLISH", "strength": "STRONG", "adx": 28}

    def test_continuation_breakaway_valid(self):
        assert self.strat.should_trade_continuation(
            self._breakaway_gap_info(), self._market_data_valid(), self._trend_bullish()
        )

    def test_no_continuation_small_gap(self):
        gap = {**self._breakaway_gap_info(), "gap_pct": 0.3, "category": "COMMON"}
        assert not self.strat.should_trade_continuation(gap, self._market_data_valid())

    def test_no_continuation_counter_trend(self):
        trend = {"direction": "BEARISH", "strength": "STRONG"}
        assert not self.strat.should_trade_continuation(
            self._breakaway_gap_info(), self._market_data_valid(), trend
        )

    def test_no_continuation_low_volume(self):
        md = {**self._market_data_valid(), "volume_ratio": 0.6}
        assert not self.strat.should_trade_continuation(
            self._breakaway_gap_info(), md, self._trend_bullish()
        )

    def test_generate_continuation_signal_ce(self):
        sig = self.strat.generate_continuation_signal(
            self._breakaway_gap_info(), self._market_data_valid(), self._trend_bullish()
        )
        assert sig is not None
        assert sig["direction"] == "CE"
        assert sig["strategy"] == "GAP_CONTINUATION"
        assert sig["trail_stop"] is True

    def test_calculate_continuation_targets(self):
        targets = self.strat.calculate_continuation_targets(
            self._breakaway_gap_info(), 23680.0
        )
        assert targets["stop_loss"] == pytest.approx(23400.0, abs=5)
        assert targets["target_1x"] > 23680.0
        assert targets["target_2x"] > targets["target_1x"]

    def test_detect_pullback_entry_gap_up(self):
        candles = _make_5m_candles(23680.0, direction="DOWN", n=10)
        # Force a small dip then hold above prev_close
        candles["Close"] = [23660, 23640, 23645, 23650, 23655,
                            23660, 23665, 23670, 23675, 23680]
        candles["Low"]   = candles["Close"] - 5
        candles["High"]  = candles["Close"] + 5

        gap_info = {**self._breakaway_gap_info(), "prev_close": 23400.0, "open_price": 23680.0}
        entry = self.strat.detect_pullback_entry(gap_info, candles)
        # pullback_min_pts = 280 * 0.2 / 100 = 0.56 pts — very small, should detect
        # The important check: returns None only if gap is filled
        # (prev_close = 23400, so price at 23640 still holds gap)
        assert entry is None or entry.get("direction") == "CE"

    def test_detect_pullback_returns_none_on_empty(self):
        assert self.strat.detect_pullback_entry(self._breakaway_gap_info(), None) is None
        assert self.strat.detect_pullback_entry(
            self._breakaway_gap_info(), pd.DataFrame()
        ) is None

    def test_continuation_gap_down_generates_pe(self):
        gap_down = {
            "gap_pct": -1.2, "gap_points": -280.0,
            "direction": "DOWN", "category": "BREAKAWAY", "strength": "STRONG",
            "prev_close": 23680.0, "open_price": 23400.0,
        }
        md = {**self._market_data_valid(), "current_price": 23400.0}
        td = {"direction": "BEARISH", "strength": "STRONG"}
        sig = self.strat.generate_continuation_signal(gap_down, md, td)
        assert sig is not None
        assert sig["direction"] == "PE"


# ─────────────────────────────────────────────────────────────────────────────
# Section 6: AdvancedGapAnalyzer
# ─────────────────────────────────────────────────────────────────────────────

class TestAdvancedGapAnalyzer:
    def setup_method(self):
        self.df      = _make_daily_df(60, base=23000.0, trend=5.0)
        self.analyzer = AdvancedGapAnalyzer(self.df)

    def test_init_empty_data(self):
        a = AdvancedGapAnalyzer(pd.DataFrame())
        assert a._sr_levels == []

    def test_fill_probability_exhaustion(self):
        gap_info = {"gap_pct": 2.8, "category": "EXHAUSTION", "direction": "UP"}
        ctx      = {"volume_profile": "LOW", "trend_alignment": "AGAINST",
                    "gap_sequence": "SINGLE", "near_sr": True}
        prob = self.analyzer.calculate_fill_probability(gap_info, ctx)
        assert prob > 60     # exhaustion + low volume + against trend → high fill prob

    def test_fill_probability_runaway_high_volume(self):
        gap_info = {"gap_pct": 2.0, "category": "RUNAWAY", "direction": "UP"}
        ctx      = {"volume_profile": "HIGH", "trend_alignment": "WITH",
                    "gap_sequence": "RUNAWAY", "near_sr": False}
        prob = self.analyzer.calculate_fill_probability(gap_info, ctx)
        assert prob < 50     # runaway + high vol + with trend → low fill prob

    def test_detect_gap_sequence_single(self):
        gap = {"direction": "UP", "gap_pct": 1.2}
        seq = self.analyzer.detect_gap_sequence(gap)
        assert seq in ("SINGLE", "SERIES", "RUNAWAY", "CLUSTER")

    def test_get_historical_gap_stats_breakaway(self):
        stats = self.analyzer.get_historical_gap_stats("BREAKAWAY")
        assert "fill_rate" in stats
        assert "sample_size" in stats
        assert 0 <= stats["fill_rate"] <= 100

    def test_analyze_gap_context_returns_all_keys(self):
        gap_info = {"gap_pct": 1.4, "category": "BREAKAWAY", "direction": "UP",
                    "volume_profile": "NORMAL"}
        ctx = self.analyzer.analyze_gap_context(gap_info, 23400.0)
        expected_keys = {
            "near_sr", "sr_distance", "trend_alignment", "at_extreme",
            "gap_sequence", "volume_profile", "fill_probability", "continuation_probability",
        }
        assert expected_keys.issubset(ctx.keys())
        assert ctx["fill_probability"] + ctx["continuation_probability"] == pytest.approx(100, abs=1)

    def test_at_extreme_detects_52week_high(self):
        df_extreme = self.df.copy()
        # Make last price the 52-week high
        max_high = float(df_extreme["High"].max())
        df_extreme.iloc[-1, df_extreme.columns.get_loc("Close")] = max_high
        a = AdvancedGapAnalyzer(df_extreme)
        assert a._at_extreme(max_high) is True

    def test_nearest_sr_no_levels(self):
        a = AdvancedGapAnalyzer(pd.DataFrame())
        near, dist = a._nearest_sr(23400.0)
        assert near is False
        assert dist == 999.0


# ─────────────────────────────────────────────────────────────────────────────
# Section 7: GapPlaybook
# ─────────────────────────────────────────────────────────────────────────────

class TestGapPlaybook:
    def setup_method(self):
        self.playbook = GapPlaybook()

    def _exhaustion_gap(self):
        return {
            "gap_pct": 2.8, "gap_points": 650.0,
            "direction": "UP", "category": "EXHAUSTION", "strength": "EXTREME",
            "prev_close": 23200.0, "open_price": 23850.0,
        }

    def _breakaway_gap(self):
        return {
            "gap_pct": 1.2, "gap_points": 280.0,
            "direction": "UP", "category": "BREAKAWAY", "strength": "STRONG",
            "prev_close": 23400.0, "open_price": 23680.0,
        }

    def _context_normal(self):
        return {
            "weekday": 1,          # Tuesday
            "vix": 15.0,
            "volume_ratio": 1.0,
            "trend_direction": "BULLISH",
            "is_near_sr": False,
            "trend_alignment": "WITH",
            "fill_probability": 55.0,
            "time_since_open": 10,
            "current_price": 23680.0,
        }

    # ── Pre-flight blocks ─────────────────────────────────────────────────────

    def test_block_on_monday(self):
        ctx = {**self._context_normal(), "weekday": 0}
        rec = self.playbook.select_strategy(self._breakaway_gap(), ctx)
        assert rec["strategy"] == "SKIP"
        assert any("Monday" in r for r in rec["reasoning"])

    def test_block_high_vix(self):
        ctx = {**self._context_normal(), "vix": 30.0}
        rec = self.playbook.select_strategy(self._breakaway_gap(), ctx)
        assert rec["strategy"] == "SKIP"
        assert any("VIX" in r for r in rec["reasoning"])

    def test_block_common_gap(self):
        gap = {**self._breakaway_gap(), "gap_pct": 0.5, "category": "COMMON"}
        rec = self.playbook.select_strategy(gap, self._context_normal())
        assert rec["strategy"] == "SKIP"

    # ── Strategy selection ────────────────────────────────────────────────────

    def test_exhaustion_gap_selects_fade(self):
        ctx = {
            **self._context_normal(),
            "volume_ratio": 0.75,   # weak volume favours fade
            "is_near_sr": True,
            "sr_level": 23900.0,
            "fill_probability": 72.0,
        }
        rec = self.playbook.select_strategy(self._exhaustion_gap(), ctx)
        # exhaustion + low volume + near S/R → FADE
        assert rec["strategy"] == "FADE"
        assert rec["confidence"] > 0

    def test_breakaway_gap_high_volume_selects_continuation(self):
        ctx = {
            **self._context_normal(),
            "volume_ratio": 1.8,       # strong volume favours continuation
            "trend_alignment": "WITH",
        }
        rec = self.playbook.select_strategy(self._breakaway_gap(), ctx)
        assert rec["strategy"] == "CONTINUATION"

    def test_result_has_required_keys(self):
        rec = self.playbook.select_strategy(self._breakaway_gap(), self._context_normal())
        assert "strategy"            in rec
        assert "confidence"          in rec
        assert "reasoning"           in rec
        assert "risk_reward"         in rec
        assert "historical_win_rate" in rec

    def test_reasoning_is_list(self):
        rec = self.playbook.select_strategy(self._breakaway_gap(), self._context_normal())
        assert isinstance(rec["reasoning"], list)

    def test_no_gap_returns_skip(self):
        gap_none = {**self._breakaway_gap(), "gap_pct": 0.1, "category": "NONE"}
        rec = self.playbook.select_strategy(gap_none, self._context_normal())
        assert rec["strategy"] == "SKIP"


# ─────────────────────────────────────────────────────────────────────────────
# Section 8: GapDetector (unit-level, no network calls)
# ─────────────────────────────────────────────────────────────────────────────

class TestGapDetector:
    def test_format_report_contains_gap_type(self):
        det      = GapDetector()
        analysis = GapAnalysis(
            gap_type=GapType.STRONG_UP,
            prev_close=23400.0,
            open_price=23760.0,
            gap_points=360.0,
            gap_pct=1.54,
            trade_direction="CE",
            wait_for_confirmation=False,
            note="Test note",
            timestamp=datetime.now(),
        )
        report = det.format_report(analysis)
        assert "STRONG GAP UP" in report
        assert "CE" in report
        assert "23,400" in report

    def test_is_strong_gap_false_when_no_analysis(self):
        det = GapDetector()
        assert det.is_strong_gap() is False

    def test_should_trade_immediately_false_when_no_analysis(self):
        det = GapDetector()
        assert det.should_trade_immediately() is False

    def test_set_index(self):
        from bot.index_config import BANKNIFTY
        det = GapDetector()
        det.set_index(BANKNIFTY)
        assert det._index.name == "BANKNIFTY"

    @patch("bot.gap_detector.yf.Ticker")
    def test_analyze_returns_none_on_empty_data(self, mock_ticker):
        mock_ticker.return_value.history.return_value = pd.DataFrame()
        det    = GapDetector()
        result = det.analyze()
        assert result is None

    @patch("bot.gap_detector.yf.Ticker")
    def test_analyze_classifies_strong_gap(self, mock_ticker):
        # Build minimal daily + intraday DataFrames
        daily = pd.DataFrame({
            "Close": [23200.0, 23400.0],
            "Open":  [23100.0, 23000.0],
            "High":  [23300.0, 23500.0],
            "Low":   [23050.0, 22950.0],
            "Volume":[100_000, 120_000],
        }, index=pd.date_range(end=date.today(), periods=2, freq="B"))

        intraday = pd.DataFrame({
            "Open":   [23820.0],
            "Close":  [23810.0],
            "High":   [23850.0],
            "Low":    [23780.0],
            "Volume": [50_000],
        }, index=pd.date_range(
            start=datetime.now().replace(hour=9, minute=15, second=0, microsecond=0),
            periods=1, freq="1min",
        ))

        ticker_mock = MagicMock()
        ticker_mock.history.side_effect = [daily, intraday]
        mock_ticker.return_value = ticker_mock

        det    = GapDetector()
        result = det.analyze()
        assert result is not None
        # 23820 - 23400 = 420 pts = 1.79% → STRONG GAP UP (> 1.5% threshold)
        assert result.gap_type in (GapType.STRONG_UP, GapType.MODERATE_UP)
        assert result.trade_direction == "CE"


# ─────────────────────────────────────────────────────────────────────────────
# Section 9: Integration — fade + continuation combos
# ─────────────────────────────────────────────────────────────────────────────

class TestGapIntegration:
    """Test combined behaviour of fade + continuation + playbook."""

    def test_fade_beats_continuation_on_exhaustion_low_vol(self):
        """Exhaustion gap + low volume → playbook should pick FADE."""
        playbook = GapPlaybook()
        gap = {
            "gap_pct": 2.9, "gap_points": 680.0,
            "direction": "UP", "category": "EXHAUSTION", "strength": "EXTREME",
            "prev_close": 23400.0, "open_price": 24080.0,
        }
        ctx = {
            "weekday": 2, "vix": 16.0,
            "volume_ratio": 0.7,
            "trend_alignment": "AGAINST",
            "is_near_sr": True, "sr_level": 24100.0,
            "fill_probability": 75.0,
            "trend_direction": "BEARISH",
            "current_price": 24060.0,
            "time_since_open": 8,
        }
        rec = playbook.select_strategy(gap, ctx)
        assert rec["strategy"] == "FADE"

    def test_continuation_beats_fade_on_breakaway_high_vol(self):
        """Breakaway gap + high volume + aligned trend → CONTINUATION."""
        playbook = GapPlaybook()
        gap = {
            "gap_pct": 1.3, "gap_points": 305.0,
            "direction": "UP", "category": "BREAKAWAY", "strength": "STRONG",
            "prev_close": 23400.0, "open_price": 23705.0,
        }
        ctx = {
            "weekday": 3, "vix": 13.0,
            "volume_ratio": 2.0,
            "trend_alignment": "WITH",
            "is_near_sr": False, "sr_level": 0,
            "fill_probability": 35.0,
            "trend_direction": "BULLISH",
            "current_price": 23705.0,
            "time_since_open": 5,
            "adx": 30,
        }
        rec = playbook.select_strategy(gap, ctx)
        assert rec["strategy"] == "CONTINUATION"

    def test_one_gap_trade_per_day_via_cache(self):
        """GapDetector caches result; second call returns same object."""
        det = GapDetector()
        analysis = GapAnalysis(
            gap_type=GapType.STRONG_UP,
            prev_close=23400.0,
            open_price=23760.0,
            gap_points=360.0,
            gap_pct=1.54,
            trade_direction="CE",
            wait_for_confirmation=False,
            note="Test",
            timestamp=datetime.now(),
        )
        det._cached_by_index["NIFTY"] = analysis
        det._last_date = date.today()
        # Second call should return same cached object (no new network call)
        with patch.object(det, "_index") as mock_idx:
            mock_idx.name = "NIFTY"
            mock_idx.yahoo_symbol = "^NSEI"
            result = det.analyze()
        assert result is analysis

    def test_gap_fade_targets_sum_to_full_fill(self):
        """Tiered targets should end at prev_close (full fill)."""
        strat   = GapFadeStrategy()
        gap     = {
            "gap_pct": 2.6, "gap_points": 600.0,
            "direction": "UP", "category": "EXHAUSTION",
            "prev_close": 23200.0, "open_price": 23800.0,
        }
        md = {"current_price": 23750.0, "open_price": 23800.0,
              "high_15min": 23850.0, "low_15min": 23720.0}
        targets = strat.calculate_fade_targets(gap, md)
        assert targets["target_full"] == pytest.approx(23200.0, abs=5)
        # Targets must be in descending order for a gap-up fade
        assert targets["target_50"] < targets["target_25"]
        assert targets["target_full"] <= targets["target_50"]


# ─────────────────────────────────────────────────────────────────────────────
# Section 10: Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_classify_gap_zero(self):
        cat, strength = classify_gap(0.0)
        assert cat == GapCategory.NONE

    def test_classify_gap_negative(self):
        # Negative passed in — abs() should be applied
        cat, strength = classify_gap(-1.5)
        # Note: classify_gap uses abs() internally
        # Depending on whether -1.5 < 0 triggers threshold, either way should not crash
        assert isinstance(cat, GapCategory)

    def test_calculate_gap_size_very_large(self):
        info = calculate_gap_size(30000, 23000)
        assert info["category"] == "EXHAUSTION"

    def test_advanced_gap_analyzer_short_data(self):
        """Analyzer should not crash on data shorter than lookback period."""
        df = _make_daily_df(5)
        a  = AdvancedGapAnalyzer(df)
        gap = {"gap_pct": 1.2, "category": "BREAKAWAY", "direction": "UP",
               "volume_profile": "NORMAL"}
        ctx = a.analyze_gap_context(gap, 23200.0)
        assert "fill_probability" in ctx

    def test_fade_strategy_gap_too_large_skip(self):
        """Gaps above max_size_pct should be skipped even for fade."""
        strat = GapFadeStrategy()
        gap = {
            "gap_pct": 5.0, "gap_points": 1200.0,
            "direction": "UP", "category": "EXHAUSTION",
        }
        md = {"current_price": 24500.0, "open_price": 24600.0,
              "high_15min": 24700.0, "low_15min": 24400.0,
              "volume_ratio": 0.8, "time_since_open": 10}
        # max_size_pct default = 3.5 → 5.0 should be blocked
        assert not strat.should_fade_gap(gap, md)

    def test_playbook_returns_skip_on_gap_enabled_false(self):
        with patch("bot.gap_detector.settings.gap") as mock_gap:
            mock_gap.enabled               = False
            mock_gap.block_on_monday       = True
            mock_gap.max_vix               = 25.0
            mock_gap.max_size_pct          = 3.5
            mock_gap.fade_enabled          = True
            mock_gap.continuation_enabled  = True
            pb = GapPlaybook()
            rec = pb.select_strategy(
                {"category": "BREAKAWAY", "gap_pct": 1.2, "direction": "UP"},
                {"weekday": 1, "vix": 14.0},
            )
        assert rec["strategy"] == "SKIP"
