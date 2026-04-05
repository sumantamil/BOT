"""Test AI integration across all strategies"""
import asyncio
import pytest
from config import settings
from bot.ai_gateway import get_ai_gateway


class MockSignal:
    """Minimal mock signal for gateway tests."""
    trend = type("obj", (object,), {"value": "BULLISH"})()
    strength = 75
    current_price = 24100
    rsi = 62.0
    vwap = 24050.0
    sma_short = 24080.0
    sma_long = 23900.0
    macd = 10.0
    macd_signal = 8.0
    recommendation = "BUY CE"
    details = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_context(strategy: str) -> dict:
    return {
        "index": "NIFTY",
        "strategy": strategy,
        "vix": 16.5,
        "option_type": "CE",
        "regime": "TRENDING UP",
        "consecutive_losses": 0,
        "trades_done_today": 1,
        "max_trades_per_day": 5,
    }


def _assert_valid_result(result: dict, strategy: str) -> None:
    assert isinstance(result, dict), f"[{strategy}] result must be a dict"
    assert result["approved"] in (True, False), f"[{strategy}] 'approved' must be bool"
    assert result["action"] in ("EXECUTE", "SKIP", "WAIT"), \
        f"[{strategy}] unexpected action: {result['action']}"
    assert 0 <= result["confidence"] <= 100, \
        f"[{strategy}] confidence out of range: {result['confidence']}"
    assert "reasoning" in result, f"[{strategy}] missing 'reasoning'"
    assert "boosted_strength" in result, f"[{strategy}] missing 'boosted_strength'"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_gateway_initialization():
    """AI gateway initialises and returns the same singleton."""
    gw1 = get_ai_gateway()
    gw2 = get_ai_gateway()
    assert gw1 is not None
    assert gw1 is gw2, "get_ai_gateway() must return a singleton"
    print("✅ AI Gateway initialized (singleton confirmed)")


@pytest.mark.asyncio
async def test_orb_validation():
    """ORB signals can be validated through the gateway."""
    gateway = get_ai_gateway()
    result = await gateway.validate_strategy_signal(
        MockSignal(), "ORB", _base_context("ORB")
    )
    _assert_valid_result(result, "ORB")
    print(
        f"✅ ORB validation: {result['action']} "
        f"(confidence: {result['confidence']:.0f}%, "
        f"approved: {result['approved']})"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["ORB", "VWAP", "GAP", "EOD", "LATE_DAY"])
async def test_all_strategies_validation(strategy: str):
    """Every strategy can be validated without errors."""
    gateway = get_ai_gateway()
    result = await gateway.validate_strategy_signal(
        MockSignal(), strategy, _base_context(strategy)
    )
    _assert_valid_result(result, strategy)
    print(
        f"✅ {strategy}: {result['action']} "
        f"(confidence: {result['confidence']:.0f}%)"
    )


@pytest.mark.asyncio
async def test_fallback_on_ai_disabled(monkeypatch):
    """Gateway returns approved=True / action=EXECUTE when AI is disabled."""
    monkeypatch.setattr(settings.ai, "enabled", False)
    # Force a fresh gateway instance with AI disabled
    from bot import ai_gateway as _gw_module
    original = _gw_module._gateway
    _gw_module._gateway = None
    try:
        gateway = get_ai_gateway()
        result = await gateway.validate_strategy_signal(
            MockSignal(), "VWAP", _base_context("VWAP")
        )
        assert result["approved"] is True
        assert result["action"] == "EXECUTE"
        print("✅ Fallback (AI disabled): auto-approved correctly")
    finally:
        _gw_module._gateway = original


@pytest.mark.asyncio
async def test_boosted_strength_in_range():
    """boosted_strength is always clamped to [0, 100]."""
    gateway = get_ai_gateway()
    result = await gateway.validate_strategy_signal(
        MockSignal(), "EOD", _base_context("EOD")
    )
    assert 0 <= result["boosted_strength"] <= 100, \
        f"boosted_strength out of range: {result['boosted_strength']}"
    print(f"✅ boosted_strength={result['boosted_strength']} is within [0, 100]")


# ---------------------------------------------------------------------------
# Direct runner (python tests/test_ai_integration.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def _run_all():
        gw = get_ai_gateway()
        print("\n=== AI Integration Smoke Tests ===\n")

        await test_ai_gateway_initialization()
        await test_orb_validation()

        for strat in ["ORB", "VWAP", "GAP", "EOD", "LATE_DAY"]:
            result = await gw.validate_strategy_signal(
                MockSignal(), strat, _base_context(strat)
            )
            _assert_valid_result(result, strat)
            print(f"✅ {strat}: {result['action']} ({result['confidence']:.0f}%)")

        await test_boosted_strength_in_range()

        print("\n✅ All AI integration tests passed!")

    asyncio.run(_run_all())
