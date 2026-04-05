"""
Local AI Service — Signal Validation via Ollama / LM Studio

Validates every trading signal through a locally-hosted LLM before the order
is placed.  If the AI is unavailable the bot falls back to its own filters and
continues trading normally.

Setup:
  1. Install Ollama:  https://ollama.com/
  2. Pull a model:    ollama pull llama3.2
  3. Start server:    ollama serve       (default: http://localhost:11434)
  4. Set .env:        AI_ENABLED=true
                      AI_BASE_URL=http://localhost:11434
                      AI_MODEL=llama3.2

OR use LM Studio (port 1234) — it exposes the same /v1/chat/completions endpoint.
"""

import json
import logging
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MarketSentimentResult:
    """Parsed response from the local LLM macro-sentiment call (PROMPT 2)."""

    # High-level decision
    action: str = "NEUTRAL"                    # "FAVORABLE" | "NEUTRAL" | "UNFAVORABLE"
    market_sentiment: str = "NEUTRAL"          # "BULLISH" | "BEARISH" | "NEUTRAL"
    sentiment_strength: float = 50.0           # 0–100

    # Conditions
    volatility_regime: str = "NORMAL"          # "LOW" | "NORMAL" | "HIGH" | "EXTREME"
    trading_conditions: str = "Fair"           # "Excellent" | "Good" | "Fair" | "Poor"
    recommended_strategy: str = "Range-Trading"

    # Narrative
    vix_interpretation: str = ""
    confidence: float = 50.0
    reasoning: str = ""

    # Meta
    session: str = ""                          # "Morning" | "Midday" | "Afternoon" | "Close"
    model_used: str = ""
    response_ms: float = 0.0
    fallback: bool = False
    cached: bool = False                       # True when returned from in-memory cache


@dataclass
class PatternMatch:
    """A single chart pattern identified by the LLM."""
    pattern_name: str = ""
    confidence: float = 0.0          # 0–100
    direction: str = "NEUTRAL"        # "UP" | "DOWN" | "NEUTRAL"
    entry_price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    reliability: str = "Low"          # "High" | "Medium" | "Low"
    reasoning: str = ""


@dataclass
class PatternRecognitionResult:
    """Parsed response from the local LLM chart-pattern call (PROMPT 3)."""

    patterns: List[PatternMatch] = field(default_factory=list)
    strongest_pattern: str = ""
    overall_assessment: str = "Neutral"   # "Bullish" | "Bearish" | "Neutral"
    recommended_action: str = "WAIT"      # "BUY_CE" | "BUY_PE" | "WAIT" | "AVOID"
    risk_assessment: str = "Medium Risk"  # "High Risk" | "Medium Risk" | "Low Risk"

    # Meta
    model_used: str = ""
    response_ms: float = 0.0
    fallback: bool = False

    @property
    def best_confidence(self) -> float:
        """Confidence of the highest-confidence pattern, or 0 if none found."""
        return max((p.confidence for p in self.patterns), default=0.0)


@dataclass
class RiskAssessmentResult:
    """Parsed response from the local LLM pre-trade risk assessment (PROMPT 4)."""

    # High-level decision
    overall_assessment: str = "APPROVE"        # "APPROVE" | "CAUTION" | "REJECT"
    risk_level: str = "MEDIUM"                 # "LOW" | "MEDIUM" | "HIGH" | "EXTREME"
    confidence: float = 70.0                   # 0–100

    # Per-checklist pass/fail flags
    risk_reward_ok: bool = True
    account_risk_ok: bool = True
    market_conditions_ok: bool = True
    signal_quality_ok: bool = True
    psychology_ok: bool = True
    timing_ok: bool = True

    # Narrative / next action
    approval_reasons: List[str] = field(default_factory=list)
    cautions: List[str] = field(default_factory=list)
    recommendation: str = "EXECUTE"            # "EXECUTE" | "REDUCE_SIZE" | "SKIP"
    suggested_size_adjustment: float = 1.0     # 0.25 | 0.5 | 0.75 | 1.0 multiplier
    final_notes: str = ""

    # Meta
    model_used: str = ""
    response_ms: float = 0.0
    fallback: bool = False


