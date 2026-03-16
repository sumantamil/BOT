"""
Custom Trading Instructions Module

Allows users to define custom trading rules via chat commands.
Rules are evaluated against market data to trigger trades.
"""

import re
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, time
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger


class ConditionType(Enum):
    """Types of conditions that can be evaluated"""
    RSI_ABOVE = "rsi_above"
    RSI_BELOW = "rsi_below"
    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    TREND_IS = "trend_is"
    TIME_BETWEEN = "time_between"
    MACD_CROSS_UP = "macd_cross_up"
    MACD_CROSS_DOWN = "macd_cross_down"
    SMA_CROSS_UP = "sma_cross_up"
    SMA_CROSS_DOWN = "sma_cross_down"
    STRENGTH_ABOVE = "strength_above"
    STRENGTH_BELOW = "strength_below"


class ActionType(Enum):
    """Types of actions that can be executed"""
    BUY_CE = "buy_ce"
    BUY_PE = "buy_pe"
    SELL_ALL = "sell_all"
    ALERT = "alert"
    PAUSE = "pause"


@dataclass
class Condition:
    """A single condition to evaluate"""
    condition_type: ConditionType
    value: Any
    
    def evaluate(self, market_data: Dict) -> bool:
        """Evaluate this condition against market data"""
        try:
            if self.condition_type == ConditionType.RSI_ABOVE:
                return market_data.get('rsi', 0) > float(self.value)
            
            elif self.condition_type == ConditionType.RSI_BELOW:
                return market_data.get('rsi', 100) < float(self.value)
            
            elif self.condition_type == ConditionType.PRICE_ABOVE:
                return market_data.get('price', 0) > float(self.value)
            
            elif self.condition_type == ConditionType.PRICE_BELOW:
                return market_data.get('price', float('inf')) < float(self.value)
            
            elif self.condition_type == ConditionType.TREND_IS:
                return market_data.get('trend', '').upper() == str(self.value).upper()
            
            elif self.condition_type == ConditionType.TIME_BETWEEN:
                # Value format: "10:00-14:00"
                now = datetime.now().time()
                start_str, end_str = str(self.value).split('-')
                start = time(*map(int, start_str.split(':')))
                end = time(*map(int, end_str.split(':')))
                return start <= now <= end
            
            elif self.condition_type == ConditionType.STRENGTH_ABOVE:
                return market_data.get('strength', 0) > float(self.value)
            
            elif self.condition_type == ConditionType.STRENGTH_BELOW:
                return market_data.get('strength', 100) < float(self.value)
            
            elif self.condition_type == ConditionType.MACD_CROSS_UP:
                macd = market_data.get('macd', 0)
                signal = market_data.get('macd_signal', 0)
                return macd > signal
            
            elif self.condition_type == ConditionType.MACD_CROSS_DOWN:
                macd = market_data.get('macd', 0)
                signal = market_data.get('macd_signal', 0)
                return macd < signal
            
            elif self.condition_type == ConditionType.SMA_CROSS_UP:
                sma_short = market_data.get('sma_short', 0)
                sma_long = market_data.get('sma_long', 0)
                return sma_short > sma_long
            
            elif self.condition_type == ConditionType.SMA_CROSS_DOWN:
                sma_short = market_data.get('sma_short', 0)
                sma_long = market_data.get('sma_long', 0)
                return sma_short < sma_long
            
            return False
            
        except Exception as e:
            logger.error(f"Condition evaluation error: {e}")
            return False


@dataclass
class Action:
    """An action to execute when conditions are met"""
    action_type: ActionType
    params: Dict = field(default_factory=dict)
    
    def get_description(self) -> str:
        """Get human-readable description"""
        if self.action_type == ActionType.BUY_CE:
            strike = self.params.get('strike', 'ATM')
            qty = self.params.get('qty', 'default')
            return f"Buy CALL at {strike} strike (qty: {qty})"
        elif self.action_type == ActionType.BUY_PE:
            strike = self.params.get('strike', 'ATM')
            qty = self.params.get('qty', 'default')
            return f"Buy PUT at {strike} strike (qty: {qty})"
        elif self.action_type == ActionType.SELL_ALL:
            return "Sell all positions"
        elif self.action_type == ActionType.ALERT:
            return f"Alert: {self.params.get('message', 'Condition met')}"
        elif self.action_type == ActionType.PAUSE:
            return "Pause auto-trading"
        return str(self.action_type.value)


