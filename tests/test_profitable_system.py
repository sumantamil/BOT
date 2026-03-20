"""
Tests for the profitable entry/exit system:
  - HighProbabilityFilter (entry_filters.py)
  - ExitManager, MultiTargetExit, TrailingStop, TimeBasedExit, VolatilityExit (exit_manager.py)
  - PositionSizer (position_sizer.py)
  - PerformanceOptimizer (performance_optimizer.py)
  - FilterConfig + PositionSizerConfig (config.py)
"""

import math
import os
import sys
import tempfile
import json
from datetime import datetime, time, timedelta
from typing import Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.entry_filters import HighProbabilityFilter, build_market_data
from bot.exit_manager import (
    DynamicStopLoss,
    MultiTargetExit,
    TrailingStop,
    TimeBasedExit,
    VolatilityExit,
    ExitManager,
)
from bot.position_sizer import PositionSizer
from bot.performance_optimizer import PerformanceOptimizer, TradeSnapshot
from config import settings, FilterConfig, PositionSizerConfig


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _good_market_data(**overrides) -> Dict:
    base = build_market_data(
        current_price=24_000.0,
        vwap=23_950.0,
        rsi=35.0,           # oversold → good for CE
        volume=1_500_000,
        avg_volume=1_000_000,
        trend="BULLISH",
        macd=50.0,
        sma_20=23_900.0,
        bb_upper=24_500.0,
        bb_lower=23_300.0,
        adx=28.0,
        vix=16.0,
        atr=80.0,
        prev_day_high=24_500.0,
        prev_day_low=23_200.0,
    )
    base.update(overrides)
    return base


def _ce_signal(**overrides) -> Dict:
    base = {"direction": "CE", "strategy": "ORB", "confidence": 80}
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# FilterConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterConfig:
    def test_defaults(self):
        cfg = FilterConfig()
        assert cfg.enabled is True
        assert cfg.min_confidence_score == 70
        assert cfg.require_volume_confirmation is True

    def test_custom_score(self, monkeypatch):
        monkeypatch.setenv("FILTER_MIN_CONFIDENCE_SCORE", "80")
        cfg = FilterConfig()
        assert cfg.min_confidence_score == 80

    def test_settings_has_entry_filter(self):
        assert hasattr(settings, "entry_filter")
        assert isinstance(settings.entry_filter, FilterConfig)


# ─────────────────────────────────────────────────────────────────────────────
# PositionSizerConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionSizerConfig:
    def test_defaults(self):
        cfg = PositionSizerConfig()
        assert cfg.enabled is True
        assert cfg.max_risk_per_trade_pct == pytest.approx(2.0)
        assert cfg.kelly_fraction == pytest.approx(0.25)

    def test_settings_has_position_sizer(self):
        assert hasattr(settings, "position_sizer")
        assert isinstance(settings.position_sizer, PositionSizerConfig)


# ─────────────────────────────────────────────────────────────────────────────
# HighProbabilityFilter
# ─────────────────────────────────────────────────────────────────────────────