@dataclass
class AIValidationResult:
    """Parsed response from the local LLM signal-validation call."""

    # High-level decision
    suggested_action: str = "EXECUTE"          # "EXECUTE" | "SKIP" | "WAIT"
    recommendation: str = ""                   # "STRONG BUY CE" | "BUY PE" | "HOLD" | "SKIP"
    confidence: float = 70.0                   # 0–100

    # Signal quality breakdown
    confluence_count: int = 0                  # 0–4 (how many indicators agree)
    time_suitability: bool = True              # False if entering at a bad time window
    market_sentiment: str = "NEUTRAL"          # "BULLISH" | "BEARISH" | "NEUTRAL"

    # Context strings for logging
    reasoning: str = ""
    risks: List[str] = field(default_factory=list)
    opportunities: List[str] = field(default_factory=list)

    # Meta
    model_used: str = ""
    response_ms: float = 0.0
    fallback: bool = False                     # True when AI was unavailable


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class LocalAIService:
    """
    Wraps a locally-hosted LLM (Ollama / LM Studio) to validate trading signals.

    Usage (from engine.py):
        from bot.local_ai_service import LocalAIService
        _ai = LocalAIService()
        result = await _ai.validate_signal(signal, market_context)
        if result.suggested_action == "SKIP":
            return
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.2",
        timeout_seconds: float = 10.0,
        min_confidence: float = 60.0,
        enabled: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout_seconds
        self.min_confidence = min_confidence
        self.enabled = enabled

        # Detect endpoint style from URL
        # Ollama:    http://localhost:11434  →  /api/chat
        # LM Studio: http://localhost:1234   →  /v1/chat/completions (OpenAI-compat)
        if "11434" in base_url or "ollama" in base_url.lower():
            self._endpoint = f"{self.base_url}/api/chat"
            self._style = "ollama"
        else:
            self._endpoint = f"{self.base_url}/v1/chat/completions"
            self._style = "openai"

        logger.info(
            f"[LocalAI] Initialized — enabled={enabled} model={model} "
            f"endpoint={self._endpoint} timeout={timeout_seconds}s"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def warmup(self) -> bool:
        """
        Pre-load the model into RAM by sending a minimal dummy request.
        Call this at bot startup (fire-and-forget) so the first real signal
        doesn't pay the cold-start penalty.

        Returns True if the server responded within timeout, False otherwise.
        """
        if not self.enabled:
            return False
        try:
            logger.info(f"[LocalAI] Warming up {self.model}...")
            dummy = await asyncio.wait_for(
                self._call_llm(
                    'Respond with exactly this JSON and nothing else: {"ok": true}'
                ),
                timeout=self.timeout,
            )
            logger.info(f"[LocalAI] Warm-up complete — {self.model} is loaded in RAM")
            return True
        except asyncio.TimeoutError:
            logger.info(
                f"[LocalAI] Warm-up timed out ({self.timeout:.0f}s) — "
                f"model will load on first real signal"
            )
            return False
        except Exception as exc:
            logger.warning(f"[LocalAI] Warm-up failed ({exc}) — server may not be running")
            return False

    async def validate_signal(
        self,
        signal,                  # TrendSignal — imported lazily to avoid circular import
        market_context: Dict,
    ) -> AIValidationResult:
        """
        Ask the local LLM to validate a trading signal.

        Returns an AIValidationResult with suggested_action ∈
        {"EXECUTE", "SKIP", "WAIT"}.

        Falls back to a neutral EXECUTE result when:
          - AI is disabled in .env
          - The LLM server is unreachable
          - The response cannot be parsed in time
        """
        if not self.enabled:
            return AIValidationResult(
                suggested_action="EXECUTE",
                fallback=True,
                reasoning="AI disabled — proceeding with bot's own filters",
            )

        prompt = self._build_validation_prompt(signal, market_context)

        t0 = asyncio.get_event_loop().time()
        try:
            raw_response = await asyncio.wait_for(
                self._call_llm(prompt),
                timeout=self.timeout,
            )
            elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000
            result = self._parse_response(raw_response)
            result.model_used = self.model
            result.response_ms = elapsed_ms

            logger.info(
                f"[LocalAI] Validation: action={result.suggested_action} "
                f"confidence={result.confidence:.0f}% confluence={result.confluence_count} "
                f"model={self.model} latency={elapsed_ms:.0f}ms"
            )

            # If confidence is below threshold, force SKIP
            if result.suggested_action == "EXECUTE" and result.confidence < self.min_confidence:
                result.suggested_action = "SKIP"
                result.reasoning = (
                    f"Confidence {result.confidence:.0f}% < threshold {self.min_confidence:.0f}%. "
                    + result.reasoning
                )
                logger.info(
                    f"[LocalAI] SKIP — confidence {result.confidence:.0f}% "
                    f"below threshold {self.min_confidence:.0f}%"
                )

            return result

        except asyncio.TimeoutError:
            logger.warning(
                f"[LocalAI] Timeout ({self.timeout}s) — proceeding without AI validation"
            )
            return AIValidationResult(
                suggested_action="EXECUTE",
                fallback=True,
                reasoning=f"AI timeout after {self.timeout:.0f}s — bot filters applied",
            )
        except Exception as exc:
            logger.warning(f"[LocalAI] Validation failed ({exc}) — proceeding without AI")
            return AIValidationResult(
                suggested_action="EXECUTE",
                fallback=True,
                reasoning=f"AI unavailable: {exc}",
            )

    # ------------------------------------------------------------------
    # Public API — PROMPT 3: Chart Pattern Recognition
    # ------------------------------------------------------------------

    async def recognize_patterns(
        self,
        chart_data: Dict,
    ) -> PatternRecognitionResult:
        """
        Ask the local LLM to identify chart patterns from the last 10 5-min candles.

        ``chart_data`` keys expected:
          candles    — list of dicts with keys: timestamp, open, high, low, close, volume
          rsi        — current RSI(14)
          macd       — current MACD value
          bollinger_bands — string description (e.g. "upper=22600 mid=22350 lower=22100")
          atr        — current ATR
          volume     — latest 5-min volume
          r1, pivot, s1  — standard pivot levels

        Falls back to a WAIT/neutral result when AI is unavailable.
        """
        if not self.enabled:
            return PatternRecognitionResult(
                fallback=True,
                recommended_action="WAIT",
            )

        prompt = self._build_pattern_prompt(chart_data)

        t0 = asyncio.get_event_loop().time()
        try:
            raw = await asyncio.wait_for(
                self._call_llm(prompt, max_tokens=512),
                timeout=self.timeout,
            )
            elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000
            result = self._parse_pattern_response(raw)
            result.model_used = self.model
            result.response_ms = elapsed_ms

            logger.info(
                f"[LocalAI] Patterns: action={result.recommended_action} "
                f"assessment={result.overall_assessment} "
                f"strongest='{result.strongest_pattern}' "
                f"patterns={len(result.patterns)} "
                f"best_conf={result.best_confidence:.0f}% "
                f"latency={elapsed_ms:.0f}ms"
            )
            return result

        except asyncio.TimeoutError:
            logger.warning(
                f"[LocalAI] Pattern timeout ({self.timeout}s) — using WAIT fallback"
            )
            return PatternRecognitionResult(
                fallback=True,
                recommended_action="WAIT",
            )
        except Exception as exc:
            logger.warning(f"[LocalAI] Pattern recognition failed ({exc}) — using WAIT fallback")
            return PatternRecognitionResult(
                fallback=True,
                recommended_action="WAIT",
            )

    # ------------------------------------------------------------------
    # Public API — PROMPT 4: Pre-Trade Risk Assessment
    # ------------------------------------------------------------------

    async def assess_trade_risk(
        self,
        signal,           # TrendSignal — imported lazily to avoid circular import
        entry: Dict,      # pre-trade context dict (see _build_risk_prompt)
    ) -> RiskAssessmentResult:
        """
        Ask the local LLM for a final risk-gate assessment before order placement.

        Evaluates R:R ratio, account exposure, timing quality, signal strength,
        and psychological factors.  Returns APPROVE / CAUTION / REJECT.

        Engine behaviour on each recommendation:
          EXECUTE / APPROVE / CAUTION  →  proceed
          REDUCE_SIZE                  →  log warning and proceed at normal sizing
          SKIP / REJECT                →  abort trade

        Falls back to APPROVE (non-blocking) when AI is unavailable.
        """
        if not self.enabled:
            return RiskAssessmentResult(
                fallback=True,
                final_notes="AI disabled — risk check skipped",
            )

        prompt = self._build_risk_prompt(signal, entry)

        t0 = asyncio.get_event_loop().time()
        try:
            raw = await asyncio.wait_for(
                self._call_llm(prompt, max_tokens=512),
                timeout=self.timeout,
            )
            elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000
            result = self._parse_risk_response(raw)
            result.model_used = self.model
            result.response_ms = elapsed_ms

            logger.info(
                f"[LocalAI] Risk: assessment={result.overall_assessment} "
                f"level={result.risk_level} recommendation={result.recommendation} "
                f"size_adj={result.suggested_size_adjustment} "
                f"conf={result.confidence:.0f}% "
                f"latency={elapsed_ms:.0f}ms"
            )
            return result

        except asyncio.TimeoutError:
            logger.warning(
                f"[LocalAI] Risk timeout ({self.timeout}s) — using APPROVE fallback"
            )
            return RiskAssessmentResult(fallback=True)
        except Exception as exc:
            logger.warning(f"[LocalAI] Risk assessment failed ({exc}) — using APPROVE fallback")
            return RiskAssessmentResult(fallback=True)

    # ------------------------------------------------------------------
    # Public API — PROMPT 2: Macro Market Sentiment
    # ------------------------------------------------------------------

    async def analyze_market_sentiment(
        self,
        context: Optional[Dict] = None,
    ) -> MarketSentimentResult:
        """
        Ask the local LLM for a macro market-conditions assessment.

        This is called once per trading session boundary (morning/midday/afternoon)
        rather than per-signal.  The result is cached in the engine and used to
        gate whether any trades fire at all:  UNFAVORABLE → no new entries.

        Falls back to a neutral NEUTRAL/FAVORABLE result when AI is unavailable.
        """
        context = context or {}
        if not self.enabled:
            return MarketSentimentResult(
                fallback=True,
                reasoning="AI disabled — sentiment check skipped",
                session=self._get_session(datetime.now()),
            )

        prompt = self._build_sentiment_prompt(context)

        t0 = asyncio.get_event_loop().time()
        try:
            raw = await asyncio.wait_for(
                self._call_llm(prompt),
                timeout=self.timeout,
            )
            elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000
            result = self._parse_sentiment_response(raw)
            result.model_used = self.model
            result.response_ms = elapsed_ms
            result.session = self._get_session(datetime.now())

            logger.info(
                f"[LocalAI] Sentiment: action={result.action} "
                f"sentiment={result.market_sentiment} strength={result.sentiment_strength:.0f}% "
                f"vix_regime={result.volatility_regime} conditions={result.trading_conditions} "
                f"latency={elapsed_ms:.0f}ms"
            )
            return result

        except asyncio.TimeoutError:
            logger.warning(
                f"[LocalAI] Sentiment timeout ({self.timeout}s) — using neutral fallback"
            )
            return MarketSentimentResult(
                fallback=True,
                reasoning=f"AI timeout after {self.timeout:.0f}s — treating as NEUTRAL",
                session=self._get_session(datetime.now()),
            )
        except Exception as exc:
            logger.warning(f"[LocalAI] Sentiment failed ({exc}) — using neutral fallback")
            return MarketSentimentResult(
                fallback=True,
                reasoning=f"AI unavailable: {exc}",
                session=self._get_session(datetime.now()),
            )

    # ------------------------------------------------------------------
    # Prompt builder  (PROMPT 1 — Signal Validation)
    # ------------------------------------------------------------------

    def _build_validation_prompt(self, signal, market_context: Dict) -> str:
        """
        Construct a structured validation prompt from a TrendSignal.

        The prompt asks the LLM to evaluate the signal across four dimensions:
          1. Trend confluence (EMA, MACD, VWAP, Supertrend)
          2. Momentum quality (RSI, Stoch RSI, Bollinger)
          3. Market phase suitability (time of day, regime)
          4. Risk management (R:R, recent losses, position limits)

        Response MUST be valid JSON — the strict schema is embedded in the prompt.
        """
        now_ist = datetime.now()
        time_str = now_ist.strftime("%H:%M IST")
        hour = now_ist.hour
        minute = now_ist.minute

        # Derived fields for context
        ema9  = getattr(signal, "ema_9",  0.0) or 0.0
        ema21 = getattr(signal, "ema_21", 0.0) or 0.0
        vwap  = getattr(signal, "vwap",   0.0) or 0.0
        boll_upper = getattr(signal, "bollinger_upper", 0.0) or 0.0
        boll_lower = getattr(signal, "bollinger_lower", 0.0) or 0.0
        atr   = getattr(signal, "atr",    0.0) or 0.0
        supert_dir = getattr(signal, "supertrend_direction", "NEUTRAL") or "NEUTRAL"
        stoch_rsi  = getattr(signal, "stoch_rsi", 50.0) or 50.0

        # Helper interpretations
        ema_alignment = (
            "BULLISH (EMA9 > EMA21)" if ema9 > ema21 > 0
            else "BEARISH (EMA9 < EMA21)" if ema9 < ema21 and ema21 > 0
            else "FLAT / UNKNOWN"
        )
        vwap_position = (
            "ABOVE VWAP (bullish intraday bias)" if vwap > 0 and signal.current_price > vwap
            else "BELOW VWAP (bearish intraday bias)" if vwap > 0
            else "VWAP unavailable"
        )
        rsi_label = self._rsi_interpretation(signal.rsi)
        boll_label = self._bollinger_interpretation(
            signal.current_price, boll_lower, boll_upper
        )

        # MACD histogram (positive = bullish momentum, negative = bearish)
        macd_hist = signal.macd - signal.macd_signal
        macd_label = (
            "BULLISH (histogram positive)" if macd_hist > 0
            else "BEARISH (histogram negative)" if macd_hist < 0
            else "FLAT"
        )

        # Market context from engine
        consecutive_losses = market_context.get("consecutive_losses", 0)
        regime = market_context.get("regime", "UNKNOWN")
        vix    = market_context.get("vix", 0.0)
        trades_done_today = market_context.get("trades_done_today", 0)
        max_trades = market_context.get("max_trades_per_day", 5)
        option_type = market_context.get("option_type", "CE")
        macro_sentiment  = market_context.get("macro_sentiment", "UNKNOWN")
        macro_conditions = market_context.get("macro_conditions", "Unknown")

        # Estimated R:R from ATR (1 ATR SL, 2 ATR target → 1:2)
        if atr > 0 and signal.current_price > 0:
            atr_pct = atr / signal.current_price * 100
            rr_note = f"ATR={atr:.1f} ({atr_pct:.2f}% of spot) → estimated SL ~{atr_pct:.1f}%, target ~{atr_pct*2:.1f}% (1:2 R:R)"
        else:
            rr_note = "ATR unavailable — R:R cannot be estimated"

        prompt = f"""You are an expert Indian options trader reviewing a NIFTY/BANKNIFTY intraday signal.

