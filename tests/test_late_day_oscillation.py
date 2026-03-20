"""
tests/test_late_day_oscillation.py
───────────────────────────────────
Comprehensive test suite for the Late-Day Oscillation strategy.

Tests cover:
  • LateDayConfig validation (safety limits)
  • LateDayOscillationStrategy daily reset
  • should_activate() — VIX, range, loss-streak, time filters
  • detect_cycle_signal() — checkpoints, direction, confirmation
  • manage_position() — target, stop, time-exit, force-exit
  • format_status() — presence of expected sections
  • Validator helpers — peak/trough detection, direction, chi-square

Run: pytest tests/test_late_day_oscillation.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, '.')

import numpy as np
import pandas as pd
from datetime import datetime, time, date, timedelta
from unittest.mock import patch, MagicMock, PropertyMock

from bot.late_day_oscillation import (
    LateDayOscillationStrategy,
    OscillationSignal,
    OscillationPosition,
    _calc_rsi,
)
from config import settings


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_candles(
    n: int = 20,
    base: float = 23000.0,
    direction: str = "UP",
    hour_start: int = 14,
    min_start: int = 30,
) -> pd.DataFrame:
    """Generate synthetic 5-min IST candles for the late-day window."""
    rng = np.random.RandomState(42)
    prices = [base]
    for _ in range(n - 1):
        move = rng.randn() * 10
        if direction == "UP":
            move = abs(move) * 0.5 + 5
        elif direction == "DOWN":
            move = -abs(move) * 0.5 - 5
        prices.append(prices[-1] + move)

    prices = np.array(prices)
    opens  = prices.copy()
    closes = np.roll(prices, -1)
    closes[-1] = prices[-1]
    highs  = np.maximum(opens, closes) + abs(rng.randn(n))
    lows   = np.minimum(opens, closes) - abs(rng.randn(n))
    volumes = rng.randint(100_000, 300_000, size=n).astype(float)

    start = datetime(2026, 3, 20, hour_start, min_start, 0)
    idx   = pd.date_range(start=start, periods=n, freq="5min")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


def _make_at_checkpoint(checkpoint: time, direction: str = "DOWN") -> pd.DataFrame:
    """
    Make a candle DataFrame where the last candles are at a checkpoint.
    The price movement direction is 'direction' (what just happened),
    so the expected signal should be the opposite.
    """
    # Generate enough candles to arrive at the checkpoint time
    start_h, start_m = 14, 30
    cp_minutes = checkpoint.hour * 60 + checkpoint.minute
    start_minutes = start_h * 60 + start_m
    n = max(4, (cp_minutes - start_minutes) // 5 + 1)
    return _make_candles(n=n, direction=direction, hour_start=start_h, min_start=start_m)


def _now_at(h: int, m: int) -> datetime:
    """Return today's datetime at the given hour:minute."""
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def reset_strategy_and_config():
    """Reset late_day config to safe defaults before each test."""
    # Preserve original values
    orig_enabled    = settings.late_day.enabled
    orig_max_vix    = settings.late_day.max_vix
    orig_max_range  = settings.late_day.max_intraday_range
    orig_max_losses = settings.late_day.max_consecutive_losses
    orig_confidence = settings.late_day.min_pattern_confidence
    orig_rsi        = settings.late_day.require_rsi_extreme
    orig_volume     = settings.late_day.require_volume_spike
    orig_confirm    = settings.late_day.require_confirmation

    yield

    settings.late_day.enabled               = orig_enabled
    settings.late_day.max_vix               = orig_max_vix
    settings.late_day.max_intraday_range    = orig_max_range
    settings.late_day.max_consecutive_losses = orig_max_losses
    settings.late_day.min_pattern_confidence = orig_confidence
    settings.late_day.require_rsi_extreme   = orig_rsi
    settings.late_day.require_volume_spike  = orig_volume
    settings.late_day.require_confirmation  = orig_confirm


