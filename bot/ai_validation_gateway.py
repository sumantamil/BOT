"""
Backwards-compatible alias for bot.ai_gateway.

Import either:
    from bot.ai_gateway import get_ai_gateway, AIValidationGateway
    from bot.ai_validation_gateway import get_ai_gateway, AIValidationGateway
"""
from bot.ai_gateway import AIValidationGateway, get_ai_gateway

__all__ = ["AIValidationGateway", "get_ai_gateway"]