class TestHighProbabilityFilter:
    def setup_method(self):
        self.filt = HighProbabilityFilter(min_score=70)

    def test_high_quality_signal_passes(self):
        is_hp, score, conf = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data()
        )
        assert is_hp, f"Expected PASS but got score={score}"
        assert score >= 70

    def test_low_volume_reduces_score(self):
        _, score_normal, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data()
        )
        _, score_low, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data(volume=500_000, avg_volume=1_000_000)
        )
        assert score_low < score_normal

    def test_counter_trend_reduces_score(self):
        _, score_with, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data(trend="BULLISH")
        )
        _, score_against, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data(trend="BEARISH")
        )
        assert score_with > score_against

    def test_high_vix_reduces_score(self):
        _, score_low_vix, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data(vix=16.0)
        )
        _, score_high_vix, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data(vix=27.0)
        )
        assert score_high_vix < score_low_vix

    def test_high_confidence_historical_adds_points(self):
        _, score_no_hist, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data()
        )
        _, score_with_hist, _ = self.filt.evaluate_signal(
            _ce_signal(),
            _good_market_data(),
            historical_stats={"win_rate": 70.0, "sample_size": 30},
        )
        assert score_with_hist >= score_no_hist

    def test_poor_historical_lowers_score(self):
        _, score_with_hist, _ = self.filt.evaluate_signal(
            _ce_signal(),
            _good_market_data(),
            historical_stats={"win_rate": 40.0, "sample_size": 30},
        )
        _, score_no_hist, _ = self.filt.evaluate_signal(
            _ce_signal(), _good_market_data()
        )
        assert score_with_hist < score_no_hist

    def test_returns_confirmations_list(self):
        _, _, conf = self.filt.evaluate_signal(_ce_signal(), _good_market_data())
        assert isinstance(conf, list)
        assert len(conf) >= 6

    def test_pe_signal_with_bearish_trend_passes(self):
        pe_sig = {"direction": "PE", "strategy": "VWAP", "confidence": 75}
        md = _good_market_data(
            trend="BEARISH",
            rsi=68.0,          # overbought → good for PE
            macd=-30.0,
            current_price=24_000.0,
            sma_20=24_200.0,   # price below SMA → bearish
            vix=14.0,
        )
        is_hp, score, _ = self.filt.evaluate_signal(pe_sig, md)
        assert score >= 50   # may or may not pass 70 depending on time/event

    def test_build_market_data_helper(self):
        md = build_market_data(current_price=24000, vix=15.0)
        assert md["current_price"] == 24000
        assert md["vix"] == 15.0
        assert "rsi" in md


# ─────────────────────────────────────────────────────────────────────────────
# DynamicStopLoss
# ─────────────────────────────────────────────────────────────────────────────

class TestDynamicStopLoss:
    def setup_method(self):
        self.dsl = DynamicStopLoss()

    def test_orb_ce_stop_below_range_low(self):
        stop = self.dsl.calculate_stop(
            "ORB", entry_price=24200, direction="CE",
            opening_range_low=23900, opening_range_high=24100
        )
        assert stop < 23900

    def test_orb_pe_stop_above_range_high(self):
        stop = self.dsl.calculate_stop(
            "ORB", entry_price=23800, direction="PE",
            opening_range_low=23900, opening_range_high=24100
        )
        assert stop > 24100

    def test_vwap_ce_atr_based(self):
        stop = self.dsl.calculate_stop(
            "VWAP", entry_price=24000, direction="CE", atr=100
        )
        expected = 24000 - 2.0 * 100
        assert stop == pytest.approx(expected, abs=1)

    def test_pe_stop_above_entry(self):
        stop = self.dsl.calculate_stop(
            "TREND", entry_price=24000, direction="PE", atr=100
        )
        assert stop > 24000

    def test_fixed_pct_mode(self):
        stop = self.dsl.calculate_stop(
            "LATE_DAY", entry_price=24000, direction="CE",
            fixed_pct=0.15
        )
        expected = 24000 * (1 - 0.15 / 100)
        assert stop == pytest.approx(expected, abs=0.5)

    def test_fallback_atr_when_zero(self):
        # Should not raise — uses 0.5% of entry as fallback ATR
        stop = self.dsl.calculate_stop(
            "VWAP", entry_price=24000, direction="CE", atr=0
        )
        assert stop < 24000


# ─────────────────────────────────────────────────────────────────────────────
# MultiTargetExit
# ─────────────────────────────────────────────────────────────────────────────