@dataclass
class TradingInstruction:
    """A complete trading instruction with conditions and action"""
    id: str
    name: str
    conditions: List[Condition]
    action: Action
    enabled: bool = True
    triggered_count: int = 0
    last_triggered: Optional[datetime] = None
    cooldown_minutes: int = 5  # Minimum time between triggers
    max_triggers_per_day: int = 3
    created_at: datetime = field(default_factory=datetime.now)
    
    def can_trigger(self) -> bool:
        """Check if instruction can be triggered (respects cooldown)"""
        if not self.enabled:
            return False
        
        if self.last_triggered:
            elapsed = (datetime.now() - self.last_triggered).total_seconds() / 60
            if elapsed < self.cooldown_minutes:
                return False
        
        # Check daily limit
        if self.triggered_count >= self.max_triggers_per_day:
            return False
        
        return True
    
    def evaluate(self, market_data: Dict) -> bool:
        """Evaluate all conditions (AND logic)"""
        if not self.can_trigger():
            return False
        
        return all(cond.evaluate(market_data) for cond in self.conditions)
    
    def mark_triggered(self):
        """Mark this instruction as triggered"""
        self.triggered_count += 1
        self.last_triggered = datetime.now()
    
    def reset_daily(self):
        """Reset daily trigger count"""
        self.triggered_count = 0


