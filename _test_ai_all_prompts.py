"""Quick end-to-end test for all 4 AI prompts against the live Ollama server."""
import asyncio
import sys
sys.path.insert(0, ".")

from bot.local_ai_service import LocalAIService
from bot.trend_analyzer import TrendSignal, Trend
from datetime import datetime

SVC = LocalAIService(enabled=True, timeout_seconds=120)

SIG = TrendSignal(
    trend=Trend.BULLISH, strength=72.0, current_price=22450.0,
    sma_short=22420.0, sma_long=22300.0, rsi=61.5,
    macd=12.3, macd_signal=9.8, timestamp=datetime.now(),
    recommendation="BUY CE", details={},
    ema_9=22460.0, ema_21=22380.0, vwap=22400.0,
    bollinger_upper=22600.0, bollinger_lower=22100.0, bollinger_mid=22350.0,
    atr=110.0, supertrend_direction="BULLISH", stoch_rsi=62.0,
)

SEP = "-" * 52
PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


async def run():
    # ── PROMPT 1 ────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("PROMPT 1  —  Signal Validation")
    print(SEP)
    r1 = await SVC.validate_signal(SIG, {
        "option_type": "CE", "regime": "TRENDING UP", "vix": 15.2,
        "consecutive_losses": 0, "trades_done_today": 1, "max_trades_per_day": 5,
    })
    print(f"  action     : {r1.suggested_action}")
    print(f"  confidence : {r1.confidence:.0f}%")
    print(f"  confluence : {r1.confluence_count}/4")
    print(f"  sentiment  : {r1.market_sentiment}")
    print(f"  fallback   : {r1.fallback}")
    print(f"  latency    : {r1.response_ms:.0f}ms")
    check("P1 not fallback", not r1.fallback)
    check("P1 action valid", r1.suggested_action in ("EXECUTE", "SKIP", "WAIT"),
          r1.suggested_action)
    check("P1 confidence >0", r1.confidence > 0, f"{r1.confidence:.0f}%")

    # ── PROMPT 2 ────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("PROMPT 2  —  Macro Market Sentiment")
    print(SEP)
    r2 = await SVC.analyze_market_sentiment({"vix": 15.8})
    print(f"  action     : {r2.action}")
    print(f"  sentiment  : {r2.market_sentiment} ({r2.sentiment_strength:.0f}%)")
    print(f"  vix_regime : {r2.volatility_regime}")
    print(f"  conditions : {r2.trading_conditions}")
    print(f"  fallback   : {r2.fallback}")
    print(f"  latency    : {r2.response_ms:.0f}ms")
    check("P2 not fallback", not r2.fallback)
    check("P2 action valid", r2.action in ("FAVORABLE", "NEUTRAL", "UNFAVORABLE"),
          r2.action)
    check("P2 sentiment >0%", r2.sentiment_strength > 0,
          f"{r2.sentiment_strength:.0f}%")

    # ── PROMPT 3 ────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("PROMPT 3  —  Chart Pattern Recognition")
    print(SEP)
    chart = {
        "candles": [
            {"timestamp": "10:00", "open": 22400, "high": 22460,
             "low": 22390, "close": 22445, "volume": 120000},
            {"timestamp": "10:05", "open": 22445, "high": 22480,
             "low": 22440, "close": 22475, "volume": 135000},
            {"timestamp": "10:10", "open": 22475, "high": 22510,
             "low": 22465, "close": 22500, "volume": 142000},
        ],
        "rsi": "61.5",
        "macd": "12.3000 / signal=9.8000",
        "bollinger_bands": "upper=22600 mid=22350 lower=22100",
        "atr": "110.0",
        "volume": 142000,
        "r1": 22520.0,
        "pivot": 22430.0,
        "s1": 22360.0,
    }
    r3 = await SVC.recognize_patterns(chart)
    strongest = r3.strongest_pattern if r3.strongest_pattern else "(none)"
    print(f"  action     : {r3.recommended_action}")
    print(f"  assessment : {r3.overall_assessment}")
    print(f"  strongest  : {strongest}")
    print(f"  risk       : {r3.risk_assessment}")
    print(f"  best_conf  : {r3.best_confidence:.0f}%")
    print(f"  fallback   : {r3.fallback}")
    print(f"  latency    : {r3.response_ms:.0f}ms")
    check("P3 not fallback", not r3.fallback)
    check("P3 action valid",
          r3.recommended_action in ("BUY_CE", "BUY_PE", "WAIT", "AVOID"),
          r3.recommended_action)

    # ── PROMPT 4 ────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("PROMPT 4  —  Pre-Trade Risk Assessment")
    print(SEP)
    entry = {
        "option_type": "CE", "direction": "BULLISH",
        "strike": 22500, "entry_price": 45.0, "stop_loss": 22.5, "target": 90.0,
        "quantity": 1, "account_balance": 100000, "daily_pnl": -500,
        "max_daily_loss": 6000, "consecutive_losses": 0,
        "today_win_rate": 60, "confluence_count": 4,
    }
    r4 = await SVC.assess_trade_risk(SIG, entry)
    print(f"  assessment : {r4.overall_assessment}")
    print(f"  risk_level : {r4.risk_level}")
    print(f"  recommend  : {r4.recommendation}")
    print(f"  size_adj   : x{r4.suggested_size_adjustment:.2f}")
    print(f"  confidence : {r4.confidence:.0f}%")
    print(f"  rr_ok      : {r4.risk_reward_ok}")
    print(f"  acct_ok    : {r4.account_risk_ok}")
    print(f"  fallback   : {r4.fallback}")
    print(f"  latency    : {r4.response_ms:.0f}ms")
    check("P4 not fallback", not r4.fallback)
    check("P4 assessment valid",
          r4.overall_assessment in ("APPROVE", "CAUTION", "REJECT"),
          r4.overall_assessment)
    check("P4 recommendation valid",
          r4.recommendation in ("EXECUTE", "REDUCE_SIZE", "SKIP"),
          r4.recommendation)

    # ── Summary ─────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    total = len(PASS) + len(FAIL)
    print(f"RESULT: {len(PASS)}/{total} checks passed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
    else:
        print("ALL CHECKS PASSED")
    print(SEP)


asyncio.run(run())