## CURRENT SIGNAL
- Direction  : {signal.trend.value} → BUY {option_type}
- Strength   : {signal.strength:.0f}%
- Index Price: ₹{signal.current_price:,.2f}
- Time       : {time_str}
- Recommendation: {signal.recommendation}

## TECHNICAL INDICATORS
- EMA Alignment    : {ema_alignment}
- MACD Histogram   : {macd_label} ({macd_hist:+.4f})
- RSI (14)         : {signal.rsi:.1f} — {rsi_label}
- Stoch RSI        : {stoch_rsi:.1f}
- VWAP Position    : {vwap_position}
- Bollinger Bands  : {boll_label}
- Supertrend Dir   : {supert_dir}
- SMA 20           : {signal.sma_short:.2f}
- SMA 50           : {signal.sma_long:.2f}

## RISK CONTEXT
- {rr_note}
- India VIX        : {vix:.1f} {"(HIGH >20 — expensive premiums)" if vix > 20 else "(normal)"}
- Consecutive losses this session: {consecutive_losses}
- Trades done today: {trades_done_today} / {max_trades} allowed
- Market regime    : {regime}
- Macro sentiment  : {macro_sentiment} ({macro_conditions} conditions — from session-level AI assessment)

## CRITICAL RULES (non-negotiable)
1. NEVER enter between 9:15–9:20 IST (opening noise — already enforced by bot)
2. AVOID 11:30–12:30 IST (lunch — low volume, wide spreads)
3. AVOID after 14:00 IST (theta decay accelerates, thin liquidity)
4. R:R must be at least 1:2 (risk ₹1 to make ₹2)
5. REDUCE confidence by 15 pts if consecutive_losses >= 2 (loss-fatigue)
6. STRONG BUY requires confluence >= 3 (at least 3 of 4 indicators agree)
7. HOLD/SKIP if confluence < 2
8. HIGH VIX (>25) favours short premium plays — bias toward SKIP for naked option buying

