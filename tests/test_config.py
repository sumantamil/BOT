"""Minimal config tests for free tier CI"""
import pytest
from config import settings

def test_config_loads():
    """Config should load without errors"""
    assert settings.broker in ["dhan", "zerodha"]
    assert settings.trading.account_balance > 30000

def test_ai_settings():
    """AI settings should be valid if enabled"""
    if settings.ai.enabled:
        assert settings.ai.base_url
        assert settings.ai.model
        assert settings.ai.timeout_seconds > 0

def test_trading_settings():
    """Trading settings should be sensible"""
    assert settings.trading.max_daily_loss < settings.trading.account_balance
    assert settings.trading.default_quantity > 0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