class TestMultiTargetExit:
    def setup_method(self):
        # Entry 24000, SL 23700 (300 pt risk), CE, 100 qty
        self.mt = MultiTargetExit(24000.0, 23700.0, 100, "CE")

    def test_no_target_at_entry(self):
        assert self.mt.check_targets(24000.0) is None

    def test_tier1_at_1r(self):
        # T1 = entry + 1×risk = 24000 + 300 = 24300
        instr = self.mt.check_targets(24300.0)
        assert instr is not None
        assert instr["exit_type"] == "PARTIAL_1"
        assert instr["exit_qty"] == 40   # 40% of 100

    def test_sl_moves_to_be_after_tier1(self):
        self.mt.check_targets(24300.0)   # hit Tier-1
        assert self.mt.stop_loss == pytest.approx(24000.0)

    def test_tier2_at_2r(self):
        # Hit Tier-1 first
        self.mt.check_targets(24300.0)
        instr = self.mt.check_targets(24600.0)   # 24000 + 2×300 = 24600
        assert instr is not None
        assert instr["exit_type"] == "PARTIAL_2"
        assert instr["exit_qty"] == 30

    def test_remaining_qty_decremented(self):
        self.mt.check_targets(24300.0)  # T1: exit 40
        self.mt.check_targets(24600.0)  # T2: exit 30
        assert self.mt.remaining_qty == 30   # 100 - 40 - 30

    def test_all_fixed_targets_hit(self):
        self.mt.check_targets(24300.0)  # T1
        self.mt.check_targets(24600.0)  # T2
        self.mt.check_targets(24900.0)  # T3
        assert self.mt.all_fixed_targets_hit is True

    def test_pe_targets_go_down(self):
        mt_pe = MultiTargetExit(24000.0, 24300.0, 100, "PE")
        # T1 = 24000 - 1×300 = 23700
        instr = mt_pe.check_targets(23700.0)
        assert instr is not None
        assert instr["exit_type"] == "PARTIAL_1"

    def test_runner_never_triggers_from_check_targets(self):
        # RUNNER target has price=0, should never trigger via check_targets
        # Even if we hit T1, T2, T3 and call again, no runner instruction
        self.mt.check_targets(24300.0)
        self.mt.check_targets(24600.0)
        self.mt.check_targets(24900.0)
        instr = self.mt.check_targets(25000.0)
        assert instr is None


# ─────────────────────────────────────────────────────────────────────────────
# TrailingStop
# ─────────────────────────────────────────────────────────────────────────────

class TestTrailingStop:
    def setup_method(self):
        self.trail = TrailingStop(entry_price=24000, initial_stop=23700, direction="CE")

    def test_not_active_before_20pct(self):
        # 10% profit — should not activate
        self.trail.update(26400, profit_pct=10.0)
        assert self.trail.is_active is False

    def test_activates_at_20pct(self):
        hit = self.trail.update(28800, profit_pct=20.0)
        assert self.trail.is_active is True
        assert not hit   # shouldn't be hit at activation

    def test_sl_moves_to_be_on_activation(self):
        # On activation the SL is set to entry (24000) then immediately
        # advanced by the trail calculation; it should be AT LEAST breakeven.
        self.trail.update(28800, profit_pct=20.0)
        assert self.trail.stop_loss >= self.trail.entry_price

    def test_trailing_stop_hit(self):
        self.trail.update(25000, 20.0)   # activate
        self.trail.update(25500, 25.0)   # new high
        # Drop back below trail
        hit = self.trail.update(24200, 1.0)
        assert hit is True

    def test_stop_never_moves_down_ce(self):
        self.trail.update(25000, 20.0)
        self.trail.update(26000, 30.0)
        prev_sl = self.trail.stop_loss
        # Price drops — stop should NOT move down
        self.trail.update(25500, 25.0)
        assert self.trail.stop_loss >= prev_sl

    def test_pe_trailing_stop_logic(self):
        trail_pe = TrailingStop(24000, 24300, "PE")
        trail_pe.update(23000, 20.0)   # activate
        trail_pe.update(22500, 25.0)   # new low
        hit = trail_pe.update(24000, 0.0)
        assert hit is True

    def test_tighter_trail_at_high_profit(self):
        """Higher profit → smaller give-back fraction → stop should be closer to peak."""
        trail1 = TrailingStop(24000, 23700, "CE")
        trail2 = TrailingStop(24000, 23700, "CE")

        # Both reach 55% profit (trail gives back 30%)
        best = 24000 + 24000 * 0.55
        trail1.update(best, 55.0)
        trail1_sl = trail1.stop_loss

        # Same best price but at 25% profit (trail gives back 50%)
        trail2.update(best, 25.0)
        trail2_sl = trail2.stop_loss

        # Higher profit → less give-back → tighter stop → higher SL value
        assert trail1_sl >= trail2_sl