## YOUR TASK
Evaluate the signal. Count the confluence:
  +1 if EMA9 > EMA21 (for CE) or EMA9 < EMA21 (for PE)
  +1 if MACD histogram confirms direction
  +1 if price is above/below VWAP correctly
  +1 if Supertrend direction confirms

Respond ONLY with valid JSON (no markdown, no backticks, no extra text):
{{
  "recommendation": "<STRONG BUY CE|BUY CE|BUY PE|STRONG BUY PE|HOLD|SKIP>",
  "confidence": <0-100>,
  "reasoning": "<one sentence explaining the key factor>",
  "confluence_count": <0-4>,
  "market_sentiment": "<BULLISH|BEARISH|NEUTRAL>",
  "risks": ["<risk 1>", "<risk 2>"],
  "opportunities": ["<opportunity 1>"],
  "time_suitability": <true|false>,
  "suggested_action": "<EXECUTE|SKIP|WAIT>"
}}"""

        return prompt

    # ------------------------------------------------------------------
    # Prompt builder  (PROMPT 3 — Chart Pattern Recognition)
    # ------------------------------------------------------------------

    def _build_pattern_prompt(self, chart_data: Dict) -> str:
        """Build chart pattern recognition prompt (PROMPT 3)."""
        candles = chart_data.get("candles", [])
        return f"""ADVANCED CHART PATTERN RECOGNITION
===================================

Analyze the following 5-minute chart data for NIFTY options trading patterns.

RECENT CANDLE DATA (Last 10 candles):
{self._format_candles(candles[-10:] if len(candles) > 10 else candles)}

TECHNICAL INDICATORS:
- RSI(14): {chart_data.get('rsi', 'N/A')}
- MACD: {chart_data.get('macd', 'N/A')}
- Volume: {chart_data.get('volume', 'N/A')} (compare to 20-day avg)
- Bollinger Bands: {chart_data.get('bollinger_bands', 'N/A')}
- ATR: {chart_data.get('atr', 'N/A')}

SUPPORT/RESISTANCE LEVELS:
- Resistance 1: ₹{chart_data.get('r1', 0):,.0f}
- Pivot: ₹{chart_data.get('pivot', 0):,.0f}
- Support 1: ₹{chart_data.get('s1', 0):,.0f}

========================================
IDENTIFY THESE PATTERNS:
========================================

1. THREE-BAR REVERSAL (Highest probability)
   - Three consecutive bearish candles after up move → downside reversal
   - Three consecutive bullish candles after down move → upside reversal
   - Entry: On the 4th candle if it confirms direction
   - Reliability: 65-75% on NIFTY 5m

2. INSIDE BAR BREAKOUT (High probability)
   - Current candle completely inside previous candle (low > prior low, high < prior high)
   - Signifies consolidation and likely breakout
   - Direction: Breakout typically in direction of prior 2-3 candles
   - Entry: On the breakout candle close
   - Reliability: 60-70%

3. PIN BAR / HAMMER (Reversal signal)
   - Long wick, small body
   - Indicates rejection of that price level
   - Entry: On candle after pin bar if price reverses

4. WEDGE PATTERNS (Continuation or reversal)
   - Rising wedge: Higher highs but lower lows (sell signal)
   - Falling wedge: Lower lows but higher highs (buy signal)

5. ENGULFING PATTERN (Strong reversal)
   - Current candle completely engulfs previous candle
   - Indicates shift in momentum
   - Entry: On candle after engulfing

6. VOLUME SPIKE BREAKOUT (Breakout continuation)
   - Price breaks level with 2x+ average volume
   - High probability continuation
   - Entry: At breakout level or on pullback

========================================
RESPOND IN JSON FORMAT ONLY (no markdown, no backticks, no extra text):
========================================
{{
  "patterns_identified": [
    {{
      "pattern_name": "<name>",
      "confidence": <0-100>,
      "direction": "UP" or "DOWN" or "NEUTRAL",
      "entry_price": <number>,
      "stop_loss": <number>,
      "target": <number>,
      "reliability": "High" or "Medium" or "Low",
      "reasoning": "<why this pattern is likely to work>"
    }}
  ],
  "strongest_pattern": "<pattern name with highest confidence>",
  "overall_assessment": "Bullish" or "Bearish" or "Neutral",
  "recommended_action": "BUY_CE" or "BUY_PE" or "WAIT" or "AVOID",
  "risk_assessment": "High Risk" or "Medium Risk" or "Low Risk"
}}