class InstructionManager:
    """
    Manages custom trading instructions.
    
    Allows users to:
    - Add new instructions via natural language
    - List active instructions
    - Enable/disable instructions
    - Remove instructions
    """
    
    def __init__(self):
        self._instructions: Dict[str, TradingInstruction] = {}
        self._instruction_counter = 0
        
        # Add some default instructions as examples
        self._add_default_instructions()
    
    def _add_default_instructions(self):
        """Add example instructions (disabled by default)"""
        # Example: Buy CE when RSI is oversold and trend is bullish
        self.add_instruction(
            name="RSI Oversold Buy",
            conditions=[
                Condition(ConditionType.RSI_BELOW, 30),
                Condition(ConditionType.TREND_IS, "BULLISH")
            ],
            action=Action(ActionType.BUY_CE, {"strike": "ATM"}),
            enabled=False  # Disabled by default
        )
        
        # Example: Alert when RSI is overbought
        self.add_instruction(
            name="RSI Overbought Alert",
            conditions=[
                Condition(ConditionType.RSI_ABOVE, 70)
            ],
            action=Action(ActionType.ALERT, {"message": "RSI is overbought - consider taking profits"}),
            enabled=False
        )
    
    def _generate_id(self) -> str:
        """Generate unique instruction ID"""
        self._instruction_counter += 1
        return f"RULE_{self._instruction_counter:03d}"
    
    def add_instruction(
        self,
        name: str,
        conditions: List[Condition],
        action: Action,
        enabled: bool = True,
        cooldown: int = 5,
        max_daily: int = 3
    ) -> TradingInstruction:
        """Add a new trading instruction"""
        instruction = TradingInstruction(
            id=self._generate_id(),
            name=name,
            conditions=conditions,
            action=action,
            enabled=enabled,
            cooldown_minutes=cooldown,
            max_triggers_per_day=max_daily
        )
        
        self._instructions[instruction.id] = instruction
        logger.info(f"Added instruction: {instruction.id} - {name}")
        
        return instruction
    
    def remove_instruction(self, instruction_id: str) -> bool:
        """Remove an instruction by ID"""
        if instruction_id in self._instructions:
            del self._instructions[instruction_id]
            logger.info(f"Removed instruction: {instruction_id}")
            return True
        return False
    
    def enable_instruction(self, instruction_id: str) -> bool:
        """Enable an instruction"""
        if instruction_id in self._instructions:
            self._instructions[instruction_id].enabled = True
            return True
        return False
    
    def disable_instruction(self, instruction_id: str) -> bool:
        """Disable an instruction"""
        if instruction_id in self._instructions:
            self._instructions[instruction_id].enabled = False
            return True
        return False
    
    def get_instruction(self, instruction_id: str) -> Optional[TradingInstruction]:
        """Get an instruction by ID"""
        return self._instructions.get(instruction_id)
    
    def list_instructions(self) -> List[TradingInstruction]:
        """List all instructions"""
        return list(self._instructions.values())
    
    def evaluate_all(self, market_data: Dict) -> List[TradingInstruction]:
        """
        Evaluate all instructions against market data.
        
        Returns:
            List of instructions that should be triggered
        """
        triggered = []
        
        for instruction in self._instructions.values():
            if instruction.evaluate(market_data):
                triggered.append(instruction)
        
        return triggered
    
    def parse_instruction(self, text: str) -> Optional[TradingInstruction]:
        """
        Parse a natural language instruction.
        
        Supported formats:
        - "if rsi < 30 then buy ce"
        - "when trend is bullish and rsi < 40 buy ce at 24000"
        - "alert when rsi > 70"
        - "if price > 24500 then buy pe"
        - "only trade between 10:00-14:00"
        
        Args:
            text: Natural language instruction
            
        Returns:
            Parsed TradingInstruction or None if parsing fails
        """
        text = text.lower().strip()
        
        # Extract conditions and action
        conditions = []
        action = None
        name = text[:50]  # Use first 50 chars as name
        
        # Parse RSI conditions
        rsi_below = re.search(r'rsi\s*[<]\s*(\d+)', text)
        rsi_above = re.search(r'rsi\s*[>]\s*(\d+)', text)
        
        if rsi_below:
            conditions.append(Condition(ConditionType.RSI_BELOW, int(rsi_below.group(1))))
        if rsi_above:
            conditions.append(Condition(ConditionType.RSI_ABOVE, int(rsi_above.group(1))))
        
        # Parse price conditions
        price_below = re.search(r'price\s*[<]\s*(\d+)', text)
        price_above = re.search(r'price\s*[>]\s*(\d+)', text)
        
        if price_below:
            conditions.append(Condition(ConditionType.PRICE_BELOW, int(price_below.group(1))))
        if price_above:
            conditions.append(Condition(ConditionType.PRICE_ABOVE, int(price_above.group(1))))
        
        # Parse trend condition
        if 'trend' in text:
            if 'bullish' in text:
                conditions.append(Condition(ConditionType.TREND_IS, "BULLISH"))
            elif 'bearish' in text:
                conditions.append(Condition(ConditionType.TREND_IS, "BEARISH"))
        
        # Parse strength conditions
        strength_above = re.search(r'strength\s*[>]\s*(\d+)', text)
        strength_below = re.search(r'strength\s*[<]\s*(\d+)', text)
        
        if strength_above:
            conditions.append(Condition(ConditionType.STRENGTH_ABOVE, int(strength_above.group(1))))
        if strength_below:
            conditions.append(Condition(ConditionType.STRENGTH_BELOW, int(strength_below.group(1))))
        
        # Parse time condition
        time_match = re.search(r'between\s*(\d{1,2}:\d{2})\s*[-to]+\s*(\d{1,2}:\d{2})', text)
        if time_match:
            time_range = f"{time_match.group(1)}-{time_match.group(2)}"
            conditions.append(Condition(ConditionType.TIME_BETWEEN, time_range))
        
        # Parse MACD conditions
        if 'macd' in text:
            if 'cross up' in text or 'crossover' in text:
                conditions.append(Condition(ConditionType.MACD_CROSS_UP, True))
            elif 'cross down' in text:
                conditions.append(Condition(ConditionType.MACD_CROSS_DOWN, True))
        
        # Parse action
        strike_match = re.search(r'(?:at|strike)\s*(\d+)', text)
        strike = int(strike_match.group(1)) if strike_match else "ATM"
        
        qty_match = re.search(r'(?:qty|quantity)\s*(\d+)', text)
        qty = int(qty_match.group(1)) if qty_match else None
        
        if 'buy ce' in text or 'buy call' in text:
            params = {"strike": strike}
            if qty:
                params["qty"] = qty
            action = Action(ActionType.BUY_CE, params)
        elif 'buy pe' in text or 'buy put' in text:
            params = {"strike": strike}
            if qty:
                params["qty"] = qty
            action = Action(ActionType.BUY_PE, params)
        elif 'sell all' in text or 'exit all' in text:
            action = Action(ActionType.SELL_ALL)
        elif 'alert' in text:
            msg = re.search(r'alert[:\s]+(.+?)(?:when|if|$)', text)
            message = msg.group(1).strip() if msg else "Custom alert triggered"
            action = Action(ActionType.ALERT, {"message": message})
        elif 'pause' in text:
            action = Action(ActionType.PAUSE)
        
        # Validate
        if not conditions:
            return None
        
        if not action:
            # Default to alert if no action specified
            action = Action(ActionType.ALERT, {"message": f"Condition met: {name}"})
        
        return self.add_instruction(
            name=name,
            conditions=conditions,
            action=action,
            enabled=True
        )
    
    def format_instructions_list(self) -> str:
        """Format all instructions as readable text"""
        if not self._instructions:
            return "No custom instructions defined. Use 'rule add <instruction>' to create one."
        
        lines = ["═══ CUSTOM TRADING INSTRUCTIONS ═══\n"]
        
        for instr in self._instructions.values():
            status = "✓ ENABLED" if instr.enabled else "✗ DISABLED"
            conditions_str = " AND ".join([
                f"{c.condition_type.value}={c.value}" for c in instr.conditions
            ])
            
            lines.append(f"[{instr.id}] {instr.name[:30]}")
            lines.append(f"  Status: {status}")
            lines.append(f"  When: {conditions_str}")
            lines.append(f"  Then: {instr.action.get_description()}")
            lines.append(f"  Triggered: {instr.triggered_count}x (max {instr.max_triggers_per_day}/day)")
            lines.append("")
        
        return "\n".join(lines)
    
    def reset_daily_counts(self):
        """Reset all daily trigger counts"""
        for instr in self._instructions.values():
            instr.reset_daily()


# Global instance
instruction_manager = InstructionManager()