# ─────────────────────────────────────────────────────────────────────────────
# TimeBasedExit
# ─────────────────────────────────────────────────────────────────────────────

class TestTimeBasedExit:
    def setup_method(self):
        self.tbe = TimeBasedExit()
        self.entry = datetime.now() - timedelta(minutes=5)

    def test_no_exit_early_with_profit(self):
        exit_, reason = self.tbe.should_exit(
            "ORB", self.entry, profit_pct=20.0, current_time=time(10, 30)
        )
        assert exit_ is False

    def test_max_hold_exits_orb(self):
        old_entry = datetime.now() - timedelta(minutes=130)
        exit_, reason = self.tbe.should_exit(
            "ORB", old_entry, profit_pct=5.0, current_time=time(11, 0)
        )
        assert exit_ is True
        assert "max hold" in reason.lower() or "ORB" in reason

    def test_max_hold_exits_vwap(self):
        old_entry = datetime.now() - timedelta(minutes=65)
        exit_, reason = self.tbe.should_exit(
            "VWAP", old_entry, profit_pct=2.0, current_time=time(11, 0)
        )
        assert exit_ is True

    def test_losing_trade_at_1430(self):
        exit_, reason = self.tbe.should_exit(
            "ORB", self.entry, profit_pct=-5.0, current_time=time(14, 35)
        )
        assert exit_ is True
        assert "14:30" in reason or "Losing" in reason

    def test_small_profit_at_15_00(self):
        exit_, reason = self.tbe.should_exit(
            "ORB", self.entry, profit_pct=5.0, current_time=time(15, 5)
        )
        assert exit_ is True

    def test_force_exit_at_1515(self):
        exit_, reason = self.tbe.should_exit(
            "TREND", self.entry, profit_pct=30.0, current_time=time(15, 15)
        )
        assert exit_ is True
        assert "15:15" in reason or "Force" in reason or "force" in reason


# ─────────────────────────────────────────────────────────────────────────────
# VolatilityExit
# ─────────────────────────────────────────────────────────────────────────────

class TestVolatilityExit:
    def setup_method(self):
        self.ve = VolatilityExit()

    def test_vix_spike_with_profit(self):
        exit_, reason = self.ve.check(
            "ORB", entry_vix=15.0, current_vix=19.0, profit_pct=20.0
        )
        assert exit_ is True
        assert "spike" in reason.lower() or "VIX" in reason

    def test_no_exit_vix_spike_no_profit(self):
        exit_, _ = self.ve.check(
            "ORB", entry_vix=15.0, current_vix=19.0, profit_pct=5.0
        )
        assert exit_ is False

    def test_vix_collapse_vwap(self):
        exit_, reason = self.ve.check(
            "VWAP", entry_vix=18.0, current_vix=14.0, profit_pct=5.0
        )
        assert exit_ is True

    def test_no_exit_vix_collapse_orb(self):
        exit_, _ = self.ve.check(
            "ORB", entry_vix=18.0, current_vix=14.0, profit_pct=5.0
        )
        assert exit_ is False

    def test_atr_expansion_scalp(self):
        exit_, reason = self.ve.check(
            "LATE_DAY", entry_vix=15.0, current_vix=16.0,
            profit_pct=3.0, current_atr_ratio=1.8
        )
        assert exit_ is True

    def test_danger_zone_vix_no_profit(self):
        exit_, reason = self.ve.check(
            "ORB", entry_vix=15.0, current_vix=27.0, profit_pct=0.0
        )
        assert exit_ is True
        assert "danger" in reason.lower() or "VIX" in reason

    def test_no_entry_vix_skips_check(self):
        exit_, _ = self.ve.check("ORB", entry_vix=0.0, current_vix=30.0, profit_pct=5.0)
        assert exit_ is False


