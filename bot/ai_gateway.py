"""
Unified AI validation gateway for ALL strategies.
Provides a single entry point for all strategies to validate signals before execution.
"""

import asyncio
from typing import Optional, Dict
from loguru import logger
from config import settings
from bot.local_ai_service import get_ai_service, LocalAIService

class AIValidationGateway:
    """
    Gate all strategy trades through AI validation (PROMPT 1).
    Handles fallback if AI is unavailable.
    """
    
    def __init__(self):
        self.ai_service = get_ai_service() if settings.ai.enabled else None
    
    async def validate_strategy_signal(
        self,
        signal,
        strategy_name: str,
        market_context: Dict
    ) -> Dict:
        """
        PROMPT 1: Validate any strategy signal before execution.
        
        Args:
            signal: TrendSignal or strategy-specific signal
            strategy_name: "ORB" | "VWAP" | "Gap" | "EOD" | "LateDay"
            market_context: Dict with VIX, regime, consecutive_losses, etc.
        
        Returns:
            {
                "approved": bool,
                "action": "EXECUTE" | "SKIP" | "WAIT",
                "confidence": 0-100,
                "reasoning": str,
                "boosted_strength": int (0-100),
            }
        """
        # Fallback if AI disabled
        if not self.ai_service or not self.ai_service.enabled:
            logger.debug(f"[{strategy_name}] AI disabled — auto-approve")
            return {
                "approved": True,
                "action": "EXECUTE",
                "confidence": 70,
                "reasoning": "AI disabled — using bot's native filters",
                "boosted_strength": getattr(signal, 'strength', 70),
            }
        
        try:
            # Forward strategy name so local_ai_service can apply strategy-specific time rules
            market_context = dict(market_context, strategy=strategy_name)
            # Call PROMPT 1: Signal validation
            ai_result = await self.ai_service.validate_signal(signal, market_context)
            
            # Map AI response to gateway response
            # WAIT and SKIP both block the trade — only EXECUTE proceeds.
            # Previously WAIT was treated as approved (bug: trade went through anyway).
            approved = ai_result.suggested_action == "EXECUTE"
            boosted = getattr(signal, 'strength', 70)
            
            if ai_result.suggested_action == "EXECUTE":
                # Boost confidence if AI agrees
                boost_factor = getattr(settings.ai, 'confidence_boost', 1.4)
                boosted = int(min(100, boosted * boost_factor))
            
            return {
                "approved": approved,
                "action": ai_result.suggested_action,
                "confidence": ai_result.confidence,
                "reasoning": ai_result.reasoning,
                "boosted_strength": boosted,
                "latency_ms": ai_result.response_ms,
            }
        
        except Exception as e:
            logger.warning(f"[{strategy_name}] AI validation error: {e} — proceeding without AI")
            return {
                "approved": True,
                "action": "EXECUTE",
                "confidence": 50,
                "reasoning": f"AI error (fallback): {str(e)}",
                "boosted_strength": getattr(signal, 'strength', 70),
            }


# Global instance
_gateway = None

def get_ai_gateway() -> AIValidationGateway:
    global _gateway
    if _gateway is None:
        _gateway = AIValidationGateway()
    return _gateway