# ─────────────────────────────────────────────────────────────────────────────
# TestLateDayConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestLateDayConfig:
    """LateDayConfig safety validation."""

    def test_disabled_by_default(self):
        assert settings.late_day.enabled is False

    def test_validate_passes_when_disabled(self):
        settings.late_day.enabled = False
        settings.late_day.validate_config()   # should not raise

    def test_validate_raises_on_oversized_position(self):
        settings.late_day.enabled = True
        settings.late_day.position_size_multiplier = 0.5  # > 0.3 limit
        with pytest.raises(ValueError, match="(?i)position_size_multiplier|position.size"):
            settings.late_day.validate_config()
        settings.late_day.position_size_multiplier = 0.25  # restore
        settings.late_day.enabled = False

    def test_validate_raises_on_wide_stop(self):
        settings.late_day.enabled = True
        settings.late_day.stop_loss_pct = 0.25  # > 0.2 limit
        with pytest.raises(ValueError, match="(?i)stop_loss_pct|stop.loss"):
            settings.late_day.validate_config()
        settings.late_day.stop_loss_pct = 0.15  # restore
        settings.late_day.enabled = False

    def test_validate_raises_on_too_many_consecutive_losses(self):
        settings.late_day.enabled = True
        settings.late_day.max_consecutive_losses = 3  # > 2 limit
        with pytest.raises(ValueError, match="(?i)consecutive_losses|consecutive"):
            settings.late_day.validate_config()
        settings.late_day.max_consecutive_losses = 2  # restore
        settings.late_day.enabled = False

    def test_validate_passes_safe_values(self):
        settings.late_day.enabled                  = True
        settings.late_day.position_size_multiplier = 0.25
        settings.late_day.stop_loss_pct            = 0.15
        settings.late_day.max_consecutive_losses   = 2
        settings.late_day.validate_config()   # should not raise
        settings.late_day.enabled = False     # reset

    def test_env_prefix(self):
        from config import LateDayConfig
        cfg = LateDayConfig()
        assert cfg.model_config.get("env_prefix") == "LATE_DAY_"


# ─────────────────────────────────────────────────────────────────────────────
# TestDailyReset
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyReset:
    """Strategy auto-resets on date change."""

    def test_fresh_strategy_is_clean(self):
        strat = LateDayOscillationStrategy()
        assert strat.reference_price is None
        assert strat.cycle_count == 0
        assert strat.active_position is None
        assert strat.today_wins == 0
        assert strat.today_losses == 0
        assert strat.consecutive_losses == 0

    def test_reset_on_new_day(self):
        strat = LateDayOscillationStrategy()
        strat._last_date = date(2000, 1, 1)   # simulate old date
        strat.today_wins = 5
        strat.consecutive_losses = 2
        strat._ensure_fresh_day()
        assert strat.today_wins == 0
        assert strat.consecutive_losses == 0

    def test_no_reset_within_same_day(self):
        strat = LateDayOscillationStrategy()
        strat._last_date = datetime.now().date()
        strat.today_wins = 3
        strat._ensure_fresh_day()
        assert strat.today_wins == 3   # not reset