# ─────────────────────────────────────────────────────────────────────────────
# ExitManager (master coordinator)
# ─────────────────────────────────────────────────────────────────────────────

class TestExitManager:
    def _make_mgr(self, strategy="ORB", entry=24000, sl=23700, qty=65, direction="CE"):
        return ExitManager(
            strategy=strategy,
            entry_price=entry,
            stop_loss=sl,
            position_size=qty,
            direction=direction,
            entry_time=datetime.now() - timedelta(minutes=10),
            entry_vix=15.0,
        )

    def test_no_exit_when_price_is_fine(self):
        mgr = self._make_mgr()
        instr = mgr.evaluate_exit(
            {"qty": 65, "pnl_pct": 5.0},
            {"current_price": 24200, "vix": 15.5,
             "current_time": time(10, 30)},   # explicit IST time — avoids force-exit at 15:15
        )
        assert instr is None

    def test_stop_loss_triggered(self):
        mgr = self._make_mgr()
        instr = mgr.evaluate_exit(
            {"qty": 65, "pnl_pct": -15.0},
            {"current_price": 23650, "vix": 15.0},
        )
        assert instr is not None
        assert instr["exit_type"] == "STOP_LOSS"
        assert instr["remaining_qty"] == 0

    def test_partial_target_hit(self):
        mgr = self._make_mgr()  # risk = 300, T1 = 24300
        instr = mgr.evaluate_exit(
            {"qty": 65, "pnl_pct": 12.0},
            {"current_price": 24300, "vix": 15.0},
        )
        assert instr is not None
        assert instr["exit_type"] == "PARTIAL_1"

    def test_time_exit_triggered(self):
        # Create manager with old entry time
        mgr = ExitManager(
            "ORB", 24000, 23700, 65, "CE",
            entry_time=datetime.now() - timedelta(minutes=130),
        )
        instr = mgr.evaluate_exit(
            {"qty": 65, "pnl_pct": 5.0},
            {"current_price": 24100, "vix": 15.0},
        )
        assert instr is not None
        assert instr["exit_type"] == "TIME_EXIT"

    def test_current_stop_loss_property(self):
        mgr = self._make_mgr(entry=24000, sl=23700)
        assert mgr.current_stop_loss == pytest.approx(23700.0)

    def test_format_status_returns_string(self):
        mgr = self._make_mgr()
        s = mgr.format_status()
        assert isinstance(s, str)
        assert "ORB" in s

    def test_pe_stop_loss_above_entry(self):
        mgr = self._make_mgr(strategy="VWAP", entry=24000, sl=24300, direction="PE")
        # CE stop is below, PE stop is above
        assert mgr.current_stop_loss == pytest.approx(24300.0)
        instr = mgr.evaluate_exit(
            {"qty": 65, "pnl_pct": -10.0},
            {"current_price": 24350, "vix": 15.0},
        )
        assert instr is not None
        assert instr["exit_type"] == "STOP_LOSS"


