"""
Trading Bot Engine

Main orchestration module that coordinates:
- Market trend analysis
- Order execution
- Position monitoring
- Chat command processing
"""

import asyncio
import re
from typing import Optional, Callable, List, Dict
from datetime import datetime, timedelta, date
from enum import Enum
from dataclasses import dataclass
from loguru import logger
import httpx

import sys
sys.path.append('..')
from config import settings
from bot.trend_analyzer import TrendAnalyzer, TrendSignal, Trend
from bot.order_manager import OrderManager
from bot.instructions import InstructionManager, ActionType, instruction_manager
from bot.market_research import market_research
from bot.backtester import backtester
from bot.market_regime import regime_detector
from bot.theta_clock import theta_clock
from bot.multi_timeframe import mtf_engine
from bot.trade_journal import trade_journal
from bot.index_config import IndexConfig, NIFTY, get_index, list_indices
from bot.orb_strategy import ORBStrategy, ORBSignal
from bot.vwap_strategy import VWAPStrategy, VWAPSignal
from bot.gap_detector import gap_detector, GapType
from browser.factory import create_broker


class BotState(Enum):
    """Bot operational states"""
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


@dataclass
class BotStatus:
    """Current bot status"""
    state: BotState
    auto_trade_enabled: bool
    last_analysis: Optional[datetime]
    last_signal: Optional[TrendSignal]
    open_positions: int
    daily_pnl: float
    message: str