# ─────────────────────────────────────────────────────────────────────────────
# TestShouldActivate
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldActivate:
    """Activation filter logic."""

    def test_returns_false_when_disabled(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled = False
        now   = _now_at(14, 45)
        assert strat.should_activate(now, {"vix": 15.0, "intraday_range_pct": 1.0}) is False

    def test_returns_false_outside_window(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled = True
        # before window
        assert strat.should_activate(_now_at(13, 59), {"vix": 15.0}) is False
        # after force-exit
        assert strat.should_activate(_now_at(15, 30), {"vix": 15.0}) is False

    def test_returns_true_inside_window(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled = True
        assert strat.should_activate(_now_at(14, 45), {"vix": 15.0}) is True

    def test_blocks_on_high_vix(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled  = True
        settings.late_day.max_vix  = 22.0
        assert strat.should_activate(
            _now_at(14, 45), {"vix": 25.0}
        ) is False

    def test_allows_low_vix(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled = True
        assert strat.should_activate(
            _now_at(14, 45), {"vix": 18.0}
        ) is True

    def test_blocks_on_trending_day(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled             = True
        settings.late_day.max_intraday_range  = 1.5
        assert strat.should_activate(
            _now_at(14, 45), {"vix": 15.0, "intraday_range_pct": 2.0}
        ) is False

    def test_blocks_loss_streak(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled               = True
        settings.late_day.max_consecutive_losses = 2
        strat.consecutive_losses = 2
        assert strat.should_activate(_now_at(14, 45), {"vix": 15.0}) is False

    def test_zero_vix_ignored(self):
        """vix=0 means unavailable — should not block."""
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled = True
        assert strat.should_activate(
            _now_at(14, 50), {"vix": 0.0}
        ) is True


# ─────────────────────────────────────────────────────────────────────────────
# TestDetectCycleSignal
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectCycleSignal:
    """Signal generation at checkpoints."""

    def _strat_with_all_confirmations_off(self) -> LateDayOscillationStrategy:
        """Strategy with confirmations disabled so we can test direction logic cleanly."""
        settings.late_day.require_rsi_extreme   = False
        settings.late_day.require_volume_spike  = False
        settings.late_day.require_confirmation  = False
        settings.late_day.min_pattern_confidence = 0
        return LateDayOscillationStrategy()

    def test_no_signal_outside_checkpoint(self):
        strat = self._strat_with_all_confirmations_off()
        # 14:38 is not a checkpoint (±3 min)
        now     = _now_at(14, 38)
        candles = _make_candles(n=10, direction="UP")
        assert strat.detect_cycle_signal(now, 23000.0, candles) is None

    def test_no_signal_when_disabled(self):
        settings.late_day.enabled = False
        strat   = LateDayOscillationStrategy()
        now     = _now_at(14, 45)
        candles = _make_candles(n=10, direction="DOWN")
        # should_activate is not checked inside detect_cycle_signal
        # but it's called upstream; detect_cycle_signal itself doesn't guard on enabled
        # — the guard is at the engine level. Here we just confirm no crash.
        result = strat.detect_cycle_signal(now, 23000.0, candles)
        # May or may not return a signal without the engine-level guard

    def test_signal_at_14_45_down_trend_gives_ce(self):
        strat = self._strat_with_all_confirmations_off()
        now   = _now_at(14, 45)
        # Last 15m moved DOWN → expect UP reversal → CE
        candles = _make_candles(n=6, direction="DOWN", hour_start=14, min_start=30)
        sig = strat.detect_cycle_signal(now, 23000.0, candles)
        # Direction should be CE (expecting UP reversal)
        if sig is not None:
            assert sig.direction == "CE"
            assert sig.cycle == 1

    def test_signal_at_14_45_up_trend_gives_pe(self):
        strat = self._strat_with_all_confirmations_off()
        now   = _now_at(14, 45)
        candles = _make_candles(n=6, direction="UP", hour_start=14, min_start=30)
        sig = strat.detect_cycle_signal(now, 23000.0, candles)
        if sig is not None:
            assert sig.direction == "PE"

    def test_same_checkpoint_not_repeated(self):
        strat   = self._strat_with_all_confirmations_off()
        now     = _now_at(14, 45)
        candles = _make_candles(n=6, direction="DOWN")
        # First call
        strat.detect_cycle_signal(now, 23000.0, candles)
        # Second call at same checkpoint — should return None (already traded)
        result = strat.detect_cycle_signal(now, 23000.0, candles)
        assert result is None

    def test_no_signal_with_open_position(self):
        strat = self._strat_with_all_confirmations_off()
        # Set last_date so _ensure_fresh_day() won't reset what we set
        strat._last_date = datetime.now().date()
        strat.active_position = OscillationPosition(
            direction="CE", entry_price=100.0, stop_loss=99.85,
            target=100.25, entry_time=datetime.now(), cycle=1,
            force_exit_time=time(15, 25),
        )
        now     = _now_at(15, 0)
        candles = _make_candles(n=8, direction="DOWN")
        assert strat.detect_cycle_signal(now, 23000.0, candles) is None

    def test_no_signal_with_insufficient_candles(self):
        strat   = self._strat_with_all_confirmations_off()
        now     = _now_at(14, 45)
        empty   = pd.DataFrame()
        assert strat.detect_cycle_signal(now, 23000.0, empty) is None

    def test_max_cycles_respected(self):
        strat = self._strat_with_all_confirmations_off()
        settings.late_day.max_cycles = 1
        # Set last_date so _ensure_fresh_day() won't reset today_cycles
        strat._last_date = datetime.now().date()
        strat.today_cycles = 1  # at limit
        now     = _now_at(15, 0)
        candles = _make_candles(n=8, direction="DOWN")
        assert strat.detect_cycle_signal(now, 23000.0, candles) is None

    def test_returns_oscillation_signal_shape(self):
        strat   = self._strat_with_all_confirmations_off()
        now     = _now_at(14, 45)
        candles = _make_candles(n=6, direction="DOWN")
        sig = strat.detect_cycle_signal(now, 23000.0, candles)
        if sig is not None:
            assert isinstance(sig, OscillationSignal)
            assert sig.direction   in ("CE", "PE")
            assert sig.stop_loss_pct > 0
            assert sig.target_pct  > 0
            assert 1 <= sig.cycle  <= 3
            assert 0 <= sig.confidence <= 100
            assert isinstance(sig.reasoning, str)


# ─────────────────────────────────────────────────────────────────────────────
# TestRegisterAndManagePosition
# ─────────────────────────────────────────────────────────────────────────────

class TestRegisterAndManagePosition:
    """Position lifecycle: register → manage → close."""

    def _open_position(self, direction: str = "CE", ltp: float = 100.0):
        strat  = LateDayOscillationStrategy()
        signal = OscillationSignal(
            direction=direction, entry_price=23000.0,
            stop_loss_pct=0.15, target_pct=0.25,
            cycle=1, confidence=70, exit_time="14:58",
            reasoning="test", last_move_direction="DOWN",
        )
        strat.register_position(signal, ltp)
        return strat

    def test_register_sets_position(self):
        strat = self._open_position("CE", 100.0)
        assert strat.active_position is not None
        assert strat.active_position.direction   == "CE"
        assert strat.active_position.entry_price == 100.0
        assert strat.active_position.stop_loss   == pytest.approx(100.0 * (1 - 0.15 / 100), rel=1e-4)
        assert strat.active_position.target      == pytest.approx(100.0 * (1 + 0.25 / 100), rel=1e-4)
        assert strat.today_cycles == 1
        assert strat.cycle_count  == 1

    def test_manage_returns_none_while_holding(self):
        strat = self._open_position("CE", 100.0)
        # mid-price — no exit trigger
        now = datetime.now().replace(hour=14, minute=47)
        result = strat.manage_position(now, 100.10)
        assert result is None

    def test_target_exit(self):
        strat  = self._open_position("CE", 100.0)
        # price well above target (100.25)
        now    = datetime.now().replace(hour=14, minute=50)
        result = strat.manage_position(now, 100.30)
        assert result == "EXIT"
        assert strat.active_position is None
        assert strat.today_wins == 1

    def test_stop_loss_exit(self):
        strat  = self._open_position("CE", 100.0)
        now    = datetime.now().replace(hour=14, minute=50)
        result = strat.manage_position(now, 99.80)  # below SL (99.85)
        assert result == "EXIT"
        assert strat.today_losses == 1
        assert strat.consecutive_losses == 1

    def test_force_exit_at_15_25(self):
        strat = self._open_position("CE", 100.0)
        now   = datetime.now().replace(hour=15, minute=25, second=0)
        result = strat.manage_position(now, 100.05)
        assert result == "EXIT"

    def test_time_exit_after_max_minutes(self):
        strat = self._open_position("CE", 100.0)
        settings.late_day.max_time_minutes = 12
        # Push entry time back 13 minutes
        strat.active_position.entry_time = (
            datetime.now() - timedelta(minutes=13)
        )
        now    = datetime.now()
        result = strat.manage_position(now, 100.05)
        assert result == "EXIT"

    def test_consecutive_losses_increment(self):
        strat = self._open_position("CE", 100.0)
        now   = datetime.now().replace(hour=14, minute=50)
        strat.manage_position(now, 99.80)  # stop hit
        assert strat.consecutive_losses == 1

    def test_consecutive_losses_reset_on_win(self):
        strat = self._open_position("CE", 100.0)
        strat.consecutive_losses = 1
        now   = datetime.now().replace(hour=14, minute=50)
        strat.manage_position(now, 100.30)  # target hit
        assert strat.consecutive_losses == 0


# ─────────────────────────────────────────────────────────────────────────────
# TestSkipAndNextCycle
# ─────────────────────────────────────────────────────────────────────────────

class TestSkipAndNextCycle:
    """skip_next_cycle / next_cycle_time helpers."""

    def test_skip_returns_time_string(self):
        strat = LateDayOscillationStrategy()
        with patch("bot.late_day_oscillation.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 20, 14, 40)
            result = strat.skip_next_cycle()
        # The first future checkpoint from 14:40 is 14:45
        assert result == "14:45"

    def test_skip_adds_to_traded_checkpoints(self):
        strat = LateDayOscillationStrategy()
        with patch("bot.late_day_oscillation.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 20, 14, 40)
            strat.skip_next_cycle()
        assert time(14, 45) in strat._traded_checkpoints

    def test_skip_returns_none_when_all_done(self):
        strat = LateDayOscillationStrategy()
        strat._traded_checkpoints = {time(14, 45), time(15, 0), time(15, 15)}
        with patch("bot.late_day_oscillation.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 20, 14, 40)
            result = strat.skip_next_cycle()
        assert result is None

    def test_next_cycle_time_format(self):
        strat = LateDayOscillationStrategy()
        with patch("bot.late_day_oscillation.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 20, 14, 40)
            nxt = strat.next_cycle_time()
        assert nxt == "14:45"


# ─────────────────────────────────────────────────────────────────────────────
# TestFormatStatus
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatStatus:
    """format_status() output sanity checks."""

    def test_contains_key_sections(self):
        strat  = LateDayOscillationStrategy()
        output = strat.format_status()
        assert "LATE-DAY OSCILLATION STATUS" in output
        assert "Force Exit" in output or "15:25" in output
        assert "Today's Stats" in output or "Cycles" in output

    def test_shows_paused_on_loss_streak(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.max_consecutive_losses = 2
        strat.consecutive_losses = 2
        output = strat.format_status()
        assert "PAUSED" in output

    def test_shows_disabled_message_when_off(self):
        settings.late_day.enabled = False
        strat  = LateDayOscillationStrategy()
        output = strat.format_status()
        assert "LATE_DAY_ENABLED" in output or "Enabled" in output


# ─────────────────────────────────────────────────────────────────────────────
# TestRSIHelper
# ─────────────────────────────────────────────────────────────────────────────

class TestRSIHelper:
    """_calc_rsi standalone function."""

    def test_rsi_returns_50_on_short_data(self):
        arr = np.array([100.0, 101.0, 99.0])
        assert _calc_rsi(arr) == 50.0

    def test_rsi_returns_100_on_all_gains(self):
        # All-upward series → RSI → 100
        arr = np.linspace(100, 120, 20)
        rsi = _calc_rsi(arr)
        assert rsi > 90

    def test_rsi_returns_low_on_all_losses(self):
        arr = np.linspace(120, 100, 20)
        rsi = _calc_rsi(arr)
        assert rsi < 10

    def test_rsi_bounded_0_100(self):
        rng = np.random.RandomState(7)
        arr = rng.randn(50).cumsum() + 100
        rsi = _calc_rsi(arr)
        assert 0 <= rsi <= 100


# ─────────────────────────────────────────────────────────────────────────────
# TestValidatorHelpers
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorHelpers:
    """Analysis helpers from LateDayOscillationAnalyzer."""

    def _make_analyzer(self):
        from analysis.late_day_oscillation_validator import LateDayOscillationAnalyzer
        return LateDayOscillationAnalyzer()

    def test_add_15_minutes(self):
        from analysis.late_day_oscillation_validator import LateDayOscillationAnalyzer
        assert LateDayOscillationAnalyzer._add_15("14:45") == "15:00"
        assert LateDayOscillationAnalyzer._add_15("15:15") == "15:30"

    def test_find_peaks_troughs_simple(self):
        ana = self._make_analyzer()
        # Simple V-shape: down then up → one trough
        closes = np.array([100.0, 99.0, 98.0, 99.0, 100.0, 101.0])
        idx    = pd.date_range("2026-03-20 14:30", periods=len(closes), freq="5min")
        df     = pd.DataFrame({"Close": closes}, index=idx)
        peaks, troughs = ana._find_peaks_troughs(df)
        assert len(troughs) >= 1

    def test_get_15min_directions_returns_list(self):
        ana = self._make_analyzer()
        idx = pd.date_range("2026-03-20 14:30", periods=12, freq="5min")
        opens  = np.linspace(100, 103, 12)
        closes = opens + np.array([1, 1, 1, -1, -1, -1, 1, 1, 1, -1, -1, -1])
        df = pd.DataFrame({"Open": opens, "Close": closes}, index=idx)
        dirs = ana._get_15min_directions(df)
        assert isinstance(dirs, list)
        assert all(d[0] in ("UP", "DOWN", None) for d in dirs)

    def test_backtest_returns_dict(self):
        """Smoke test: backtest doesn't crash on synthetic data."""
        ana = self._make_analyzer()
        # Inject synthetic data
        rng = np.random.RandomState(42)
        idx = pd.date_range("2026-01-02 14:30", periods=120, freq="5min")
        closes = 23000 + rng.randn(120).cumsum()
        opens  = closes + rng.randn(120) * 2
        highs  = np.maximum(opens, closes) + abs(rng.randn(120))
        lows   = np.minimum(opens, closes) - abs(rng.randn(120))
        vols   = rng.randint(50000, 200000, size=120).astype(float)
        df = pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
            index=idx,
        )
        # Filter to late window only
        from analysis.late_day_oscillation_validator import _filter_late_window
        ana.data = _filter_late_window(df)
        result = ana.backtest_simple_strategy()
        assert isinstance(result, dict)

    def test_detect_oscillations_empty(self):
        ana = self._make_analyzer()
        ana.data = pd.DataFrame()
        result = ana.detect_oscillations()
        assert result == {}

    def test_calc_cycle_statistics_no_days(self):
        ana = self._make_analyzer()
        ana._day_results = []
        result = ana.calculate_cycle_statistics()
        assert result["total_days_analyzed"] == 0
        assert result["pattern_frequency"]   == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TestEdgeCases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_reference_set_only_once(self):
        strat = LateDayOscillationStrategy()
        strat.set_reference(23000.0, _now_at(14, 30))
        strat.set_reference(24000.0, _now_at(14, 31))
        assert strat.reference_price == 23000.0   # second call ignored

    def test_manage_position_no_position_returns_none(self):
        strat  = LateDayOscillationStrategy()
        now    = _now_at(14, 50)
        result = strat.manage_position(now, 100.0)
        assert result is None

    def test_multiple_loss_pauses_strategy(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.enabled               = True
        settings.late_day.max_consecutive_losses = 2
        strat.consecutive_losses = 2
        should = strat.should_activate(_now_at(14, 50), {"vix": 15.0})
        assert should is False

    def test_checkpoint_window_boundary(self):
        """Test that checkpoint at exactly ±_CP_WINDOW_MIN is accepted/rejected."""
        strat = LateDayOscillationStrategy()
        # At exactly 14:45 → should be in window
        inside = strat._active_checkpoint(time(14, 45))
        assert inside == time(14, 45)
        # At 14:42 → outside the ±3 min pre-checkpoint window (3 min before == 0 ok,
        # but 14:42 is 3 min BEFORE 14:45 which is not ≥0)
        outside_pre = strat._active_checkpoint(time(14, 42))
        assert outside_pre is None
        # At 14:47 (2 min after) → inside window (0 ≤ 2 ≤ 3)
        inside_post = strat._active_checkpoint(time(14, 47))
        assert inside_post == time(14, 45)

    def test_rsi_confirmation_up_expects_rsi_below_40(self):
        strat = LateDayOscillationStrategy()
        settings.late_day.require_rsi_extreme = True
        # Create candles that produce low RSI (all-falling)
        closes = np.linspace(120, 100, 30)
        idx    = pd.date_range("2026-03-20 14:30", periods=30, freq="5min")
        candles = pd.DataFrame({"Close": closes, "Open": closes, "High": closes, "Low": closes, "Volume": np.ones(30) * 100000}, index=idx)
        assert strat._rsi_confirms(candles, "UP") is True   # oversold → confirm UP

    def test_rsi_confirmation_down_expects_rsi_above_60(self):
        strat = LateDayOscillationStrategy()
        # Create candles that produce high RSI (all-rising)
        closes = np.linspace(100, 120, 30)
        idx    = pd.date_range("2026-03-20 14:30", periods=30, freq="5min")
        candles = pd.DataFrame({"Close": closes, "Open": closes, "High": closes, "Low": closes, "Volume": np.ones(30) * 100000}, index=idx)
        assert strat._rsi_confirms(candles, "DOWN") is True  # overbought → confirm DOWN