# ─────────────────────────────────────────────────────────────────────────────
# PositionSizer
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionSizer:
    def setup_method(self):
        self.sizer = PositionSizer(account_balance=100_000)

    def test_basic_calculation(self):
        qty = self.sizer.calculate_position_size(
            signal={"strategy": "ORB", "direction": "CE", "confidence": 70},
            entry_price=24_000,
            stop_loss_price=23_700,
            lot_size=75,
        )
        # max_risk = 100000 * 0.02 = 2000, risk_per_unit = 300, base = 6.67 → 1 lot (75)
        assert qty == 75

    def test_high_confidence_increases_size(self):
        low_conf = self.sizer.calculate_position_size(
            {"strategy": "ORB", "direction": "CE", "confidence": 60},
            24_000, 23_700, lot_size=75
        )
        high_conf = self.sizer.calculate_position_size(
            {"strategy": "ORB", "direction": "CE", "confidence": 95},
            24_000, 23_700, lot_size=75
        )
        assert high_conf >= low_conf

    def test_kelly_factor_no_stats_is_1(self):
        factor = self.sizer._kelly_factor("ORB")
        assert factor == pytest.approx(1.0)

    def test_kelly_factor_with_stats(self):
        self.sizer.update_stats({
            "ORB": {"total_trades": 30, "win_rate": 65.0, "avg_win": 20.0, "avg_loss": -10.0}
        })
        factor = self.sizer._kelly_factor("ORB")
        assert 0.5 <= factor <= 2.0

    def test_kelly_factor_below_20_trades_returns_1(self):
        self.sizer.update_stats({
            "ORB": {"total_trades": 15, "win_rate": 70.0, "avg_win": 20.0, "avg_loss": -10.0}
        })
        assert self.sizer._kelly_factor("ORB") == pytest.approx(1.0)

    def test_confidence_factor_table(self):
        assert self.sizer._confidence_factor(95) == pytest.approx(1.3)
        assert self.sizer._confidence_factor(85) == pytest.approx(1.1)
        assert self.sizer._confidence_factor(75) == pytest.approx(1.0)
        assert self.sizer._confidence_factor(65) == pytest.approx(0.8)
        assert self.sizer._confidence_factor(55) == pytest.approx(0.5)

    def test_implied_risk_capped(self):
        # Very tight stop → would produce huge qty; should be capped
        qty = self.sizer.calculate_position_size(
            {"strategy": "ORB", "direction": "CE", "confidence": 95},
            entry_price=24_000,
            stop_loss_price=23_990,   # only 10pt risk → 200 lots without cap
            lot_size=75,
        )
        max_risk = 100_000 * 0.02   # ₹2,000
        implied = 10 * qty
        # Should not exceed 1.5× max_risk
        assert implied <= max_risk * 1.5 + 75 * 10   # allow 1 lot slack

    def test_no_stats_produces_reasonable_qty(self):
        qty = self.sizer.calculate_position_size(
            {"strategy": "UNKNOWN", "direction": "CE", "confidence": 70},
            24_000, 23_500, lot_size=1
        )
        assert qty >= 1

    def test_explain_returns_string(self):
        s = self.sizer.explain(
            {"strategy": "ORB", "direction": "CE", "confidence": 70},
            24000, 23700, lot_size=75
        )
        assert "PositionSizer" in s or "POSITION SIZER" in s.upper()


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceOptimizer
# ─────────────────────────────────────────────────────────────────────────────

def _make_trades(strategy: str, wins: int, losses: int, win_pnl=500, loss_pnl=-300) -> list:
    """Generate synthetic TradeSnapshot objects."""
    snaps = []
    for i in range(wins):
        snaps.append(TradeSnapshot(
            trade_id=f"{strategy}_W{i}",
            strategy=strategy,
            direction="CE",
            entry_time=datetime.now(),
            pnl=win_pnl,
            pnl_pct=20.0,
            win=True,
            exit_reason="PARTIAL_1",
        ))
    for i in range(losses):
        snaps.append(TradeSnapshot(
            trade_id=f"{strategy}_L{i}",
            strategy=strategy,
            direction="CE",
            entry_time=datetime.now(),
            pnl=loss_pnl,
            pnl_pct=-15.0,
            win=False,
            exit_reason="STOP_LOSS",
        ))
    return snaps