TRADING CHECKLIST (apply to each pattern before recommending):
- Is the pattern confirmed by volume?
- Is RSI not at an extreme (unless breakout)?
- Is entry near a support/resistance or moving average?
- Is risk/reward at least 1:2?
- Is the pattern formed in a trending market (not plain consolidation)?"""

    @staticmethod
    def _format_candles(candles: list) -> str:
        """
        Format a list of OHLCV candle dicts as a compact text table.

        Accepts dicts with keys: timestamp (or time/date), open, high, low, close, volume.
        Missing keys are rendered as '?'.
        """
        if not candles:
            return "  (no candle data available)"
        lines = ["  #   Time      Open      High      Low       Close     Vol"]
        lines.append("  " + "-" * 65)
        for i, c in enumerate(candles, 1):
            ts = (
                str(c.get("timestamp") or c.get("time") or c.get("date") or "?")[:16]
            )
            o = c.get("open",  c.get("Open",  "?"))
            h = c.get("high",  c.get("High",  "?"))
            l = c.get("low",   c.get("Low",   "?"))
            cl = c.get("close", c.get("Close", "?"))
            v = c.get("volume", c.get("Volume", "?"))
            # Candle direction indicator
            try:
                direction = "↑" if float(cl) >= float(o) else "↓"
            except (TypeError, ValueError):
                direction = " "
            try:
                lines.append(
                    f"  {i:2d}  {ts:<10}  "
                    f"{float(o):>8,.1f}  {float(h):>8,.1f}  "
                    f"{float(l):>8,.1f}  {float(cl):>8,.1f} {direction}  "
                    f"{int(v):>8,}"
                )
            except (TypeError, ValueError):
                lines.append(f"  {i:2d}  {ts:<10}  {o}  {h}  {l}  {cl}  {v}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Pattern response parser  (PROMPT 3)
    # ------------------------------------------------------------------

    def _parse_pattern_response(self, raw: str) -> PatternRecognitionResult:
        """Parse the LLM's JSON pattern response into a PatternRecognitionResult."""
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                cleaned = "\n".join(
                    l for l in lines if not l.startswith("```")
                ).strip()

            data = json.loads(cleaned)

            patterns: List[PatternMatch] = []
            for p in data.get("patterns_identified", []):
                try:
                    patterns.append(PatternMatch(
                        pattern_name=str(p.get("pattern_name", "")),
                        confidence=float(p.get("confidence", 0)),
                        direction=str(p.get("direction", "NEUTRAL")).upper(),
                        entry_price=float(p.get("entry_price", 0) or 0),
                        stop_loss=float(p.get("stop_loss", 0) or 0),
                        target=float(p.get("target", 0) or 0),
                        reliability=str(p.get("reliability", "Low")),
                        reasoning=str(p.get("reasoning", "")),
                    ))
                except (KeyError, ValueError, TypeError):
                    continue

            # Normalise recommended_action
            action_raw = str(data.get("recommended_action", "WAIT")).upper()
            if action_raw not in ("BUY_CE", "BUY_PE", "WAIT", "AVOID"):
                # Derive from overall_assessment if action is garbled
                oa = str(data.get("overall_assessment", "")).lower()
                if "bullish" in oa:
                    action_raw = "BUY_CE"
                elif "bearish" in oa:
                    action_raw = "BUY_PE"
                else:
                    action_raw = "WAIT"

            return PatternRecognitionResult(
                patterns=patterns,
                strongest_pattern=str(data.get("strongest_pattern", "")),
                overall_assessment=str(data.get("overall_assessment", "Neutral")),
                recommended_action=action_raw,
                risk_assessment=str(data.get("risk_assessment", "Medium Risk")),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(f"[LocalAI] Pattern parse error ({exc}) — raw: {raw[:200]}")
            raw_upper = raw.upper()
            if "AVOID" in raw_upper or "HIGH RISK" in raw_upper:
                return PatternRecognitionResult(
                    fallback=True,
                    recommended_action="AVOID",
                    risk_assessment="High Risk",
                )
            return PatternRecognitionResult(
                fallback=True,
                recommended_action="WAIT",
            )

    # ------------------------------------------------------------------
    # Prompt builder + parser  (PROMPT 4 — Pre-Trade Risk Assessment)
    # ------------------------------------------------------------------

    def _build_risk_prompt(self, signal, entry: Dict) -> str:
        """Build pre-trade risk assessment prompt (PROMPT 4)."""
        _ep  = entry.get("entry_price", 0)
        _sl  = entry.get("stop_loss", 0)
        _tgt = entry.get("target", 0)
        _risk   = abs(_ep - _sl)
        _reward = abs(_tgt - _ep)
        _rr = _reward / max(_risk, 0.01)
        _pos_risk = (_risk * entry.get("quantity", 1) * 100) / max(entry.get("account_balance", 1), 1) * 100

        return f"""
PRE-TRADE RISK ASSESSMENT & FINAL APPROVAL
===========================================

PROPOSED TRADE:
- Option Type: {entry.get('option_type', '?')} (Call or Put)
- Direction: {entry.get('direction', 'Unknown')} ({"Long Call = Bullish" if entry.get('option_type') == 'CE' else 'Long Put = Bearish'})
- Strike Price: \u20b9{entry.get('strike', 0):,.0f}
- Entry Price: \u20b9{_ep:,.2f}
- Stop Loss: \u20b9{_sl:,.2f}
- Target: \u20b9{_tgt:,.2f}

RISK CALCULATION:
- Max Loss Per Trade: \u20b9{_risk:,.2f}
- Max Gain Per Trade: \u20b9{_reward:,.2f}
- Risk/Reward Ratio: 1:{_rr:.2f}
- Position Size: {entry.get('quantity', 1)} lots
- Notional Value: \u20b9{_ep * entry.get('quantity', 1) * 100:,.0f}

SIGNAL QUALITY:
- Technical Strength: {signal.strength}%
- Trend: {signal.trend.value}
- RSI: {signal.rsi:.0f}
- Confluence (# of indicators aligned): {entry.get('confluence_count', 0)}/4

ACCOUNT STATUS - CRITICAL!:
- Account Balance: \u20b9{entry.get('account_balance', 0):,.0f}
- Today's P&L: \u20b9{entry.get('daily_pnl', 0):+,.0f}
- Daily Loss Limit: \u20b9{entry.get('max_daily_loss', 0):,.0f}
- Remaining Capital: \u20b9{max(0, entry.get('account_balance', 0) - abs(entry.get('daily_pnl', 0))):,.0f}
- Position % of Risk: {_pos_risk:.2f}%
- Consecutive Losses Today: {entry.get('consecutive_losses', 0)}
- Win Rate Today: {entry.get('today_win_rate', 0):.0f}%

========================================
RISK CHECKLIST - ALL MUST PASS!
========================================

1. **Risk/Reward Check**:
   - Is ratio at least 1:1.5? YES/NO
   - Ideally 1:2 or better? YES/NO

2. **Account Risk Check**:
   - Is position risk < 2% of account balance? YES/NO
   - Can we afford 3 consecutive losses at this size? YES/NO
   - Is daily P&L already down more than 50% of max loss limit? YES/NO

3. **Market Condition Check**:
   - Are we in a choppy market (NEUTRAL trend)? YES/NO
   - If YES: Is signal strength > 70%? YES/NO
   - Are we in market open chaos (9:15-9:20)? YES/NO
   - Are we in lunch hour (11:30-12:30)? YES/NO
   - Are we past optimal entry time (>14:00)? YES/NO

4. **Signal Quality Check**:
   - Is signal strength >= 60%? YES/NO
   - Are at least 2-3 indicators aligned? YES/NO
   - Is this following recent winning setups, not revenge trading? YES/NO

5. **Psychological Check**:
   - Have we had 2+ losses already? YES/NO
   - If YES: Are we trying to make it back (revenge trading)? YES/NO
   - Is account fatigue affecting judgment? YES/NO

6. **Timing Check**:
   - Is this entry at support/resistance or moving average? YES/NO
   - Is this at a natural pullback level? YES/NO
   - Is entry on a fresh candle, not mid-candle? YES/NO

========================================
RESPOND IN JSON FORMAT:
========================================
{{
  "overall_assessment": "APPROVE" / "CAUTION" / "REJECT",
  "risk_level": "LOW" / "MEDIUM" / "HIGH" / "EXTREME",
  "confidence": 0-100,
  "checklist_results": {{
    "risk_reward_ok": true/false,
    "account_risk_ok": true/false,
    "market_conditions_ok": true/false,
    "signal_quality_ok": true/false,
    "psychology_ok": true/false,
    "timing_ok": true/false
  }},
  "approval_reasons": ["reason1", "reason2"],
  "cautions": ["caution1", "caution2"],
  "recommendation": "EXECUTE" / "REDUCE_SIZE" / "SKIP",
  "suggested_size_adjustment": 1.0 or 0.75 or 0.5 or 0.25,
  "final_notes": "Any additional warnings or notes"
}}

REJECTION CRITERIA (Any of these = REJECT):
- Risk/Reward < 1:1
- Position risk > 3% of account
- Already down > 50% of daily loss limit
- In market open chaos or lunch hour
- Signal strength < 50%
- Consecutive losses >= 3 (take a break!)
- Account balance < 5x position risk

APPROVAL CRITERIA (All must pass):
- Risk/Reward >= 1:1.5
- Position risk <= 2% of account
- Signal strength >= 60%
- At least 2 indicators aligned
- Not in choppy/consolidating market
- Clear entry point at support/moving average
"""

    def _parse_risk_response(self, raw: str) -> RiskAssessmentResult:
        """Parse the LLM's JSON risk assessment response into a RiskAssessmentResult."""
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                cleaned = "\n".join(
                    l for l in lines if not l.startswith("```")
                ).strip()

            data = json.loads(cleaned)

            checklist = data.get("checklist_results", {})

            # Normalise overall_assessment
            oa = str(data.get("overall_assessment", "APPROVE")).upper()
            if oa not in ("APPROVE", "CAUTION", "REJECT"):
                oa = "CAUTION"

            # Normalise recommendation
            rec = str(data.get("recommendation", "EXECUTE")).upper()
            if rec not in ("EXECUTE", "REDUCE_SIZE", "SKIP"):
                rec = "EXECUTE"

            # Normalise risk level
            rl = str(data.get("risk_level", "MEDIUM")).upper()
            if rl not in ("LOW", "MEDIUM", "HIGH", "EXTREME"):
                rl = "MEDIUM"

            # Clamp size adjustment to allowed values
            raw_adj = float(data.get("suggested_size_adjustment", 1.0) or 1.0)
            if raw_adj >= 0.9:
                adj = 1.0
            elif raw_adj >= 0.65:
                adj = 0.75
            elif raw_adj >= 0.38:
                adj = 0.5
            else:
                adj = 0.25

            return RiskAssessmentResult(
                overall_assessment=oa,
                risk_level=rl,
                confidence=float(data.get("confidence", 70) or 70),
                risk_reward_ok=bool(checklist.get("risk_reward_ok", True)),
                account_risk_ok=bool(checklist.get("account_risk_ok", True)),
                market_conditions_ok=bool(checklist.get("market_conditions_ok", True)),
                signal_quality_ok=bool(checklist.get("signal_quality_ok", True)),
                psychology_ok=bool(checklist.get("psychology_ok", True)),
                timing_ok=bool(checklist.get("timing_ok", True)),
                approval_reasons=list(data.get("approval_reasons", [])),
                cautions=list(data.get("cautions", [])),
                recommendation=rec,
                suggested_size_adjustment=adj,
                final_notes=str(data.get("final_notes", "")),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(f"[LocalAI] Risk parse error ({exc}) — raw: {raw[:200]}")
            raw_upper = raw.upper()
            if "REJECT" in raw_upper or "EXTREME" in raw_upper:
                return RiskAssessmentResult(
                    fallback=True,
                    overall_assessment="REJECT",
                    risk_level="EXTREME",
                    recommendation="SKIP",
                )
            return RiskAssessmentResult(fallback=True)

    # ------------------------------------------------------------------
    # Prompt builder  (PROMPT 2 — Macro Market Sentiment)
    # ------------------------------------------------------------------

    def _build_sentiment_prompt(self, context: Optional[Dict] = None) -> str:
        """Build market sentiment prompt — MACRO ANALYSIS (PROMPT 2)."""
        context = context or {}
        now = datetime.now()
        day_name = now.strftime("%A")
        time_str = now.strftime("%H:%M IST")

        return f"""INDIAN MARKET SENTIMENT ASSESSMENT
==================================

Current Time: {time_str} on {day_name}, {now.strftime('%d-%b-%Y')}
India VIX: {context.get('vix', 'N/A')}

MARKET PATTERNS BY TIME:

Morning Session (9:15-11:30):
- Characteristic: Gap establishment, ORB breakouts, overnight sentiment
- VIX typically: High 9:15-9:30, settles 9:30-10:00
- Best for: Directional breakout trades (ORB strategy)
- Avoid: Fade trades, oversold bounces

Midday (11:30-13:00):
- Characteristic: Lunch hour, thin liquidity, consolidation
- Typical pattern: Mean-reversion, false breakouts
- Best for: Sitting out or very high probability setups only
- Risk: Wide spreads, slippage

Afternoon (13:00-15:00):
- Characteristic: Institutional activity, VWAP mean-reversion
- Volatility: Moderate, directional clarity
- Best for: VWAP trades, trend continuation
- Risk: Mixed signals, indecision

Close (15:00-15:30):
- Characteristic: EOD momentum, forced unwinding
- Liquidity: Declining, spreads widening
- Best for: EOD closing momentum trades (experimental)
- Risk: Execution difficulty, wide spreads

ASSESS THESE FACTORS:

1. **Overnight Gap Impact**:
   - Large gap (>1%): Likely to fill or reverse mid-session
   - Small gap (<0.5%): Likely to continue direction
   - No gap: Market dependent on overnight macros

2. **Volatility Assessment (VIX)**:
   - VIX < 15: Low volatility, choppy trading, favor mean-reversion
   - VIX 15-20: Normal volatility, both breakouts and mean-reversion work
   - VIX 20-25: High volatility, favor breakouts, avoid reversals
   - VIX > 25: Extremely high, favor defensive plays, small positions

3. **Time of Day Effect**:
   - What time is it now? {time_str}
   - Current session: {self._get_session(now)}
   - Typical behavior: {self._get_session_behavior(now)}

4. **Weekly Pattern**:
   - {day_name} typically sees: {self._get_weekly_pattern(day_name)}

5. **Macroeconomic Events**:
   - Today's events: {context.get('macro_events', 'None known')}
   - RBI announcements, FII flows, budget data?

========================================
RESPOND IN THIS JSON FORMAT ONLY (no markdown, no backticks, no extra text):
========================================
{{
  "market_sentiment": "BULLISH" or "BEARISH" or "NEUTRAL",
  "sentiment_strength": <0-100>,
  "volatility_regime": "LOW" or "NORMAL" or "HIGH" or "EXTREME",
  "trading_conditions": "Excellent" or "Good" or "Fair" or "Poor",
  "recommended_strategy": "Breakout" or "Mean-Reversion" or "Range-Trading" or "Caution-Advised",
  "vix_interpretation": "<one sentence interpreting current VIX level>",
  "confidence": <0-100>,
  "reasoning": "<1-2 sentence summary of conditions>",
  "action": "FAVORABLE" or "NEUTRAL" or "UNFAVORABLE"
}}

SENTIMENT SCORING GUIDE:
- BULLISH + High confidence = Go ahead with CE trades
- BEARISH + High confidence = Go ahead with PE trades
- NEUTRAL = Wait for clearer directional setup
- Mixed signals = Use only high confluence setups (3+ indicators)"""

    @staticmethod
    def _get_session(now: datetime) -> str:
        """Return the name of the current trading session."""
        h, m = now.hour, now.minute
        total = h * 60 + m
        if total < 9 * 60 + 15:
            return "Pre-Market"
        if total <= 11 * 60 + 30:
            return "Morning"
        if total <= 13 * 60:
            return "Midday"
        if total <= 15 * 60:
            return "Afternoon"
        return "Close / Post-Market"

    @staticmethod
    def _get_session_behavior(now: datetime) -> str:
        """Return a short description of typical behaviour for the current session."""
        h, m = now.hour, now.minute
        total = h * 60 + m
        if total < 9 * 60 + 15:
            return "Pre-market — no live Indian data yet"
        if total <= 9 * 60 + 30:
            return "First 15 min — extreme volatility, algo rebalancing, avoid entries"
        if total <= 11 * 60 + 30:
            return "Morning session — ORB/gap breakouts, trending moves, directional clarity"
        if total <= 13 * 60:
            return "Lunch hour — thin liquidity, false breakouts, widen spreads, sit out"
        if total <= 14 * 60:
            return "Afternoon — institutional VWAP rebalancing, mean-reversion opportunities"
        if total <= 15 * 60:
            return "Late afternoon — EOD momentum builds, watch for closing push"
        return "Post-market — no actionable signals"

    @staticmethod
    def _get_weekly_pattern(day_name: str) -> str:
        """Return the typical weekly pattern note for the given day."""
        patterns = {
            "Monday": "Gap risk from weekend news; trend often set by 10:30; weekly options start expiry countdown",
            "Tuesday": "Follow-through from Monday; relatively stable; good for breakout continuation",
            "Wednesday": "Mid-week consolidation common; mixed signals; favor range-bound strategies",
            "Thursday": "Weekly NIFTY options expiry — high gamma, sharp moves near strikes, avoid OTM buys",
            "Friday": "Monthly expiry risk if last Friday; profit-booking/short-covering into close; gap risk over weekend",
        }
        return patterns.get(day_name, "Normal trading day")

    # ------------------------------------------------------------------
    # Sentiment response parser  (PROMPT 2)
    # ------------------------------------------------------------------

    def _parse_sentiment_response(self, raw: str) -> MarketSentimentResult:
        """Parse the LLM's JSON sentiment response into a MarketSentimentResult."""
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                cleaned = "\n".join(
                    l for l in lines if not l.startswith("```")
                ).strip()

            data = json.loads(cleaned)

            # Normalise action field
            action_raw = str(data.get("action", "NEUTRAL")).upper()
            if action_raw not in ("FAVORABLE", "NEUTRAL", "UNFAVORABLE"):
                # Derive from trading_conditions if action is garbled
                cond = str(data.get("trading_conditions", "")).lower()
                if "excellent" in cond or "good" in cond:
                    action_raw = "FAVORABLE"
                elif "poor" in cond:
                    action_raw = "UNFAVORABLE"
                else:
                    action_raw = "NEUTRAL"

            return MarketSentimentResult(
                action=action_raw,
                market_sentiment=str(data.get("market_sentiment", "NEUTRAL")).upper(),
                sentiment_strength=float(data.get("sentiment_strength", 50)),
                volatility_regime=str(data.get("volatility_regime", "NORMAL")).upper(),
                trading_conditions=str(data.get("trading_conditions", "Fair")),
                recommended_strategy=str(data.get("recommended_strategy", "Range-Trading")),
                vix_interpretation=str(data.get("vix_interpretation", "")),
                confidence=float(data.get("confidence", 50)),
                reasoning=str(data.get("reasoning", "")),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(f"[LocalAI] Sentiment parse error ({exc}) — raw: {raw[:200]}")
            raw_upper = raw.upper()
            if "UNFAVORABLE" in raw_upper or "POOR" in raw_upper or "EXTREME" in raw_upper:
                return MarketSentimentResult(
                    action="UNFAVORABLE",
                    fallback=True,
                    reasoning="AI said unfavorable (parse failed — raw response scanned)",
                )
            return MarketSentimentResult(
                fallback=True,
                reasoning="AI response unparseable — treating as NEUTRAL",
            )

    # ------------------------------------------------------------------
    # LLM HTTP call
    # ------------------------------------------------------------------

    async def _call_llm(self, prompt: str, max_tokens: int = 256) -> str:
        """Send prompt to local LLM and return raw text response."""
        async with httpx.AsyncClient(timeout=self.timeout + 2) as client:
            if self._style == "ollama":
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": "json",          # Ollama JSON mode — forces JSON output
                    "options": {
                        "temperature": 0.1,    # Low temperature for consistent structured output
                        "num_predict": max_tokens,
                    },
                }
                resp = await client.post(self._endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data["message"]["content"]

            else:  # OpenAI-compatible (LM Studio)
                payload = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a trading signal validator. Always respond with valid JSON only.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                }
                resp = await client.post(self._endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------
    # Response parser
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> AIValidationResult:
        """Parse the LLM's JSON response into an AIValidationResult."""
        try:
            # Strip any accidental markdown fences the model added
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                cleaned = "\n".join(
                    l for l in lines if not l.startswith("```")
                ).strip()

            data = json.loads(cleaned)

            # Normalise suggested_action
            action_raw = str(data.get("suggested_action", "EXECUTE")).upper()
            if action_raw not in ("EXECUTE", "SKIP", "WAIT"):
                # Derive from recommendation if action field is garbled
                rec = str(data.get("recommendation", "")).upper()
                if "SKIP" in rec or "HOLD" in rec:
                    action_raw = "SKIP"
                else:
                    action_raw = "EXECUTE"

            return AIValidationResult(
                suggested_action=action_raw,
                recommendation=str(data.get("recommendation", "")),
                confidence=float(data.get("confidence", 70)),
                confluence_count=int(data.get("confluence_count", 0)),
                time_suitability=bool(data.get("time_suitability", True)),
                market_sentiment=str(data.get("market_sentiment", "NEUTRAL")),
                reasoning=str(data.get("reasoning", "")),
                risks=list(data.get("risks", [])),
                opportunities=list(data.get("opportunities", [])),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(f"[LocalAI] Response parse error ({exc}) — raw: {raw[:200]}")
            # Heuristic fallback parse: scan for SKIP/EXECUTE keywords in raw text
            raw_upper = raw.upper()
            if "SKIP" in raw_upper or "HOLD" in raw_upper or "AVOID" in raw_upper:
                return AIValidationResult(
                    suggested_action="SKIP",
                    fallback=True,
                    reasoning=f"AI said skip (parse failed — raw response scanned)",
                )
            return AIValidationResult(
                suggested_action="EXECUTE",
                fallback=True,
                reasoning="AI response unparseable — proceeding with bot's own filters",
            )

    # ------------------------------------------------------------------
    # Helper interpreters
    # ------------------------------------------------------------------

    @staticmethod
    def _rsi_interpretation(rsi: float) -> str:
        """Human-readable RSI zone label."""
        if rsi >= 80:
            return "EXTREMELY OVERBOUGHT — reversal risk"
        if rsi >= 70:
            return "OVERBOUGHT — caution for CE"
        if rsi >= 55:
            return "BULLISH MOMENTUM"
        if rsi >= 45:
            return "NEUTRAL"
        if rsi >= 30:
            return "BEARISH MOMENTUM"
        if rsi >= 20:
            return "OVERSOLD — caution for PE"
        return "EXTREMELY OVERSOLD — reversal risk"

    @staticmethod
    def _bollinger_interpretation(price: float, lower: float, upper: float) -> str:
        """Human-readable Bollinger position label."""
        if not (lower > 0 and upper > lower):
            return "BANDS unavailable"
        band_width = upper - lower
        mid = (upper + lower) / 2
        if price >= upper:
            return f"AT/ABOVE UPPER BAND ({price:.1f} ≥ {upper:.1f}) — potential CE exhaust"
        if price <= lower:
            return f"AT/BELOW LOWER BAND ({price:.1f} ≤ {lower:.1f}) — potential PE exhaust"
        pct_position = (price - lower) / band_width * 100
        if pct_position >= 60:
            return f"UPPER HALF of bands ({pct_position:.0f}%) — bullish bias"
        if pct_position <= 40:
            return f"LOWER HALF of bands ({pct_position:.0f}%) — bearish bias"
        return f"MID BANDS ({pct_position:.0f}%) — neutral"


# ---------------------------------------------------------------------------
# Module-level singleton (created lazily from engine.py)
# ---------------------------------------------------------------------------

_ai_service_instance: Optional[LocalAIService] = None


def get_ai_service() -> LocalAIService:
    """
    Return the module-level LocalAIService singleton.
    Config is read from environment via config.settings.ai.
    """
    global _ai_service_instance
    if _ai_service_instance is None:
        try:
            from config import settings as _cfg
            ai_cfg = _cfg.ai
            _ai_service_instance = LocalAIService(
                base_url=ai_cfg.base_url,
                model=ai_cfg.model,
                timeout_seconds=ai_cfg.timeout_seconds,
                min_confidence=ai_cfg.min_confidence,
                enabled=ai_cfg.enabled,
            )
        except Exception as exc:
            logger.warning(f"[LocalAI] Config load failed ({exc}) — using defaults")
            _ai_service_instance = LocalAIService()
    return _ai_service_instance