class TradingBot:
    """
    Main trading bot that orchestrates all components.
    
    Features:
    - Periodic trend analysis
    - Automatic trade execution based on signals
    - Manual trade commands via chat
    - Position monitoring and risk management
    """
    
    def __init__(self):
        self.config = settings
        
        # Active index (default NIFTY)
        self._active_index: IndexConfig = NIFTY
        
        # Initialize components
        self.analyzer = TrendAnalyzer(index_config=self._active_index)
        self.kite = create_broker()
        self.order_manager: Optional[OrderManager] = None
        self.instructions = instruction_manager
        self.research = market_research
        
        # State management
        self._state = BotState.STOPPED
        self._auto_trade = settings.trading.auto_trade_enabled
        self._analysis_task: Optional[asyncio.Task] = None
        self._position_monitor_task: Optional[asyncio.Task] = None  # NEW: Position monitoring
        
        # Event callbacks (for WebSocket updates)
        self._message_callbacks: List[Callable] = []
        
        # Last analysis
        self._last_signal: Optional[TrendSignal] = None
        self._last_analysis_time: Optional[datetime] = None
        
        # Cache for current prices (updated by position monitor)
        self._current_prices: Dict[str, float] = {}

        # Cached P&L (updated every position-monitor cycle — no extra broker API calls)
        self._cached_daily_pnl: float = 0.0
        self._cached_current_pnl: float = 0.0
        self._pnl_last_updated: Optional[datetime] = None

        # ORB one-trade-per-day guard at engine level.
        # Uses a date (not bool) so it auto-resets on a new calendar day
        # and survives ORB index-switch resets and bot restarts.
        self._orb_triggered_date: Optional[date] = None
        # Same guard for VWAP — prevents re-entry on restarts (1 VWAP trade per day max)
        self._vwap_triggered_date: Optional[date] = None

        # Regime cache — re-fetch at most once per 30 min (daily data, expensive)
        self._cached_regime = None
        self._last_regime_time: Optional[datetime] = None
        self._regime_cache_seconds: int = 30 * 60  # 30 minutes
        
        # Watch feature - continuous strike monitoring
        self._watch_task: Optional[asyncio.Task] = None
        self._watch_strike: Optional[int] = None
        self._watch_option_type: Optional[str] = None
        self._watch_expiry: Optional[str] = None
        self._watch_interval: int = 30  # seconds between updates

        # ORB strategy (Opening Range Breakout)
        self.orb = ORBStrategy(index_config=self._active_index)

        # VWAP Mean Reversion strategy (active in RANGING regime)
        self.vwap_strat = VWAPStrategy(index_config=self._active_index)

        # Gap detector (runs once per day at market open)
        self._gap_detector = gap_detector
        self._gap_traded_today: bool = False   # prevents duplicate gap trades
        
    def register_message_callback(self, callback: Callable):
        """Register callback for bot messages (to send to chat)"""
        self._message_callbacks.append(callback)
    
    async def _broadcast_message(self, message: str, msg_type: str = "info"):
        """Send message to all registered callbacks"""
        for callback in self._message_callbacks:
            try:
                await callback({
                    "type": msg_type,
                    "message": message,
                    "timestamp": datetime.now().isoformat()
                })
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    async def _send_telegram_alert(self, message: str, alert_type: str = "info"):
        """Send alert to Telegram"""
        if not self.config.alert.telegram_enabled:
            return
        
        if not self.config.alert.telegram_bot_token or not self.config.alert.telegram_chat_id:
            logger.warning("Telegram not properly configured")
            return
        
        try:
            bot_token = self.config.alert.telegram_bot_token
            chat_id = self.config.alert.telegram_chat_id
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": message,
                    },
                    timeout=10
                )
                
                if response.status_code != 200:
                    logger.error(f"Telegram send failed: {response.status_code} - {response.text}")
                else:
                    logger.debug(f"Telegram alert sent: {alert_type}")
                    
        except Exception as e:
            logger.error(f"Error sending Telegram alert: {e}")
    
    async def initialize(self):
        """Initialize all bot components"""
        logger.info("Initializing Trading Bot...")
        self._state = BotState.STARTING
        
        await self._broadcast_message("Initializing bot...", "system")
        
        try:
            # Initialize browser
            await self.kite.initialize(headless=False)
            
            # Initialize order manager
            self.order_manager = OrderManager(self.kite)

            # Recover any open positions from broker API (survives restarts)
            await self._recover_open_positions()

            # Always start position monitor so recovered positions get SL/target/auto-close
            # monitoring immediately, regardless of whether auto-trade or analysis loop is on.
            await self.start_position_monitor()

            self._state = BotState.RUNNING
            await self._broadcast_message("Bot initialized successfully!", "success")
            logger.info("Bot initialization complete")
            
        except Exception as e:
            self._state = BotState.ERROR
            error_msg = f"Initialization failed: {e}"
            logger.error(error_msg)
            await self._broadcast_message(error_msg, "error")
            raise

    async def _recover_open_positions(self):
        """
        Re-populate order_manager._positions from the broker's live day positions
        after a restart. This ensures SL/target/auto-close monitoring resumes
        for any trades placed before the restart.
        """
        try:
            pos_data = await self.kite.get_kite_positions()
            recovered = 0
            closed_trades_today = 0
            for p in pos_data.get("day", []):
                qty = p.get("quantity", 0)
                buy_qty = p.get("buy_quantity", 0) or 0
                if qty == 0 and buy_qty > 0:
                    # Count today's already-closed trades to restore daily limit
                    closed_trades_today += 1
                    continue
                if qty == 0:
                    continue  # not a traded position
                symbol = p.get("tradingsymbol", "")
                avg_price = float(p.get("average_price") or p.get("buy_price") or 0)
                if not symbol or avg_price <= 0:
                    continue
                # Determine option type from symbol suffix
                from browser.dhan import OptionType, OrderType
                opt_type = OptionType.CE if symbol.upper().endswith("CE") else OptionType.PE
                # Parse the real strike from Dhan tradingsymbol: NIFTY-Mar2026-23250-CE
                strike_match = re.search(r'-(\d+)-(CE|PE)$', symbol, re.IGNORECASE)
                strike = int(strike_match.group(1)) if strike_match else 0
                trade_id = f"RECOVERED_{symbol}"
                if trade_id in self.order_manager._positions:
                    continue  # already tracked
                from bot.order_manager import TradeRecord
                trade = TradeRecord(
                    trade_id=trade_id,
                    symbol=symbol,
                    option_type=opt_type,
                    strike=strike,
                    order_type=OrderType.BUY,
                    quantity=abs(qty),
                    price=avg_price,
                    timestamp=datetime.now(),
                    status="OPEN",
                    highest_price=avg_price,
                )
                self.order_manager._positions[trade_id] = trade
                # Pre-populate the broker's security_id cache so close_position
                # can use the cache-first path without needing to re-derive the expiry.
                sec_id = p.get("security_id", "")
                if sec_id and hasattr(self.kite, '_position_security_ids'):
                    self.kite._position_security_ids[symbol.upper()] = str(sec_id)
                recovered += 1
                logger.info(f"♻️  Recovered open position: {symbol} x{abs(qty)} @ ₹{avg_price:.2f}")

            # Restore daily trade counter so max_trades_per_day is respected after restart
            if closed_trades_today > 0:
                self.order_manager._daily_stats.total_trades = max(
                    self.order_manager._daily_stats.total_trades,
                    closed_trades_today + recovered
                )
                logger.info(f"Daily trade counter restored: {self.order_manager._daily_stats.total_trades} trades already done today")

            # Mark strategies as already triggered based on how many trades have
            # occurred today.  The bot supports 2 simultaneous positions (one from
            # ORB, one from VWAP), so only block VWAP when both slots are already
            # consumed.  ORB always fires first (morning), so it is always blocked
            # once any trade is found; VWAP is only blocked when 2+ trades exist.
            total_today = closed_trades_today + recovered
            if total_today > 0:
                self._orb_triggered_date = datetime.now().date()
                if hasattr(self, 'orb') and self.orb:
                    self.orb._signal_fired = True
                    from bot.orb_strategy import ORBState
                    self.orb._state = ORBState.TRIGGERED
                if total_today >= 2:
                    # Both trade slots used — block VWAP re-entry too
                    self._vwap_triggered_date = datetime.now().date()
                    logger.info(f"ORB + VWAP both marked TRIGGERED on recovery ({total_today} trades today — both slots used)")
                else:
                    # Only 1 trade so far — ORB slot used, VWAP slot still available
                    logger.info(f"ORB marked TRIGGERED on recovery ({total_today} trade today). VWAP slot still OPEN for a 2nd position.")

            if recovered:
                await self._broadcast_message(
                    f"♻️  Recovered {recovered} open position(s) from broker", "system"
                )
            else:
                logger.info("Position recovery: no open positions found")
        except Exception as e:
            logger.warning(f"Position recovery failed (non-fatal): {e}")

    async def login(self) -> bool:
        """Login to Zerodha Kite"""
        await self._broadcast_message("Please login to Zerodha Kite in the browser...", "system")
        
        success = await self.kite.login(wait_for_2fa=True)
        
        if success:
            await self._broadcast_message("Login successful!", "success")
        else:
            await self._broadcast_message("Login failed or timed out", "error")
        
        return success
    
    async def start_analysis_loop(self):
        """Start the periodic analysis loop"""
        if self._analysis_task and not self._analysis_task.done():
            logger.warning("Analysis loop already running")
            return
        
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        await self._broadcast_message("Analysis loop started", "system")
        
        # Also start position monitoring
        await self.start_position_monitor()
    
    async def start_position_monitor(self):
        """Start the position monitoring loop for stop-loss and target exits"""
        if self._position_monitor_task and not self._position_monitor_task.done():
            logger.warning("Position monitor already running")
            return
        
        self._position_monitor_task = asyncio.create_task(self._position_monitor_loop())
        await self._broadcast_message("Position monitoring started", "system")
    
    async def stop_analysis_loop(self):
        """Stop the analysis loop"""
        if self._analysis_task:
            self._analysis_task.cancel()
            try:
                await self._analysis_task
            except asyncio.CancelledError:
                pass
            self._analysis_task = None
        
        # Also stop position monitor
        await self.stop_position_monitor()
        
        await self._broadcast_message("Analysis loop stopped", "system")
    
    async def stop_position_monitor(self):
        """Stop the position monitoring loop"""
        if self._position_monitor_task:
            self._position_monitor_task.cancel()
            try:
                await self._position_monitor_task
            except asyncio.CancelledError:
                pass
            self._position_monitor_task = None
        
        await self._broadcast_message("Position monitoring stopped", "system")
    
    async def _analysis_loop(self):
        """Main analysis loop - runs periodically with regime-aware strategy selection."""
        interval = settings.trend.analysis_interval_seconds

        logger.info(f"Starting analysis loop (interval: {interval}s)")
        _last_loop_date = None

        while True:
            try:
                # Reset daily flags at the start of each new trading day
                today = datetime.now().date()
                if _last_loop_date != today:
                    self._gap_traded_today = False
                    self.order_manager._reset_daily_stats_if_needed()
                    _last_loop_date = today

                # ── Step 1: Detect market regime (cached — re-fetches at most every 30 min) ──
                now_ts = datetime.now()
                cache_stale = (
                    self._cached_regime is None
                    or self._last_regime_time is None
                    or (now_ts - self._last_regime_time).total_seconds() >= self._regime_cache_seconds
                )
                if cache_stale:
                    fresh = regime_detector.analyze()
                    # Always update the timestamp so a failing yfinance fetch doesn't
                    # cause a retry-storm (one attempt per 30 min, not every 60 s).
                    self._last_regime_time = now_ts
                    if fresh is not None:
                        self._cached_regime = fresh
                regime_result = self._cached_regime
                regime_value = regime_result.regime.value if regime_result else None

                if cache_stale and regime_result:
                    logger.info(
                        f"Regime: {regime_value} | ADX={regime_result.adx:.1f} "
                        f"| Hurst={regime_result.hurst:.3f} | Trade={regime_result.should_trade}"
                    )

                # ── Step 1b: Gap detection (once per day, near market open) ───
                now = now_ts  # reuse timestamp already captured above
                is_near_open = (
                    now.weekday() <= 4  # Mon–Fri
                    and now.hour == 9
                    and 15 <= now.minute <= 30
                )
                if is_near_open and settings.gap.enabled:
                    await self._check_gap_signal()

                # ── Step 2: Technical trend analysis (regime-aware EMA weight) ─
                signal = self.analyzer.analyze(regime=regime_value)

                if signal:
                    self._last_signal = signal
                    self._last_analysis_time = datetime.now()

                    trade_journal.log_analysis(
                        trend=signal.trend.value,
                        strength=signal.strength,
                        price=signal.current_price,
                        rsi=signal.rsi,
                    )

                    report = self.analyzer.format_analysis_report(signal)
                    # Append regime context to the analysis report
                    if regime_result:
                        report += f"\n  Regime: {regime_value} | Trade Signal: {'YES' if regime_result.should_trade else 'NO'}"
                    await self._broadcast_message(report, "analysis")

                    await self._evaluate_custom_instructions(signal)

                # ── Step 3: Strategy selection based on regime ─────────────────
                if self._auto_trade:
                    is_ranging   = regime_value == "RANGING" if regime_value else False
                    is_trending  = regime_value in ("TRENDING UP", "TRENDING DOWN") if regime_value else False
                    should_trade = regime_result.should_trade if regime_result else True

                    if is_ranging:
                        # RANGING → VWAP Mean Reversion (skip regular trend-follow)
                        await self._check_vwap_signal()
                        # ORB still runs in ranging — opening breakouts can happen even on flat days
                        await self._check_orb_signal()
                        if signal and regime_result and not regime_result.should_trade:
                            logger.info("Ranging market: skipping multi-indicator trend trade.")
                    elif should_trade and signal and signal.trend != Trend.NEUTRAL:
                        # TRENDING / VOLATILE with direction → multi-indicator trade + ORB
                        await self._execute_auto_trade(signal)
                        await self._check_orb_signal()
                    else:
                        # No clear regime or neutral trend → ORB only (safest)
                        # Exception: if regime is STRONGLY TRENDING (ADX > 50) but 5m signal
                        # is NEUTRAL (mixed short-term candles), also run VWAP. This catches
                        # afternoon trades on strong-trend days where intraday indicators lag.
                        # VWAP has its own 0.4% deviation + RSI guards so it won't over-trade.
                        if is_trending and regime_result and regime_result.adx > 50:
                            logger.info(
                                f"Strong trend (ADX={regime_result.adx:.0f}) with neutral 5m signal "
                                f"— running VWAP as secondary entry"
                            )
                            await self._check_vwap_signal()
                        await self._check_orb_signal()

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info("Analysis loop cancelled")
                break
            except Exception as e:
                logger.error(f"Analysis loop error: {e}")
                await self._broadcast_message(f"Analysis error: {e}", "error")
                await asyncio.sleep(10)
    
    async def _position_monitor_loop(self):
        """Monitor open positions for stop-loss and target hits"""
        logger.info("Starting position monitoring loop")
        
        # Check positions every 15 seconds for SL/target
        # Broadcast P&L every 10 checks (150 seconds) 
        check_interval = 15
        pnl_broadcast_count = 0
        
        while True:
            try:
                now = datetime.now()

                # ── Daily loss limit hit: close all open positions immediately ────────
                if self.order_manager:
                    open_count = len([
                        t for t in self.order_manager._positions.values()
                        if t.status == "OPEN"
                    ])
                    daily_loss_hit = (
                        self.order_manager._daily_pnl
                        <= -settings.trading.max_daily_loss
                    )
                    if daily_loss_hit and open_count > 0:
                        logger.warning(
                            f"Daily loss limit ₹{settings.trading.max_daily_loss:,.0f} reached "
                            f"— auto-closing {open_count} open position(s)"
                        )
                        await self._broadcast_message(
                            f"🛑 Daily loss limit reached (₹{settings.trading.max_daily_loss:,.0f}) "
                            f"— closing {open_count} open position(s)",
                            "system"
                        )
                        results = await self.order_manager.close_all_positions()
                        ok = sum(1 for r in results if r.success)
                        await self._broadcast_message(
                            f"Daily loss auto-close: {ok}/{len(results)} positions closed.",
                            "system"
                        )
                        await self._send_telegram_alert(
                            f"🛑 Daily loss limit hit — {ok}/{len(results)} positions closed.",
                            "trade_exit"
                        )

                # ── Market-close auto-exit: close all N minutes before close ─────────
                if self.order_manager and now.weekday() <= 4:  # Mon–Fri only
                    safety_mins = settings.trading.close_all_before_market_close
                    auto_close_dt = now.replace(
                        hour=settings.trading.market_close_hour,
                        minute=settings.trading.market_close_minute,
                        second=0, microsecond=0
                    ) - timedelta(minutes=safety_mins)
                    open_count = len([
                        t for t in self.order_manager._positions.values()
                        if t.status == "OPEN"
                    ])
                    if now >= auto_close_dt and open_count > 0:
                        logger.warning(
                            f"Market close in ≤{safety_mins} min — "
                            f"auto-closing {open_count} position(s)"
                        )
                        await self._broadcast_message(
                            f"⏰ Market close approaching ({safety_mins} min) — "
                            f"auto-closing {open_count} open position(s)",
                            "system"
                        )
                        results = await self.order_manager.close_all_positions()
                        ok = sum(1 for r in results if r.success)
                        await self._broadcast_message(
                            f"Auto-close: {ok}/{len(results)} positions closed.", "system"
                        )
                        await self._send_telegram_alert(
                            f"⏰ Market-close auto-exit: {ok}/{len(results)} positions closed.",
                            "trade_exit"
                        )

                # Check if we have open positions
                if not self.order_manager or not self.order_manager._positions:
                    await asyncio.sleep(check_interval)
                    continue
                
                # Get current market prices
                signal = self.analyzer.analyze()
                if not signal:
                    await asyncio.sleep(check_interval)
                    continue
                
                # Get current prices for all positions
                # Fetch actual option prices from Kite API
                current_prices = {}
                for trade_id, trade in self.order_manager._positions.items():
                    if trade.status == "OPEN":
                        try:
                            # Try to get actual option price from broker
                            price = await self.kite.get_instrument_price(trade.symbol)
                            if price and price > 0:
                                current_prices[trade.symbol] = price
                            else:
                                # No LTP available — skip SL/target check this cycle.
                                # Never use the index price as a proxy for an option premium
                                # because that causes catastrophically false target triggers.
                                logger.debug(
                                    f"LTP unavailable for {trade.symbol} — "
                                    f"skipping SL/target check this cycle"
                                )
                        except Exception as e:
                            logger.debug(f"Could not fetch price for {trade.symbol}: {e}")
                
                # Cache prices for get_status()
                self._current_prices = current_prices

                # ── Update cached P&L from in-memory state (no broker API call) ──
                if self.order_manager:
                    self._cached_daily_pnl = self.order_manager._daily_pnl
                    self._cached_current_pnl = sum(
                        (current_prices.get(t.symbol, t.price) - t.price) * t.quantity
                        for t in self.order_manager._positions.values()
                        if t.status == "OPEN" and t.symbol in current_prices
                    )
                    self._pnl_last_updated = datetime.now()

                # ── Profit-tier partial exits (run BEFORE full SL/target check) ──
                tier_actions = self.order_manager.check_profit_tiers(current_prices)
                for tier_id, tier_qty, tier_label in tier_actions:
                    try:
                        trade_ref = self.order_manager._positions.get(tier_id)
                        if not trade_ref:
                            continue
                        exit_price = current_prices.get(trade_ref.symbol, 0.0)
                        result = await self.order_manager.close_position(
                            trade_id=tier_id, exit_price=exit_price, partial_qty=tier_qty
                        )
                        if result.success:
                            if tier_label == "TIER1":
                                trade_ref.tier1_exited = True
                            locked_pct = settings.trading.take_profit_tier_1_percent if tier_label == "TIER1" \
                                else settings.trading.take_profit_tier_2_percent
                            msg = (
                                f"💰 {tier_label} profit exit: {trade_ref.symbol} "
                                f"— sold {tier_qty} units @ {locked_pct:.0f}% gain | "
                                f"₹{exit_price:.2f}"
                            )
                            await self._broadcast_message(msg, "trade_success")
                            await self._send_telegram_alert(msg, "trade_exit")
                            logger.info(f"{tier_label} partial exit for {tier_id}: qty={tier_qty}")
                            try:
                                _strike = trade_ref.strike or 0
                                _otype  = trade_ref.option_type.value if hasattr(trade_ref.option_type, 'value') else str(trade_ref.option_type)
                                trade_journal.log_trade(f"SELL_{tier_label}", _strike, _otype, exit_price, tier_qty)
                            except Exception:
                                pass
                        else:
                            logger.warning(f"{tier_label} exit failed for {tier_id}: {result.message}")
                    except Exception as e:
                        logger.error(f"Profit tier exit error {tier_id}: {e}")

                # ── Full SL / trailing-stop / target exits ────────────────────────
                actions_needed = self.order_manager.check_stop_loss_targets(current_prices)

                # Update last signal for use in get_status()
                self._last_signal = signal
                
                if actions_needed:
                    logger.info(f"Position monitoring: {len(actions_needed)} positions need action (SL/target hit)")

                    for trade_id in actions_needed:
                        try:
                            # Capture exit price BEFORE closing (position is still in dict)
                            trade_ref = self.order_manager._positions.get(trade_id)
                            exit_price = current_prices.get(trade_ref.symbol, 0.0) if trade_ref else 0.0

                            result = await self.order_manager.close_position(
                                trade_id=trade_id, exit_price=exit_price
                            )

                            if result.success:
                                await self._broadcast_message(
                                    f"✅ Position auto-closed: {result.message}",
                                    "trade_success"
                                )
                                pnl_display = f"₹{trade_ref.pnl:,.2f}" if trade_ref and trade_ref.pnl is not None else "N/A"
                                tg_message = (
                                    f"✅ *Position Closed*\n"
                                    f"{trade_ref.symbol if trade_ref else trade_id}\n"
                                    f"P&L: {pnl_display}\n{result.message}"
                                )
                                await self._send_telegram_alert(tg_message, "trade_exit")
                                logger.info(f"Auto-closed position {trade_id}: {result.message}")
                                try:
                                    _strike = trade_ref.strike if trade_ref else 0
                                    _otype  = trade_ref.option_type.value if trade_ref and hasattr(trade_ref.option_type, 'value') else ''
                                    trade_journal.log_trade("SELL", _strike, _otype, exit_price, trade_ref.quantity if trade_ref else 0)
                                except Exception:
                                    pass
                            else:
                                await self._broadcast_message(
                                    f"❌ Failed to close position: {result.message}",
                                    "trade_error"
                                )
                                logger.error(f"Failed to close {trade_id}: {result.message}")
                        except Exception as e:
                            logger.error(f"Error closing position {trade_id}: {e}")
                            await self._broadcast_message(
                                f"❌ Error closing position: {e}",
                                "trade_error"
                            )
                
                # Broadcast P&L status periodically
                pnl_broadcast_count += 1
                if pnl_broadcast_count >= 10:  # Every 150 seconds
                    await self._broadcast_pnl_status()
                    pnl_broadcast_count = 0
                
                # Wait before next check
                await asyncio.sleep(check_interval)
                
            except asyncio.CancelledError:
                logger.info("Position monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Position monitor error: {e}")
                await self._broadcast_message(f"Position monitor error: {e}", "error")
                await asyncio.sleep(10)
    
    async def _broadcast_pnl_status(self):
        """Broadcast current P&L and position status"""
        if not self.order_manager:
            return
        
        try:
            # Get positions summary
            open_positions = [t for t in self.order_manager._positions.values() if t.status == "OPEN"]
            daily_pnl = self.order_manager._daily_pnl
            daily_stats = self.order_manager._daily_stats
            
            if open_positions:
                positions_text = "\n".join([
                    f"  • {t.symbol} x {t.quantity} @ ₹{t.price:.2f}"
                    for t in open_positions
                ])
            else:
                positions_text = "  No open positions"
            
            pnl_message = f"""
╔═══════════════════════════════════════╗
║         📊 LIVE P&L UPDATE            ║
╚═══════════════════════════════════════╝

💰 DAILY SUMMARY:
  Total Trades:     {daily_stats.total_trades}
  Winning Trades:   {daily_stats.winning_trades} ✅
  Losing Trades:    {daily_stats.losing_trades} ❌
  Realized P&L:     ₹{daily_stats.realized_pnl:,.2f}
  Current Day P&L:  ₹{daily_pnl:,.2f}
  
📈 OPEN POSITIONS ({len(open_positions)}):
{positions_text}

═══════════════════════════════════════
"""
            
            await self._broadcast_message(pnl_message, "pnl")
            logger.info(f"Daily P&L: ₹{daily_pnl:,.2f} | Open positions: {len(open_positions)}")
            
        except Exception as e:
            logger.error(f"Error broadcasting P&L: {e}")
    
    async def _evaluate_custom_instructions(self, signal: TrendSignal):
        """Evaluate custom trading instructions against current market data"""
        # Build market data dict for instruction evaluation
        market_data = {
            'price': signal.current_price,
            'rsi': signal.rsi,
            'macd': signal.macd,
            'macd_signal': signal.macd_signal,
            'sma_short': signal.sma_short,
            'sma_long': signal.sma_long,
            'trend': signal.trend.value,
            'strength': signal.strength
        }
        
        # Evaluate all instructions
        triggered = self.instructions.evaluate_all(market_data)
        
        for instruction in triggered:
            await self._execute_instruction_action(instruction, signal)
            instruction.mark_triggered()
    
    async def _execute_instruction_action(self, instruction, signal: TrendSignal):
        """Execute the action from a triggered instruction"""
        action = instruction.action
        
        await self._broadcast_message(
            f"📋 Rule triggered: [{instruction.id}] {instruction.name}",
            "rule_triggered"
        )
        
        if action.action_type == ActionType.BUY_CE:
            # Perform deep research before execution
            await self._execute_option_with_research(
                signal=signal,
                option_type="CE",
                requested_strike=action.params.get('strike', 'ATM'),
                quantity=action.params.get('qty'),
                source="Rule"
            )
        
        elif action.action_type == ActionType.BUY_PE:
            # Perform deep research before execution
            await self._execute_option_with_research(
                signal=signal,
                option_type="PE",
                requested_strike=action.params.get('strike', 'ATM'),
                quantity=action.params.get('qty'),
                source="Rule"
            )
        
        elif action.action_type == ActionType.SELL_ALL:
            if self.order_manager:
                results = await self.order_manager.close_all_positions()
                await self._broadcast_message(f"Rule action: Closed {len(results)} positions", "trade_success")
        
        elif action.action_type == ActionType.ALERT:
            message = action.params.get('message', 'Alert triggered')
            await self._broadcast_message(f"⚠️ ALERT: {message}", "alert")
        
        elif action.action_type == ActionType.PAUSE:
            self._auto_trade = False
            await self._broadcast_message("Rule action: Auto-trading PAUSED", "system")
    
    async def _execute_orb_direct(
        self,
        signal: TrendSignal,
        option_type: str,
        orb_signal,
    ) -> bool:
        """
        Fast-path ORB execution: places the order immediately at ATM without
        waiting for the full option-chain research cycle (which costs 10-15s).

        ATM strike is calculated inline from the live spot price and the
        active index's strike interval (50 for NIFTY, 100 for BANKNIFTY/SENSEX).
        Research results are broadcast AFTER the fill for informational use.
        """
        if not self.order_manager:
            await self._broadcast_message("Order manager not initialized", "error")
            return False

        interval = self._active_index.strike_interval
        atm = int(round(signal.current_price / interval) * interval)

        await self._broadcast_message(
            f"ORB ENTRY: BUY {option_type} @ {atm} "
            f"(spot {signal.current_price:.2f}, direction={orb_signal.direction}, "
            f"strength={orb_signal.strength:.0f}%)",
            "trade_signal"
        )

        result = await self.order_manager.manual_order(
            option_type=option_type,
            strike=atm,
            order_type="BUY",
            quantity=None,
        )

        if result.success:
            await self._broadcast_message(
                f"✅ ORB trade executed: {result.message}", "trade_success"
            )
            # Journal the entry
            try:
                entry_price = 0.0
                for _t in self.order_manager._positions.values():
                    if (_t.status == "OPEN"
                            and _t.option_type.value == option_type
                            and _t.strike == atm):
                        entry_price = _t.price
                        break
                trade_journal.log_trade(
                    "BUY", atm, option_type, entry_price,
                    self.order_manager.get_active_quantity()
                )
            except Exception:
                pass
            await self._send_telegram_alert(
                f"ORB BREAKOUT {orb_signal.direction}\n"
                f"Entry: {orb_signal.breakout_price:.2f}  ATM: {atm}\n"
                f"SL: {orb_signal.stop_loss:.2f}  T1: {orb_signal.target_1:.2f}\n"
                f"Strength: {orb_signal.strength:.0f}%",
                "trade_entry"
            )
            return True
        else:
            await self._broadcast_message(
                f"❌ ORB order failed: {result.message}", "trade_error"
            )
            return False

    async def _execute_option_with_research(
        self,
        signal: TrendSignal,
        option_type: str,
        requested_strike: any = "ATM",
        quantity: int = None,
        source: str = "Manual"
    ):
        """
        Execute an option trade with deep research first.
        
        This method:
        1. Performs deep market research
        2. Shows the analysis to the user
        3. Validates the strike against recommendations
        4. Executes the trade
        
        Args:
            signal: Current TrendSignal
            option_type: "CE" or "PE"
            requested_strike: Strike price or "ATM"
            quantity: Order quantity
            source: Source of trade ("Manual", "Rule", "Auto")
        """
        if not self.order_manager:
            await self._broadcast_message("Order manager not initialized", "error")
            return
        
        # Step 1: Perform deep research
        await self._broadcast_message(
            f"🔍 Performing deep research before {option_type} trade...",
            "system"
        )
        
        analysis = self.research.analyze_option_chain(
            spot_price=signal.current_price,
            trend=signal.trend.value,
            trend_strength=signal.strength,
            rsi=signal.rsi
        )
        
        # Step 2: Show research results
        research_report = self.research.format_option_analysis(analysis, signal.trend.value)
        await self._broadcast_message(research_report, "analysis")
        
        # Step 3: Determine the best strike based on research
        if requested_strike == "ATM" or requested_strike is None:
            # Use moderate recommendation by default
            recommended = analysis.moderate_strike
            final_strike = recommended.strike
            strike_info = f"Using MODERATE recommendation: {recommended.moneyness} @ {final_strike}"
        else:
            final_strike = int(requested_strike)
            # Check if requested strike matches any recommendation
            if final_strike == analysis.conservative_strike.strike:
                strike_info = f"Requested strike matches CONSERVATIVE recommendation"
            elif final_strike == analysis.moderate_strike.strike:
                strike_info = f"Requested strike matches MODERATE recommendation"
            elif final_strike == analysis.aggressive_strike.strike:
                strike_info = f"Requested strike matches AGGRESSIVE recommendation"
            else:
                # Custom strike - warn user
                distance = abs(final_strike - analysis.atm_strike)
                if distance > 150:
                    await self._broadcast_message(
                        f"⚠️ Warning: Strike {final_strike} is far from ATM ({analysis.atm_strike}). Proceed with caution.",
                        "alert"
                    )
                strike_info = f"Using custom strike: {final_strike}"
        
        await self._broadcast_message(f"📊 {strike_info}", "system")
        
        # Step 4: Validate option type matches trend
        if option_type == "CE" and signal.trend.value == "BEARISH":
            await self._broadcast_message(
                "⚠️ Warning: Buying CALL in a BEARISH trend. High risk!",
                "alert"
            )
        elif option_type == "PE" and signal.trend.value == "BULLISH":
            await self._broadcast_message(
                "⚠️ Warning: Buying PUT in a BULLISH trend. High risk!",
                "alert"
            )
        
        # Step 5: Execute the trade
        await self._broadcast_message(
            f"🚀 Executing: BUY {option_type} @ {final_strike}",
            "trade_signal"
        )
        
        result = await self.order_manager.manual_order(
            option_type=option_type,
            strike=final_strike,
            order_type="BUY",
            quantity=quantity
        )
        
        if result.success:
            await self._broadcast_message(
                f"✅ {source} trade executed: {result.message}",
                "trade_success"
            )
            # Journal the entry so the Ledger tab shows the trade
            try:
                entry_price = 0.0
                for _t in self.order_manager._positions.values():
                    if _t.status == "OPEN" and _t.option_type.value == option_type and _t.strike == final_strike:
                        entry_price = _t.price
                        break
                trade_journal.log_trade("BUY", final_strike, option_type, entry_price, quantity or self.order_manager.get_active_quantity())
            except Exception:
                pass
            # Send Telegram alert for trade entry
            tg_message = f"🚀 *Trade Entry - {source}*\n{option_type} @ {final_strike}\nQty: {quantity}\n{result.message}"
            await self._send_telegram_alert(tg_message, "trade_entry")
            return True
        else:
            await self._broadcast_message(
                f"❌ {source} trade failed: {result.message}",
                "trade_error"
            )
            # Send Telegram alert for trade failure
            tg_message = f"❌ *Trade Failed - {source}*\n{option_type} @ {final_strike}\n{result.message}"
            await self._send_telegram_alert(tg_message, "trade_error")
            return False
    
    async def _check_gap_signal(self):
        """
        Run gap detection once per trading day near market open.
        For a strong gap → place a trade immediately in the gap direction.
        For a moderate gap → log and alert; ORB will confirm later.
        """
        if self._gap_traded_today:
            return

        try:
            gap = self._gap_detector.analyze()
            if not gap:
                return

            report = self._gap_detector.format_report(gap)
            await self._broadcast_message(report, "gap_analysis")
            await self._send_telegram_alert(report, "gap_analysis")
            logger.info(f"Gap analysis done: {gap.gap_type.value} {gap.gap_pct:+.2f}%")

            # No trade needed for neutral gaps
            if gap.gap_type == GapType.NEUTRAL or not self._auto_trade:
                return

            # Moderate gap — just alert; ORB loop will confirm
            if gap.wait_for_confirmation:
                note = (
                    f"📊 Moderate gap detected ({gap.gap_pct:+.2f}%). "
                    f"Waiting for ORB confirmation to enter {gap.trade_direction}."
                )
                await self._broadcast_message(note, "gap_analysis")
                return

            # Strong gap — enter immediately
            if not self.order_manager:
                return

            can_trade, reason = self.order_manager.can_place_order()
            if not can_trade:
                logger.info(f"Gap trade blocked: {reason}")
                return

            # Fetch current index price for ATM strike
            from bot.trend_analyzer import TrendAnalyzer
            signal = self.analyzer.analyze()
            if not signal:
                return

            current_price = signal.current_price
            strike_interval = self._active_index.strike_interval
            atm_strike = round(current_price / strike_interval) * strike_interval

            from browser.dhan import OptionType
            from config import settings as cfg
            opt = OptionType.CE if gap.trade_direction == "CE" else OptionType.PE
            qty = int(self._active_index.lot_size * cfg.gap.quantity_multiplier)

            result = await self.order_manager.manual_order(
                option_type=opt.value,   # "CE" or "PE"
                strike=atm_strike,
                order_type="BUY",
                quantity=qty,
            )

            if result.success:
                self._gap_traded_today = True
                msg = (
                    f"🚀 **Gap Trade Executed**\n"
                    f"  Gap: {gap.gap_type.value} ({gap.gap_pct:+.2f}%)\n"
                    f"  Bought {opt.value} @ strike {atm_strike} x {qty} units\n"
                    f"  Order: {result.order_id}"
                )
                await self._broadcast_message(msg, "trade_success")
                await self._send_telegram_alert(msg, "trade_entry")
                logger.info(f"Gap trade placed: {opt.value} {atm_strike} x {qty}")
            else:
                logger.error(f"Gap trade failed: {result.message}")
                await self._broadcast_message(f"❌ Gap trade failed: {result.message}", "trade_error")

        except Exception as e:
            logger.error(f"Gap signal check error: {e}")

    async def _check_orb_signal(self):
        """Check ORB strategy and execute trade if a breakout is detected."""
        # Engine-level one-trade-per-day guard: survives ORB resets and index switches
        if self._orb_triggered_date == datetime.now().date():
            return
        try:
            orb_signal = self.orb.analyze()
            if not orb_signal:
                return

            # ── Guard 1: Minimum strength threshold ───────────────────────────
            if orb_signal.strength < 75:
                logger.info(
                    f"ORB: Skipping — strength {orb_signal.strength:.0f}% < 75% threshold"
                )
                await self._broadcast_message(
                    f"ORB: Signal skipped — strength {orb_signal.strength:.0f}% below 75% minimum",
                    "alert"
                )
                return

            # ── Guard 2: Trend-direction filter ───────────────────────────────
            if not self._last_signal:
                # Fetch trend so we have a direction to validate against
                _tsig = self.analyzer.analyze()
                if _tsig:
                    self._last_signal = _tsig

            if self._last_signal:
                _trend = self._last_signal.trend.value   # "BULLISH" | "BEARISH" | "NEUTRAL"

                if orb_signal.direction == "LONG" and _trend == "BEARISH":
                    logger.info("ORB: Skipping LONG — overall trend is BEARISH (counter-trend)")
                    await self._broadcast_message(
                        "ORB: LONG signal skipped — market trend is BEARISH", "alert"
                    )
                    return

                if orb_signal.direction == "SHORT" and _trend == "BULLISH":
                    logger.info("ORB: Skipping SHORT — overall trend is BULLISH (counter-trend)")
                    await self._broadcast_message(
                        "ORB: SHORT signal skipped — market trend is BULLISH", "alert"
                    )
                    return

                # ── Guard 3: Higher bar on NEUTRAL days ───────────────────────
                if _trend == "NEUTRAL" and orb_signal.strength < 80:
                    logger.info(
                        f"ORB: Skipping — NEUTRAL trend requires ≥80% strength "
                        f"(got {orb_signal.strength:.0f}%)"
                    )
                    await self._broadcast_message(
                        f"ORB: Signal skipped — NEUTRAL market needs ≥80% strength "
                        f"(got {orb_signal.strength:.0f}%)",
                        "alert"
                    )
                    return

            # Broadcast the breakout notification
            await self._broadcast_message(self._format_orb_signal(orb_signal), "analysis")

            # Need a TrendSignal for option chain research (current index price + RSI)
            if not self._last_signal:
                signal = self.analyzer.analyze()
                if not signal:
                    await self._broadcast_message(
                        "ORB: Could not fetch trend signal for trade execution", "error"
                    )
                    return
                self._last_signal = signal

            option_type = "CE" if orb_signal.direction == "LONG" else "PE"

            # ── Guard 4: RSI overbought/oversold filter ──────────────────────────
            _rsi = self._last_signal.rsi if self._last_signal else 50
            if option_type == "CE" and _rsi > 65:
                logger.info(
                    f"ORB: Skipping LONG — RSI {_rsi:.1f} is overbought (>65), chasing breakout"
                )
                await self._broadcast_message(
                    f"ORB: CE signal skipped — RSI {_rsi:.1f} already overbought (>65)",
                    "alert"
                )
                return
            if option_type == "PE" and _rsi < 35:
                logger.info(
                    f"ORB: Skipping SHORT — RSI {_rsi:.1f} is oversold (<35), chasing breakdown"
                )
                await self._broadcast_message(
                    f"ORB: PE signal skipped — RSI {_rsi:.1f} already oversold (<35)",
                    "alert"
                )
                return

            # ── Guard 5: 15m timeframe must agree with ORB direction ─────────────
            try:
                mtf_result = mtf_engine.analyze()
                if mtf_result and mtf_result.signals.get("15m"):
                    _15m = mtf_result.signals["15m"]
                    if option_type == "CE" and _15m.trend == "BEARISH":
                        logger.info(
                            f"ORB: Skipping LONG — 15m trend is BEARISH — no MTF confluence"
                        )
                        await self._broadcast_message(
                            "ORB: CE signal skipped — 15m chart is BEARISH (no confluence)",
                            "alert"
                        )
                        return
                    if option_type == "PE" and _15m.trend == "BULLISH":
                        logger.info(
                            f"ORB: Skipping SHORT — 15m trend is BULLISH — no MTF confluence"
                        )
                        await self._broadcast_message(
                            "ORB: PE signal skipped — 15m chart is BULLISH (no confluence)",
                            "alert"
                        )
                        return
            except Exception as _mtf_err:
                logger.warning(f"ORB: MTF check failed ({_mtf_err}), skipping MTF guard")

            # Fast-path: place ORB trade directly at ATM — skip slow research
            # Research adds 10-15s latency; by then the breakout candle may reverse.
            order_ok = await self._execute_orb_direct(
                signal=self._last_signal,
                option_type=option_type,
                orb_signal=orb_signal,
            )

            if order_ok:
                self._orb_triggered_date = datetime.now().date()  # block further ORB trades today

        except Exception as e:
            logger.error(f"ORB signal check error: {e}")

    async def _check_vwap_signal(self):
        """Check VWAP mean-reversion strategy and execute if a signal fires."""
        # Engine-level one-trade-per-day guard: survives VWAP resets and bot restarts
        if self._vwap_triggered_date == datetime.now().date():
            return
        try:
            vwap_signal = self.vwap_strat.analyze()
            if not vwap_signal:
                return

            # ── Regime-direction alignment guard ─────────────────────────────
            # On strong TRENDING DOWN days: block LONG (CE) entries — only allow PE.
            # On strong TRENDING UP days: block SHORT (PE) entries — only allow CE.
            # This prevents buying CE into a confirmed downtrend via VWAP reversion.
            cached_regime = getattr(self, "_cached_regime", None)
            if cached_regime:
                regime_val = cached_regime.regime.value
                if regime_val == "TRENDING DOWN" and vwap_signal.direction == "LONG":
                    logger.info(
                        "VWAP: Skipping LONG (CE) — regime is TRENDING DOWN (counter-trend)"
                    )
                    return
                if regime_val == "TRENDING UP" and vwap_signal.direction == "SHORT":
                    logger.info(
                        "VWAP: Skipping SHORT (PE) — regime is TRENDING UP (counter-trend)"
                    )
                    return

            await self._broadcast_message(self._format_vwap_signal(vwap_signal), "analysis")

            if not self._last_signal:
                signal = self.analyzer.analyze()
                if not signal:
                    await self._broadcast_message(
                        "VWAP: Could not fetch trend signal for trade execution", "error"
                    )
                    return
                self._last_signal = signal

            option_type = "CE" if vwap_signal.direction == "LONG" else "PE"

            order_ok = await self._execute_option_with_research(
                signal=self._last_signal,
                option_type=option_type,
                requested_strike="ATM",
                quantity=None,
                source="VWAP"
            )

            if order_ok:
                self._vwap_triggered_date = datetime.now().date()  # block further VWAP trades today
                tg_msg = (
                    f"VWAP REVERSION {vwap_signal.direction}\n"
                    f"Entry: {vwap_signal.current_price:.2f}\n"
                    f"VWAP: {vwap_signal.vwap:.2f}  Dev: {vwap_signal.deviation_pct:+.2f}%\n"
                    f"SL: {vwap_signal.stop_loss:.2f}  Target: {vwap_signal.target:.2f}\n"
                    f"RSI: {vwap_signal.rsi:.1f}  Strength: {vwap_signal.strength:.0f}%"
                )
                await self._send_telegram_alert(tg_msg, "trade_entry")

        except Exception as e:
            logger.error(f"VWAP signal check error: {e}")

    def _format_vwap_signal(self, sig: VWAPSignal) -> str:
        """Format a VWAPSignal for display in the chat console."""
        side = "LONG  →  BUY CE" if sig.direction == "LONG" else "SHORT  →  BUY PE"
        vol_label = "Yes" if sig.volume_confirmed else "No"
        return (
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  VWAP MEAN REVERSION  —  {self._active_index.display_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  Direction    : {side}\n"
            f"  Entry Price  : {sig.current_price:,.2f}\n"
            f"  VWAP         : {sig.vwap:,.2f}\n"
            f"  Deviation    : {sig.deviation_pct:+.2f}%\n"
            f"\n"
            f"  Trade Plan:\n"
            f"    Stop Loss  : {sig.stop_loss:,.2f}\n"
            f"    Target     : {sig.target:,.2f}  (mean revert to VWAP)\n"
            f"\n"
            f"  RSI          : {sig.rsi:.1f}\n"
            f"  Vol Confirm  : {vol_label}\n"
            f"  Confidence   : {sig.strength:.0f}%\n"
            f"  Time         : {sig.timestamp.strftime('%H:%M:%S')} IST\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    def _format_orb_signal(self, sig: ORBSignal) -> str:
        """Format an ORBSignal for display in the chat console."""
        side = "LONG  →  BUY CE" if sig.direction == "LONG" else "SHORT  →  BUY PE"
        vol_label = "Yes" if sig.volume_confirmed else "No (low volume)"
        return (
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  ORB BREAKOUT  —  {self._active_index.display_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  Direction    : {side}\n"
            f"  Break Price  : {sig.breakout_price:,.2f}\n"
            f"\n"
            f"  Opening Range:\n"
            f"    High       : {sig.range_high:,.2f}\n"
            f"    Low        : {sig.range_low:,.2f}\n"
            f"    Width      : {sig.range_width:,.2f}  pts\n"
            f"\n"
            f"  Trade Plan:\n"
            f"    Stop Loss  : {sig.stop_loss:,.2f}  (opposite range end)\n"
            f"    Target 1   : {sig.target_1:,.2f}  (+{sig.range_width * settings.orb.target_multiplier:.1f} pts, x{settings.orb.target_multiplier})\n"
            f"    Target 2   : {sig.target_2:,.2f}  (+{sig.range_width * settings.orb.target_multiplier_2:.1f} pts, x{settings.orb.target_multiplier_2})\n"
            f"\n"
            f"  Confidence   : {sig.strength:.0f}%\n"
            f"  Volume Spike : {vol_label}\n"
            f"  Time         : {sig.timestamp.strftime('%H:%M:%S')} IST\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    async def _execute_auto_trade(self, signal: TrendSignal):
        """Execute automatic trade based on signal with deep research"""
        if not self.order_manager:
            return
        
        # Determine option type from trend
        if signal.trend == Trend.BULLISH:
            option_type = "CE"
        elif signal.trend == Trend.BEARISH:
            option_type = "PE"
        else:
            return  # Don't trade on neutral
        
        # Execute with deep research
        await self._execute_option_with_research(
            signal=signal,
            option_type=option_type,
            requested_strike="ATM",
            quantity=None,
            source="Auto"
        )
    
    async def process_command(self, command: str) -> str:
        """
        Process a chat command.
        
        Available commands:
        - status: Get current bot status
        - analyze: Run immediate analysis
        - buy CE <strike>: Buy call option
        - buy PE <strike>: Buy put option
        - sell <symbol>: Sell/exit position
        - close all: Close all positions
        - pause: Pause auto-trading
        - resume: Resume auto-trading
        - set sl <percentage>: Set stop-loss
        - positions: Show open positions
        - help: Show available commands
        
        Args:
            command: Command string from chat
            
        Returns:
            Response message
        """
        trade_journal.log_user_command(command)
        
        nl_override = self._normalize_natural_language_command(command)
        nl_note = None
        if nl_override:
            command = nl_override["command"]
            nl_note = nl_override["note"]

        cmd = command.strip().lower()
        parts = cmd.split()
        
        if not parts:
            return "Empty command. Type 'help' for available commands."
        
        action = parts[0]
        
        try:
            if action == "help":
                return self._get_help_text()
            
            elif action == "status":
                return self._get_status_text()
            
            elif action == "analyze":
                signal = self.analyzer.analyze()
                if signal:
                    self._last_signal = signal
                    return self.analyzer.format_analysis_report(signal)
                return "Analysis failed - check data availability"
            
            elif action == "buy":
                return await self._handle_buy_command(parts)
            
            elif action == "sell" or action == "exit":
                return await self._handle_sell_command(parts)
            
            elif action == "close" and len(parts) > 1 and parts[1] == "all":
                return await self._handle_close_all()
            
            elif action == "pause":
                self._auto_trade = False
                return "Auto-trading PAUSED. Manual commands still work."
            
            elif action == "resume":
                self._auto_trade = True
                return "Auto-trading RESUMED."
            
            elif action == "set":
                return self._handle_set_command(parts)
            
            elif action == "positions":
                if self.order_manager:
                    return self.order_manager.get_positions_summary()
                return "Order manager not initialized"
            
            elif action == "daily" or action == "summary":
                if self.order_manager:
                    return self.order_manager.get_daily_summary()
                return "Order manager not initialized"
            
            elif action == "screenshot":
                await self.kite.get_screenshot()
                return "Screenshot saved"
            
            elif action == "rule" or action == "rules":
                return await self._handle_rule_command(parts)
            
            elif action == "if" or action == "when":
                # Direct instruction parsing
                return self._handle_add_rule(command)
            
            elif action == "research" or action == "smart":
                return self._handle_research_command()
            
            elif action == "stock" or action == "equity":
                response = await self._handle_stock_command(parts)
                if nl_note:
                    return f"{nl_note}\n\n{response}"
                return response
            
            elif action == "screen":
                return self._handle_screen_command(parts)
            
            elif action == "strike":
                return self._handle_strike_command(parts)
            
            elif action == "watch":
                return await self._handle_watch_command(parts)
            
            elif action == "stop" or action == "unwatch":
                return await self._stop_watch()
            
            elif action == "backtest":
                return self._handle_backtest(parts)
            
            elif action == "regime":
                return self._handle_regime()
            
            elif action == "theta":
                return self._handle_theta(parts)
            
            elif action == "mtf" or action == "confluence":
                return self._handle_mtf()
            
            elif action == "journal":
                return self._handle_journal(parts)
            
            elif action == "orb":
                return self._handle_orb_command(parts)

            elif action == "vwap":
                return self._handle_vwap_command(parts)

            elif action == "gap":
                return self._handle_gap_command()

            elif action == "index" or action == "switch":
                return await self._handle_index_command(parts)
            
            else:
                return f"Unknown command: {action}. Type 'help' for available commands."
                
        except Exception as e:
            logger.error(f"Command error: {e}")
            return f"Error processing command: {e}"

    def _normalize_natural_language_command(self, command: str) -> Optional[Dict[str, str]]:
        """
        Map simple natural language questions to structured commands.

        Examples:
          "When can I buy HDFCBANK?" -> stock HDFCBANK
          "Should I buy INFY" -> stock INFY
          "buy RELIANCE" -> stock RELIANCE (if CE/PE not specified)
        """
        text = command.strip()
        if not text:
            return None

        lower = text.lower()
        exchange_hint = None
        if "bse" in lower:
            exchange_hint = "BSE"
        elif "nse" in lower:
            exchange_hint = "NSE"

        # Avoid overriding option trades or explicit commands
        if any(token in lower for token in [" ce", " pe", "strike", "nifty", "option", "options"]):
            return None

        # Match "buy <symbol>" where symbol is alphabetic
        buy_match = re.search(r"\bbuy\s+([a-zA-Z]{2,15})\b", lower)
        if buy_match:
            raw_symbol = buy_match.group(1).upper()
            resolved = self.research.resolve_symbol(raw_symbol, exchange=exchange_hint)
            symbol = resolved.replace(".NS", "").replace(".BO", "") if resolved else raw_symbol
            exchange_label = exchange_hint or ("NSE" if resolved and resolved.endswith(".NS") else "BSE" if resolved and resolved.endswith(".BO") else "")
            suffix = f" {exchange_label}" if exchange_label else ""
            return {
                "command": f"stock {symbol}{suffix}",
                "note": f"Interpreting as stock analysis for {symbol}{suffix}."
            }

        # Match question-style prompts with a symbol near "buy"
        if any(prefix in lower for prefix in ["when", "can i", "should i", "is it", "do i", "what about", "price", "analysis", "target", "good time", "invest"]):
            tokens = re.findall(r"\b[a-zA-Z]{2,15}\b", lower)
            for token in tokens:
                resolved = self.research.resolve_symbol(token.upper(), exchange=exchange_hint)
                if resolved:
                    symbol = resolved.replace(".NS", "").replace(".BO", "")
                    exchange_label = exchange_hint or ("NSE" if resolved.endswith(".NS") else "BSE")
                    return {
                        "command": f"stock {symbol} {exchange_label}",
                        "note": f"Interpreting as stock analysis for {symbol} ({exchange_label})."
                    }

        return None
    
    async def _handle_buy_command(self, parts: List[str]) -> str:
        """Handle buy command: buy CE/PE <strike> - includes deep research"""
        if len(parts) < 2:
            return "Usage: buy CE <strike> or buy PE <strike> (strike is optional - will use ATM)"
        
        option_type = parts[1].upper()
        if option_type not in ["CE", "PE"]:
            return "Invalid option type. Use CE (Call) or PE (Put)"
        
        # Strike is optional - if not provided, use ATM
        strike = "ATM"
        if len(parts) >= 3:
            try:
                strike = int(parts[2])
            except ValueError:
                return "Invalid strike price. Must be a number or omit for ATM."
        
        quantity = None
        if len(parts) > 3:
            try:
                quantity = int(parts[3])
            except ValueError:
                pass
        
        if not self.order_manager:
            return "Order manager not initialized. Please initialize bot first."
        
        # Get current signal for research
        if not self._last_signal:
            signal = self.analyzer.analyze()
            if not signal:
                return "Could not analyze market. Please try again."
            self._last_signal = signal
        
        # Execute with deep research
        await self._execute_option_with_research(
            signal=self._last_signal,
            option_type=option_type,
            requested_strike=strike,
            quantity=quantity,
            source="Manual"
        )
        
        return "Trade processed with deep research. See analysis above."
    
    async def _handle_sell_command(self, parts: List[str]) -> str:
        """Handle sell/exit command"""
        if len(parts) < 2:
            return "Usage: sell <symbol or trade_id>"
        
        identifier = parts[1].upper()
        
        if not self.order_manager:
            return "Order manager not initialized"
        
        # Try as trade ID first, then as symbol
        result = await self.order_manager.close_position(
            trade_id=identifier,
            symbol=identifier
        )
        
        if result.success:
            # Send Telegram alert for trade exit
            tg_message = f"✅ *Position Sold*\n{identifier}\n{result.message}"
            await self._send_telegram_alert(tg_message, "trade_exit")
        
        return result.message
    
    async def _handle_close_all(self) -> str:
        """Handle close all command"""
        if not self.order_manager:
            return "Order manager not initialized"
        
        results = await self.order_manager.close_all_positions()
        
        if not results:
            return "No positions to close"
        
        success_count = sum(1 for r in results if r.success)
        
        # Send Telegram alert for closing all positions
        tg_message = f"🛑 *Closed All Positions*\nSuccessful: {success_count}/{len(results)}"
        await self._send_telegram_alert(tg_message, "trade_exit")
        
        return f"Closed {success_count}/{len(results)} positions"
    
    def _handle_set_command(self, parts: List[str]) -> str:
        """Handle set command for configuration"""
        if len(parts) < 3:
            return "Usage: set sl <percentage> or set qty <quantity>"
        
        setting = parts[1].lower()
        value = parts[2]
        
        try:
            if setting == "sl":
                new_sl = float(value)
                settings.trading.stop_loss_percentage = new_sl
                return f"Stop-loss set to {new_sl}%"
            
            elif setting == "qty":
                new_qty = int(value)
                settings.trading.default_quantity = new_qty
                return f"Default quantity set to {new_qty}"
            
            elif setting == "target":
                new_target = float(value)
                settings.trading.target_percentage = new_target
                return f"Target set to {new_target}%"
            
            else:
                return f"Unknown setting: {setting}"
                
        except ValueError:
            return "Invalid value"
    
    async def _handle_rule_command(self, parts: List[str]) -> str:
        """Handle rule management commands"""
        if len(parts) < 2:
            return self.instructions.format_instructions_list()
        
        subcommand = parts[1].lower()
        
        if subcommand == "list" or subcommand == "ls":
            return self.instructions.format_instructions_list()
        
        elif subcommand == "add":
            if len(parts) < 3:
                return """
Usage: rule add <instruction>

Examples:
  rule add if rsi < 30 then buy ce
  rule add if rsi > 70 and trend is bearish then buy pe
  rule add if price > 24500 then alert
  rule add when rsi < 25 buy ce at 24000
  rule add if strength > 75 and trend is bullish then buy ce

Supported conditions:
  - rsi < N / rsi > N
  - price < N / price > N
  - trend is bullish / trend is bearish
  - strength < N / strength > N
  - between HH:MM-HH:MM (time range)

Supported actions:
  - buy ce [at <strike>] [qty <N>]
  - buy pe [at <strike>] [qty <N>]
  - sell all
  - alert [message]
  - pause
"""
            
            instruction_text = ' '.join(parts[2:])
            return self._handle_add_rule(instruction_text)
        
        elif subcommand == "enable":
            if len(parts) < 3:
                return "Usage: rule enable <rule_id>"
            rule_id = parts[2].upper()
            if self.instructions.enable_instruction(rule_id):
                return f"Rule {rule_id} enabled"
            return f"Rule {rule_id} not found"
        
        elif subcommand == "disable":
            if len(parts) < 3:
                return "Usage: rule disable <rule_id>"
            rule_id = parts[2].upper()
            if self.instructions.disable_instruction(rule_id):
                return f"Rule {rule_id} disabled"
            return f"Rule {rule_id} not found"
        
        elif subcommand == "remove" or subcommand == "delete":
            if len(parts) < 3:
                return "Usage: rule remove <rule_id>"
            rule_id = parts[2].upper()
            if self.instructions.remove_instruction(rule_id):
                return f"Rule {rule_id} removed"
            return f"Rule {rule_id} not found"
        
        elif subcommand == "reset":
            self.instructions.reset_daily_counts()
            return "Daily trigger counts reset for all rules"
        
        elif subcommand == "help":
            return self._get_rule_help_text()
        
        else:
            return f"Unknown rule command: {subcommand}. Try 'rule help'"
    
    def _handle_add_rule(self, instruction_text: str) -> str:
        """Add a new rule from natural language"""
        instruction = self.instructions.parse_instruction(instruction_text)
        
        if instruction:
            return f"""
✓ Rule created successfully!

ID: {instruction.id}
Name: {instruction.name[:40]}...
Conditions: {len(instruction.conditions)} condition(s)
Action: {instruction.action.get_description()}
Status: ENABLED

The rule will be evaluated on each analysis cycle.
Use 'rule disable {instruction.id}' to disable it.
"""
        else:
            return """
✗ Could not parse instruction. 

Make sure to include:
1. A condition (e.g., "rsi < 30", "price > 24000", "trend is bullish")
2. An action (e.g., "buy ce", "buy pe at 24000", "alert", "sell all")

Examples:
  if rsi < 30 then buy ce
  when trend is bullish and rsi < 40 buy ce at 24000
  if price > 24500 then alert price is high
"""
    
    def _handle_research_command(self) -> str:
        """Handle deep research command for smart strike selection"""
        if not self._last_signal:
            # Run analysis first
            signal = self.analyzer.analyze()
            if not signal:
                return "Could not perform analysis. Try again later."
            self._last_signal = signal
        
        signal = self._last_signal
        
        # Perform deep option chain analysis
        analysis = self.research.analyze_option_chain(
            spot_price=signal.current_price,
            trend=signal.trend.value,
            trend_strength=signal.strength,
            rsi=signal.rsi
        )
        
        return self.research.format_option_analysis(analysis, signal.trend.value)
    
    async def _handle_stock_command(self, parts: List[str]) -> str:
        """Handle stock/equity analysis command"""
        if len(parts) < 2:
            return """
Usage: stock <symbol>

Optional exchange:
    stock HDFCBANK NSE
    stock HDFCBANK BSE

Examples:
  stock RELIANCE    - Analyze Reliance Industries
  stock TCS         - Analyze TCS
  stock HDFCBANK    - Analyze HDFC Bank
  stock INFY        - Analyze Infosys

This will provide:
  - Technical analysis (RSI, SMA, trend)
  - Support/Resistance levels
  - Buy/Sell/Hold recommendation
  - Target price and stop loss
"""
        
        symbol = parts[1].upper()
        exchange = None
        if len(parts) >= 3:
            exchange_hint = parts[2].upper()
            if exchange_hint in ["BSE", "BO"]:
                exchange = "BSE"
            elif exchange_hint in ["NSE", "NS"]:
                exchange = "NSE"
        
        analysis = self.research.analyze_stock(symbol, exchange=exchange)
        
        if not analysis:
            return f"Could not analyze {symbol}. Check if the symbol is correct (e.g., RELIANCE, TCS, INFY)"
        
        return self.research.format_stock_analysis(analysis)
    
    def _handle_screen_command(self, parts: List[str]) -> str:
        """Handle stock screening command"""
        criteria = "bullish"  # default
        
        if len(parts) > 1:
            criteria = parts[1].lower()
            if criteria not in ["bullish", "bearish", "oversold", "overbought"]:
                return """
Usage: screen <criteria>

Available criteria:
  screen bullish    - Find stocks with bullish signals (BUY candidates)
  screen bearish    - Find stocks with bearish signals
  screen oversold   - Find stocks with RSI < 30 (potential bounce)
  screen overbought - Find stocks with RSI > 70 (potential correction)
"""
        
        results = self.research.screen_stocks(criteria)
        
        if not results:
            return f"No stocks found matching '{criteria}' criteria"
        
        lines = [f"═══ STOCK SCREEN: {criteria.upper()} ═══\n"]
        
        for i, stock in enumerate(results, 1):
            action_emoji = "🟢" if stock.action == "BUY" else "🔴" if stock.action == "SELL" else "🟡"
            lines.append(
                f"{i}. {stock.symbol.replace('.NS', '')} - ₹{stock.current_price:,.2f} "
                f"({'+' if stock.change_percent > 0 else ''}{stock.change_percent:.1f}%)"
            )
            lines.append(f"   {action_emoji} {stock.action} | RSI: {stock.rsi:.1f} | Trend: {stock.trend}")
            lines.append(f"   Target: ₹{stock.target_price:,.2f} | SL: ₹{stock.stop_loss:,.2f}")
            lines.append("")
        
        lines.append(f"\nUse 'stock <symbol>' for detailed analysis")
        
        return "\n".join(lines)
    
    def _handle_strike_command(self, parts: List[str]) -> str:
        """
        Handle strike analysis command: strike <price> <CE/PE> <expiry_date>
        
        Examples:
            strike 25000 CE 27jan    - Analyze 25000 Call expiring 27 Jan
            strike 24900 PE 3feb     - Analyze 24900 Put expiring 3 Feb
            strike 25100 CE 17feb    - Analyze 25100 Call expiring 17 Feb (monthly)
        """
        if len(parts) < 2:
            return self._get_strike_help()
        
        # Parse strike price
        try:
            strike = int(parts[1])
        except ValueError:
            return f"Invalid strike price: {parts[1]}. Please enter a number (e.g., strike 25000 CE 27jan)"
        
        lo, hi = self._active_index.strike_range
        if strike < lo or strike > hi:
            return f"Strike {strike} seems outside {self._active_index.display_name} range ({lo}-{hi}). Please check."
        
        # Get current market data
        signal = self.analyzer.analyze()
        if not signal:
            return "Could not fetch market data. Please try again."
        
        spot_price = signal.current_price
        trend = signal.trend.value
        rsi = signal.rsi
        
        # Parse remaining arguments for option type and expiry date
        option_type = None
        expiry_date_str = None
        
        for part in parts[2:]:
            part_upper = part.upper()
            if part_upper in ["CE", "PE"]:
                option_type = part_upper
            else:
                # Try to parse as expiry date (e.g., 27jan, 3feb, 17feb)
                expiry_date_str = part.lower()
        
        # Auto-select option type if not specified
        if option_type is None:
            if trend == "BULLISH":
                option_type = "CE"
            elif trend == "BEARISH":
                option_type = "PE"
            else:
                # Neutral - suggest based on strike position
                if strike > spot_price:
                    option_type = "CE"
                else:
                    option_type = "PE"
        
        analysis = self.research.analyze_specific_strike(
            strike=strike,
            option_type=option_type,
            spot_price=spot_price,
            trend=trend,
            rsi=rsi,
            expiry_date_str=expiry_date_str,
            index_name=self._active_index.name,
        )
        
        return self.research.format_strike_analysis(analysis)
    
    async def _handle_watch_command(self, parts: List[str]) -> str:
        """
        Handle watch command: continuously monitor a strike until stopped.
        
        Usage: watch <price> <CE/PE> <expiry> [interval]
        Example: watch 25000 CE 27jan 30
        """
        if len(parts) < 2:
            return """
Usage: watch <price> <CE/PE> <expiry> [interval_seconds]

Examples:
  watch 25000 CE 27jan        - Monitor 25000 CE expiring 27 Jan (every 30s)
  watch 24900 PE 3feb 60      - Monitor 24900 PE every 60 seconds
  watch 25100 CE 10feb 15     - Monitor every 15 seconds

To stop watching:
  stop  or  unwatch

The bot will continuously update you with:
  - Current price movement
  - GO / NO-GO status changes
  - Key level breaches
"""
        
        # Stop any existing watch first
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            await self._broadcast_message("Stopping previous watch...", "system")
        
        # Parse strike price
        try:
            strike = int(parts[1])
        except ValueError:
            return f"Invalid strike price: {parts[1]}"
        
        # Parse option type and expiry
        option_type = None
        expiry_date_str = None
        interval = 30  # default
        
        for part in parts[2:]:
            part_upper = part.upper()
            if part_upper in ["CE", "PE"]:
                option_type = part_upper
            elif part.isdigit():
                interval = max(10, min(300, int(part)))  # 10-300 seconds
            else:
                expiry_date_str = part.lower()
        
        if not option_type:
            return "Please specify CE or PE. Example: watch 25000 CE 27jan"
        
        if not expiry_date_str:
            return "Please specify expiry date. Example: watch 25000 CE 27jan"
        
        # Store watch parameters
        self._watch_strike = strike
        self._watch_option_type = option_type
        self._watch_expiry = expiry_date_str
        self._watch_interval = interval
        
        # Start the watch task
        self._watch_task = asyncio.create_task(self._watch_loop())
        
        return f"""
================================================================
  WATCHING: {strike} {option_type} ({expiry_date_str.upper()})
================================================================
  Update Interval: Every {interval} seconds
  
  Type 'stop' or 'unwatch' to stop monitoring.
  
  Starting first analysis...
================================================================
"""
    
    async def _watch_loop(self):
        """Background loop for continuous strike monitoring"""
        last_decision = None
        last_price = None
        update_count = 0
        
        try:
            while True:
                update_count += 1
                
                # Get current market data
                signal = self.analyzer.analyze()
                if not signal:
                    await self._broadcast_message("Could not fetch market data. Retrying...", "error")
                    await asyncio.sleep(self._watch_interval)
                    continue
                
                spot_price = signal.current_price
                trend = signal.trend.value
                rsi = signal.rsi
                
                analysis = self.research.analyze_specific_strike(
                    strike=self._watch_strike,
                    option_type=self._watch_option_type,
                    spot_price=spot_price,
                    trend=trend,
                    rsi=rsi,
                    expiry_date_str=self._watch_expiry,
                    index_name=self._active_index.name,
                )
                
                # Determine decision
                confidence = analysis['confidence']
                recommendation = analysis['recommendation']
                
                if recommendation == "BUY" and confidence >= 65:
                    decision = "GO"
                elif recommendation == "RISKY BUY" and confidence >= 50:
                    decision = "CAUTION"
                elif recommendation == "AVOID":
                    decision = "NO-GO"
                else:
                    decision = "NO-GO"
                
                # Calculate price change
                price_change = ""
                if last_price:
                    change = spot_price - last_price
                    pct = (change / last_price) * 100
                    arrow = "^" if change > 0 else "v" if change < 0 else "-"
                    price_change = f" ({arrow} {abs(change):.2f}, {pct:+.2f}%)"
                
                # Build update message
                decision_changed = last_decision and decision != last_decision
                
                update_msg = f"""
----------------------------------------------------------------
  WATCH UPDATE #{update_count} | {datetime.now().strftime('%H:%M:%S')}
----------------------------------------------------------------
  {self._watch_strike} {self._watch_option_type} ({self._watch_expiry.upper()})
  
  Spot: Rs.{spot_price:,.2f}{price_change}
  Trend: {trend} | RSI: {rsi:.1f}
  
  >>> {decision} {'<<<' if decision == 'GO' else ''}
  Confidence: {confidence}%
  {analysis['reason']}
"""
                
                if decision_changed:
                    update_msg += f"""
  *** DECISION CHANGED: {last_decision} -> {decision} ***
"""
                
                # Check for key level breach
                if spot_price > analysis.get('resistance_levels', [0])[0]:
                    update_msg += f"  !! RESISTANCE BREACHED: {analysis['resistance_levels'][0]}\n"
                elif spot_price < analysis.get('support_levels', [99999])[0]:
                    update_msg += f"  !! SUPPORT BREACHED: {analysis['support_levels'][0]}\n"
                
                await self._broadcast_message(update_msg, "analysis")
                
                # Update tracking
                last_decision = decision
                last_price = spot_price
                
                # Wait for next update
                await asyncio.sleep(self._watch_interval)
                
        except asyncio.CancelledError:
            await self._broadcast_message(
                f"Watch stopped for {self._watch_strike} {self._watch_option_type}",
                "system"
            )
        except Exception as e:
            logger.error(f"Watch error: {e}")
            await self._broadcast_message(f"Watch error: {e}", "error")
    
    async def _stop_watch(self) -> str:
        """Stop the current watch task"""
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            
            strike_info = f"{self._watch_strike} {self._watch_option_type}"
            self._watch_task = None
            self._watch_strike = None
            self._watch_option_type = None
            self._watch_expiry = None
            
            return f"Stopped watching {strike_info}"
        else:
            return "No active watch to stop."
    
    def _handle_backtest(self, parts: List[str]) -> str:
        """Run strategy backtest: backtest [period]"""
        period = "3mo"
        if len(parts) > 1:
            p = parts[1].lower()
            period_map = {"1m": "1mo", "3m": "3mo", "6m": "6mo", "1y": "1y", "2y": "2y"}
            period = period_map.get(p, p)

        result = backtester.run(period)
        if not result:
            return f"Backtest failed for {self._active_index.display_name} -- insufficient data or fetch error."
        trade_journal.log_alert(f"Backtest run: {self._active_index.name} {period}, WR={result.win_rate}%, PF={result.profit_factor}")
        return backtester.format_result(result)

    def _handle_regime(self) -> str:
        """Detect current market regime."""
        result = regime_detector.analyze()
        if not result:
            return "Could not analyze market regime. Data fetch failed."
        trade_journal.log_regime(result.regime.value, result.adx, result.should_trade)
        return regime_detector.format_analysis(result)

    def _handle_theta(self, parts: List[str]) -> str:
        """Calculate theta decay: theta <premium> <days>"""
        if len(parts) < 3:
            return (
                "Usage: theta <premium> <days_to_expiry>\n"
                "Example: theta 150 3  (Rs.150 premium, 3 days to expiry)\n"
                "         theta 85 1   (Rs.85 premium, 1 day to expiry)"
            )
        try:
            premium = float(parts[1])
            days = float(parts[2])
        except ValueError:
            return "Invalid numbers. Usage: theta <premium> <days>"

        result = theta_clock.calculate(premium, days)
        return theta_clock.format_report(result)

    def _handle_mtf(self) -> str:
        """Run multi-timeframe confluence analysis."""
        result = mtf_engine.analyze()
        if not result:
            return "Multi-timeframe analysis failed -- could not fetch data for enough timeframes."
        if result.is_confirmed:
            trade_journal.log_signal(
                result.confluence_direction, 0, "MTF",
                result.confluence_score * 33.3, result.recommendation
            )
        return mtf_engine.format_result(result)

    def _handle_journal(self, parts: List[str]) -> str:
        """Show trade journal: journal [review|export]"""
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "review":
                return trade_journal.get_review()
            elif sub == "export":
                return trade_journal.export_csv()
        return trade_journal.get_summary()

    def _handle_orb_command(self, parts: List[str]) -> str:
        """Handle ORB strategy commands: orb / orb on / orb off / orb set window <N>"""
        if len(parts) < 2:
            return self.orb.format_status()

        sub = parts[1].lower()

        if sub in ("on", "enable"):
            settings.orb.enabled = True
            return "ORB strategy ENABLED. Will fire on next breakout during 9:45–11:30 IST."

        if sub in ("off", "disable"):
            settings.orb.enabled = False
            return "ORB strategy DISABLED."

        if sub == "set" and len(parts) >= 4 and parts[2].lower() == "window":
            try:
                mins = int(parts[3])
                if not 5 <= mins <= 60:
                    return "Window must be between 5 and 60 minutes."
                settings.orb.window_minutes = mins
                return f"ORB window set to {mins} minutes (range: 09:15 → {9 + (15 + mins) // 60:02d}:{(15 + mins) % 60:02d} IST)."
            except ValueError:
                return "Usage: orb set window <minutes>  (e.g., orb set window 15)"

        if sub == "status":
            return self.orb.format_status()

        return (
            "Usage:\n"
            "  orb           — show current ORB levels and state\n"
            "  orb on/off    — enable/disable ORB auto-trading\n"
            "  orb set window <N>  — change range window (5–60 min)"
        )

    def _handle_vwap_command(self, parts: List[str]) -> str:
        """Handle VWAP strategy commands: vwap / vwap on / vwap off / vwap set dev <pct>"""
        if len(parts) < 2:
            return self.vwap_strat.format_status()

        sub = parts[1].lower()

        if sub in ("on", "enable"):
            settings.vwap.enabled = True
            return "VWAP strategy ENABLED. Active 9:30–14:30 IST in RANGING markets."

        if sub in ("off", "disable"):
            settings.vwap.enabled = False
            return "VWAP strategy DISABLED."

        if sub == "set" and len(parts) >= 4 and parts[2].lower() == "dev":
            try:
                pct = float(parts[3])
                if not 0.1 <= pct <= 2.0:
                    return "Deviation must be between 0.1% and 2.0%."
                settings.vwap.deviation_pct = pct
                return f"VWAP deviation threshold set to {pct:.2f}%."
            except ValueError:
                return "Usage: vwap set dev <pct>  (e.g., vwap set dev 0.5)"

        if sub == "status":
            return self.vwap_strat.format_status()

        return (
            "Usage:\n"
            "  vwap               — show VWAP levels and state\n"
            "  vwap on/off        — enable/disable VWAP auto-trading\n"
            "  vwap set dev <pct> — change deviation threshold (default 0.4%)"
        )

    def _handle_gap_command(self) -> str:
        """Handle 'gap' command — show today's gap analysis (forces a fresh fetch)."""
        try:
            # Clear cache so we get a fresh read
            self._gap_detector._today_analysis = None
            analysis = self._gap_detector.analyze()
            if not analysis:
                return "Gap analysis unavailable — market may not be open yet or data fetch failed."
            return self._gap_detector.format_report(analysis)
        except Exception as e:
            return f"Gap analysis error: {e}"

    async def _handle_index_command(self, parts: List[str]) -> str:
        """Handle index switching: index nifty / index banknifty / index sensex"""
        if len(parts) < 2:
            names = ", ".join(list_indices())
            return (
                f"Current index: {self._active_index.display_name}\n\n"
                f"Usage: index <name>\n"
                f"Available: {names}\n\n"
                f"Examples:\n"
                f"  index nifty\n"
                f"  index banknifty\n"
                f"  index sensex"
            )

        requested = parts[1].upper()
        idx = get_index(requested)
        if not idx:
            return f"Unknown index '{requested}'. Available: {', '.join(list_indices())}"

        if idx.name == self._active_index.name:
            return f"Already analyzing {idx.display_name}."

        self._active_index = idx
        self.analyzer.set_index(idx)
        self.order_manager.set_active_index(idx)
        self.orb.set_index(idx)
        self.vwap_strat.set_index(idx)
        self.kite.set_active_index(idx)  # propagate to broker so place_order uses correct index

        # Update advanced modules with the new index
        backtester.set_index(idx)
        regime_detector.set_index(idx)
        mtf_engine.set_index(idx)
        theta_clock.set_index(idx)

        self._last_signal = None
        self._last_analysis_time = None

        msg = (
            f"Switched to {idx.display_name}\n"
            f"  Exchange: {idx.exchange} | Lot: {idx.lot_size} | "
            f"Strike interval: {idx.strike_interval} | Expiry: {idx.expiry_day_name}"
        )
        if not idx.weekly_expiry:
            msg += " (Monthly only)"
        await self._broadcast_message(msg, "system")
        return msg

    def _get_strike_help(self) -> str:
        """Get help text for strike command"""
        idx = self._active_index
        today = datetime.now()
        expiries = []
        
        if idx.weekly_expiry:
            days_until = (idx.expiry_weekday - today.weekday()) % 7
            if days_until == 0 and today.hour >= 15:
                days_until = 7
            next_exp = today + timedelta(days=days_until)
            for i in range(4):
                exp_date = next_exp + timedelta(weeks=i)
                label = f"{i+1} week" if i == 0 else f"{i+1} weeks"
                expiries.append(f"  {exp_date.strftime('%d%b').lower()} - {exp_date.strftime('%d %b %Y')} ({label})")
        else:
            from bot.trend_analyzer import TrendAnalyzer
            ref = today
            for _ in range(4):
                last_exp = TrendAnalyzer._last_weekday_of_month(ref, idx.expiry_weekday)
                if last_exp.date() < today.date() or (last_exp.date() == today.date() and today.hour >= 15):
                    if ref.month == 12:
                        ref = ref.replace(year=ref.year + 1, month=1, day=1)
                    else:
                        ref = ref.replace(month=ref.month + 1, day=1)
                    last_exp = TrendAnalyzer._last_weekday_of_month(ref, idx.expiry_weekday)
                ed = last_exp
                expiries.append(f"  {ed.strftime('%d%b').lower()} - {ed.strftime('%d %b %Y')} (monthly)")
                if ref.month == 12:
                    ref = ref.replace(year=ref.year + 1, month=1, day=1)
                else:
                    ref = ref.replace(month=ref.month + 1, day=1)

        lo, hi = idx.strike_range
        return f"""
Usage: strike <price> <CE/PE> <expiry>

Active Index: {idx.display_name} ({idx.exchange})
Strike Range: {lo} - {hi} (interval: {idx.strike_interval})

Examples:
  strike {lo + idx.strike_interval * 10} CE {expiries[0].split(' - ')[0].strip()}
  strike {lo + idx.strike_interval * 8} PE {expiries[1].split(' - ')[0].strip() if len(expiries) > 1 else ''}

Upcoming Expiry Dates ({idx.expiry_day_name}):
{chr(10).join(expiries)}

Format for expiry: <day><month> (e.g., 27jan, 3feb, 10feb)

The analysis shows:
  - GO / NO-GO decision
  - Days to expiry
  - Estimated premium
  - Moneyness (ITM/ATM/OTM)
  - Risk assessment
"""
    
    def _get_rule_help_text(self) -> str:
        """Get help text for rule commands"""
        return """
================================================================
                  CUSTOM RULES HELP                            
================================================================
  MANAGING RULES:
    rule list              - Show all rules
    rule add <instruction> - Add new rule
    rule enable <id>       - Enable a rule
    rule disable <id>      - Disable a rule
    rule remove <id>       - Delete a rule
    rule reset             - Reset daily trigger counts

  QUICK ADD (shortcut):
    if <condition> then <action>
    when <condition> <action>

  CONDITIONS (can combine with 'and'):
    rsi < 30           - RSI below value
    rsi > 70           - RSI above value
    price < 24000      - Price below value
    price > 24500      - Price above value
    trend is bullish   - Bullish trend
    trend is bearish   - Bearish trend
    strength > 60      - Signal strength above
    between 10:00-14:00 - Time range

  ACTIONS:
    buy ce              - Buy Call (ATM strike)
    buy ce at 24000     - Buy Call at specific strike
    buy pe              - Buy Put (ATM strike)
    buy pe at 23900 qty 100 - Buy Put with quantity
    sell all            - Close all positions
    alert <message>     - Show alert message
    pause               - Pause auto-trading

  EXAMPLES:
    rule add if rsi < 30 and trend is bullish then buy ce
    if rsi > 70 then alert RSI overbought
    when price > 24500 buy pe at 24500
================================================================
"""
    
    def _get_help_text(self) -> str:
        """Get help text for available commands"""
        idx = self._active_index
        return f"""
================================================================
                    AVAILABLE COMMANDS                         
================================================================
  ACTIVE INDEX: {idx.display_name} ({idx.exchange})
  
  INDEX SWITCHING:
    index nifty       - Switch to NIFTY 50
    index banknifty   - Switch to BANK NIFTY
    index sensex      - Switch to SENSEX
    index             - Show current index
    
  OPTIONS ANALYSIS (Deep research runs automatically!):
    status        - Show bot status and last analysis
    analyze       - Run immediate trend analysis
    research      - View smart strike recommendations
    strike <price> [CE/PE] - Analyze specific strike
    
  OPTIONS TRADING (auto deep-research before execution):
    buy CE [strike] [qty]  - Buy Call (deep research first)
    buy PE [strike] [qty]  - Buy Put (deep research first)
    buy CE                 - Uses recommended ATM strike
    sell <symbol>          - Exit a position
    close all              - Close all positions
    positions              - Show open positions
    
  STRIKE ANALYSIS (specify exact expiry date):
    strike <price> CE <expiry>  - Analyze Call at specific strike
    strike <price> PE <expiry>  - Analyze Put at specific strike
    strike <price> CE           - Default: next expiry
    
  CONTINUOUS MONITORING (watch until stopped):
    watch <price> CE <expiry>   - Monitor every 30 seconds
    watch <price> PE <expiry> 60 - Monitor every 60 seconds
    stop / unwatch              - Stop watching
    
  EQUITY/STOCKS (research only, no execution):
    stock <symbol>   - Analyze any stock (e.g., stock RELIANCE)
    screen bullish   - Find stocks with buy signals
    screen oversold  - Find stocks with RSI < 30
    screen overbought - Find stocks with RSI > 70
    
  CUSTOM RULES (auto deep-research on trigger):
    rule list              - Show all custom rules
    rule add <instruction> - Add new rule
    rule help              - Detailed rules help
    
  ADVANCED ANALYSIS:
    orb               - Opening Range Breakout status / levels
    orb on/off        - Enable/disable ORB auto-trading
    vwap              - VWAP Mean Reversion status / levels
    vwap on/off       - Enable/disable VWAP auto-trading (RANGING days)
    vwap set dev <N>  - Deviation threshold % (default 0.4)
    gap               - Today's gap-up/gap-down analysis
    journal           - Today's session summary
    journal review    - Post-market review with lessons
    journal export    - Export journal to CSV
    
  CONTROL:
    pause / resume    - Pause/resume auto-trading
    
  SETTINGS:
    set sl <pct>     - Set stop-loss percentage
    set target <pct> - Set target percentage
    set qty <num>    - Set default quantity
    
  OTHER:
    daily         - Show daily summary
    help          - Show this help
================================================================
"""
    
    def _get_status_text(self) -> str:
        """Get current status text"""
        status = self.get_status()
        
        signal_info = "No analysis yet"
        if status.last_signal:
            signal_info = f"{status.last_signal.trend.value} (strength: {status.last_signal.strength:.0f}%)"
        
        idx = self._active_index
        return f"""
================================================================
                      BOT STATUS                               
================================================================
  Active Index: {idx.display_name} ({idx.exchange})
  State: {status.state.value}
  Auto-Trade: {'ENABLED' if status.auto_trade_enabled else 'DISABLED'}
  
  Last Analysis: {status.last_analysis.strftime('%H:%M:%S') if status.last_analysis else 'N/A'}
  Current Trend: {signal_info}
  
  Open Positions: {status.open_positions}
  Daily P&L: Rs.{status.daily_pnl:,.2f}
  
  Kite Login: {'Connected' if self.kite.is_logged_in() else 'Not Connected'}
  
  ORB Strategy : {'ENABLED' if settings.orb.enabled else 'DISABLED'}  |  State: {self.orb.get_status()['state']}
  VWAP Strategy: {'ENABLED' if settings.vwap.enabled else 'DISABLED'}  |  State: {self.vwap_strat.get_status()['state']}
================================================================
"""
    
    def get_status(self) -> BotStatus:
        """Get current bot status"""
        open_positions = 0
        daily_pnl = 0.0
        
        if self.order_manager:
            open_positions = len([
                p for p in self.order_manager._positions.values() 
                if p.status == "OPEN"
            ])
            # Calculate total P&L including unrealized positions
            # Use cached current prices from position monitor
            daily_pnl = self.order_manager.calculate_total_pnl(self._current_prices if self._current_prices else None)
        
        return BotStatus(
            state=self._state,
            auto_trade_enabled=self._auto_trade,
            last_analysis=self._last_analysis_time,
            last_signal=self._last_signal,
            open_positions=open_positions,
            daily_pnl=daily_pnl,
            message="OK" if self._state == BotState.RUNNING else str(self._state.value)
        )
    
    async def shutdown(self):
        """Shutdown the bot gracefully"""
        logger.info("Shutting down bot...")
        await self._broadcast_message("Bot shutting down...", "system")
        
        # Stop analysis loop
        await self.stop_analysis_loop()
        
        # Close all positions if requested
        # await self._handle_close_all()  # Uncomment for safety
        
        # Close browser
        await self.kite.close()
        
        self._state = BotState.STOPPED
        logger.info("Bot shutdown complete")


def create_bot() -> TradingBot:
    """Create and return a new TradingBot instance"""
    return TradingBot()
