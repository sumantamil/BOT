"""Test that critical modules load"""
import pytest

def test_engine_imports():
    """Engine module should import"""
    from bot.engine import TradingBot
    assert TradingBot is not None

def test_ai_imports():
    """AI service should import"""
    from bot.local_ai_service import get_ai_service
    assert get_ai_service is not None

def test_config_imports():
    """Config should import"""
    from config import settings
    assert settings is not None

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