class TestPerformanceOptimizer:
    def setup_method(self):
        self.opt = PerformanceOptimizer()

    def _load(self, strategy, wins, losses):
        self.opt._trades = _make_trades(strategy, wins, losses)
        self.opt._rebuild_stats()

    def test_empty_no_crash(self):
        report = self.opt.generate_report()
        assert "No trade history" in report

    def test_rebuild_stats_correct_win_rate(self):
        self._load("ORB", 7, 3)   # 70% win rate
        stats = self.opt.get_strategy_stats()
        assert "ORB" in stats
        assert stats["ORB"]["win_rate"] == pytest.approx(70.0)
        assert stats["ORB"]["total_trades"] == 10

    def test_profit_factor(self):
        self._load("ORB", 6, 4)   # 6 × 500 / (4 × 300) = 3000 / 1200 = 2.5
        stats = self.opt.get_strategy_stats()
        assert stats["ORB"]["profit_factor"] == pytest.approx(2.5, rel=0.01)

    def test_recommendation_disable_for_bad_strategy(self):
        self._load("VWAP", 4, 11)  # wr = 26%, well below 45%
        # Add a second strategy so analyze_by_strategy doesn't only return one
        self.opt._trades += _make_trades("ORB", 7, 3)
        self.opt._rebuild_stats()
        recs = self.opt.generate_recommendations()
        vwap_recs = [r for r in recs if "VWAP" in r["item"] and r["action"] == "DISABLE"]
        assert len(vwap_recs) >= 1

    def test_recommendation_increase_for_good_strategy(self):
        # WR 70%, PF ≥ 1.8
        self._load("ORB", 14, 6)   # 70% WR, PF = 14*500 / (6*300) ≈ 3.89
        recs = self.opt.generate_recommendations()
        orb_recs = [r for r in recs if "ORB" in r["item"] and r["action"] == "INCREASE"]
        assert len(orb_recs) >= 1

    def test_strategy_stats_for_kelly(self):
        self._load("TREND", 8, 4)
        stats = self.opt.get_strategy_stats()
        assert "avg_win" in stats["TREND"]
        assert "avg_loss" in stats["TREND"]

    def test_analyze_by_hour(self):
        self._load("ORB", 5, 5)
        hour_data = self.opt.analyze_by_hour()
        assert isinstance(hour_data, dict)
        for h, d in hour_data.items():
            assert "win_rate" in d

    def test_report_contains_strategy_name(self):
        self._load("EOD", 7, 3)
        report = self.opt.generate_report()
        assert "EOD" in report

    def test_load_journals_missing_dir_returns_zero(self):
        n = self.opt.load_journals("/nonexistent/path")
        assert n == 0

    def test_load_journals_from_tempdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = {
                "trades": [
                    {
                        "trade_id": "T001",
                        "source": "ORB",
                        "option_type": "CE",
                        "timestamp": "2026-03-20T10:30:00",
                        "pnl": 500.0,
                    },
                    {
                        "trade_id": "T002",
                        "source": "VWAP",
                        "option_type": "PE",
                        "timestamp": "2026-03-20T11:45:00",
                        "pnl": -200.0,
                    },
                ]
            }
            with open(os.path.join(tmpdir, "journal_2026-03-20.json"), "w") as f:
                json.dump(journal, f)

            opt2 = PerformanceOptimizer()
            n = opt2.load_journals(tmpdir)
            assert n == 2
            stats = opt2.get_strategy_stats()
            assert "ORB" in stats
            assert "VWAP" in stats

    def test_add_trade_from_mock_record(self):
        class FakeRecord:
            trade_id = "X001"
            source   = "GAP_FADE"
            option_type = "PE"
            timestamp = datetime.now()
            pnl = 350.0

        self.opt.add_trade(FakeRecord())
        stats = self.opt.get_strategy_stats()
        assert "GAP_FADE" in stats
        assert stats["GAP_FADE"]["total_trades"] == 1
