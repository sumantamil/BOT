"""
Trading Bot Engine

Main orchestration module that coordinates:
- Market trend analysis
- Order execution
- Position monitoring
- Chat command processing
"""

import asyncio
import os
import re
from typing import Optional, Callable, List, Dict
from datetime import datetime, timedelta, date
from enum import Enum
from dataclasses import dataclass
from loguru import logger
import httpx

# ── Persistent error log file ────────────────────────────────────────────────
# Every ERROR/CRITICAL line is written here in addition to the console.
# backtrace=True  → full call stack printed with each exception.
# diagnose=True   → local variable values printed beside each frame.
# enqueue=True    → thread-safe writes from async callbacks.
_LOG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logger.add(
    os.path.join(_LOG_DIR, "errors.log"),
    level="ERROR",
    rotation="10 MB",
    retention="7 days",
    backtrace=True,
    diagnose=True,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
    enqueue=True,
)

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
from bot.index_config import IndexConfig, NIFTY, BANKNIFTY, SENSEX, get_index, list_indices
from bot import profit_tracker
from bot.orb_strategy import ORBStrategy, ORBSignal
from bot.vwap_strategy import VWAPStrategy, VWAPSignal
from bot.gap_detector import (
    gap_detector, GapType,
    GapPlaybook, GapFadeStrategy, GapContinuationStrategy,
    calculate_gap_size, get_gap_signal,
)
from bot.iv_monitor import iv_monitor
from bot.paper_trader import PaperTrader
from bot.straddle_strategy import StraddleStrategy
from bot.late_day_oscillation import LateDayOscillationStrategy, OscillationSignal
from bot.entry_filters import HighProbabilityFilter, build_market_data
from bot.exit_manager import ExitManager, DynamicStopLoss
from bot.position_sizer import PositionSizer
from bot.performance_optimizer import PerformanceOptimizer, get_performance_optimizer
from bot.telegram_handler import TelegramCommandHandler
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


class EODTracker:
    """
    Tracks EOD Closing Momentum strategy win/loss results.

    Persists to .eod_performance.json so history survives restarts.
    After 20 trades, logs a CRITICAL warning if win rate < 55%.
    After 20 trades with win rate < 45%, logs an auto-disable recommendation.
    """

    _PERF_FILE = ".eod_performance.json"
    _WARN_THRESHOLD = 0.55   # warn below this win rate
    _MIN_TRADES = 20         # minimum trades before statistical warning fires

    def __init__(self):
        import os, json as _j
        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._path = os.path.join(_base, self._PERF_FILE)
        self._results: list[bool] = []
        self._load()

    def _load(self):
        import os, json as _j
        try:
            if os.path.exists(self._path):
                data = _j.loads(open(self._path).read())
                self._results = [bool(v) for v in data.get("results", [])]
        except Exception:
            pass

    def _save(self):
        import json as _j
        try:
            with open(self._path, "w") as f:
                _j.dump({"results": self._results}, f)
        except Exception as e:
            logger.debug(f"EODTracker save failed: {e}")

    def record(self, win: bool):
        """Record an EOD trade outcome and emit warnings when warranted."""
        self._results.append(win)
        self._save()
        n = len(self._results)
        if n >= self._MIN_TRADES:
            wr = sum(self._results) / n
            if wr < self._WARN_THRESHOLD:
                logger.critical(
                    f"⚠️  EOD win rate: {wr * 100:.1f}% over {n} trades "
                    f"(target ≥ {self._WARN_THRESHOLD * 100:.0f}%). "
                    f"Consider setting EOD_ENABLED=false."
                )
            else:
                logger.info(f"EOD performance: {wr * 100:.1f}% win rate over {n} trades ✅")

    def win_rate(self) -> float:
        """Return win rate (0.0–1.0); -1.0 if fewer than MIN_TRADES."""
        if len(self._results) < self._MIN_TRADES:
            return -1.0
        return sum(self._results) / len(self._results)

    def count(self) -> int:
        return len(self._results)


_eod_tracker = EODTracker()


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

        # Per-index one-trade-per-day guards.
        # Using dicts keyed by index name so NIFTY/BANKNIFTY/SENSEX get independent guards.
        # Date values auto-reset on a new calendar day; bool values reset in the daily-reset block.
        self._index_orb_triggered: Dict[str, Optional[date]] = {}
        self._index_vwap_triggered: Dict[str, Optional[date]] = {}
        self._index_gap_traded: Dict[str, bool] = {}
        self._index_gap_direction: Dict[str, str] = {}   # "UP" / "DOWN" per index for today
        self._index_gap_fail_count: Dict[str, int] = {}  # consecutive order failures per index
        self._index_eod_triggered: Dict[str, Optional[date]] = {}
        self._index_trend_triggered: Dict[str, Optional[date]] = {}  # paper TREND 1/day guard
        self._index_day_direction: Dict[str, str] = {}  # "CE" or "PE" — first direction fired today per index

        # Path for persisting daily trigger flags across same-day restarts
        self._daily_state_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "daily_trade_state.json"
        )
        # Restore triggers from previous run on same day (prevents re-firing already-used slots)
        self._restore_daily_state()

        # Regime cache — per-index, re-fetch at most once per 30 min (daily data, expensive)
        self._index_regime_cache: Dict[str, object] = {}
        self._index_regime_time: Dict[str, Optional[datetime]] = {}
        self._regime_cache_seconds: int = 30 * 60  # 30 minutes
        # Convenience pointer updated each loop iteration (used by _check_vwap_signal)
        self._cached_regime = None
        
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

        # Per-index strategy instances — all three indices scanned in every cycle
        self._index_analyzers: Dict[str, TrendAnalyzer] = {
            "NIFTY":      self.analyzer,
            "BANKNIFTY":  TrendAnalyzer(index_config=BANKNIFTY),
            "SENSEX":     TrendAnalyzer(index_config=SENSEX),
        }
        self._index_orbs: Dict[str, ORBStrategy] = {
            "NIFTY":      self.orb,
            "BANKNIFTY":  ORBStrategy(index_config=BANKNIFTY),
            "SENSEX":     ORBStrategy(index_config=SENSEX),
        }
        self._index_vwaps: Dict[str, VWAPStrategy] = {
            "NIFTY":      self.vwap_strat,
            "BANKNIFTY":  VWAPStrategy(index_config=BANKNIFTY),
            "SENSEX":     VWAPStrategy(index_config=SENSEX),
        }
        # Per-index last signal cache (used to restore context after loop)
        self._index_last_signal: Dict[str, Optional[TrendSignal]] = {}
        # Per-index last analysis timestamp (for accurate status display)
        self._index_last_analysis_time: Dict[str, Optional[datetime]] = {}
        # User's manually-selected index (survives analysis-loop context swaps)
        self._user_selected_index: IndexConfig = NIFTY

        # Paper trading tracker — records entries/exits and P&L in paper mode
        self.paper_trader = PaperTrader()
        self.paper_trader.configure(
            sl_pct=float(getattr(settings.trading, 'stop_loss_percentage', 20.0)),
            target_pct=float(getattr(settings.trading, 'target_percentage', 50.0)),
            alert_cb=self._send_telegram_alert,
            hard_time_exit_hour=int(getattr(settings.trading, 'hard_time_exit_hour', 13)),
            time_exit_only_losers=bool(getattr(settings.trading, 'time_exit_only_losers', True)),
            time_exit_min_profit=float(getattr(settings.trading, 'time_exit_min_profit', 50.0)),
        )
        # Current prices per index — updated each analysis cycle for paper exit checks
        self._paper_index_prices: Dict[str, float] = {}

        # Long Straddle / Strangle strategy
        self.straddle_strategy = StraddleStrategy()

        # Late-Day Oscillation strategy (14:30–15:25 IST) — disabled by default
        # Validate pattern first: python analysis/late_day_oscillation_validator.py
        self.late_day_strat = LateDayOscillationStrategy()
        try:
            settings.late_day.validate_config()
        except ValueError as _lde:
            logger.error(f"Late-day config error (strategy disabled): {_lde}")
            settings.late_day.enabled = False

        # ── Phase 2 profitable-entry infrastructure ──────────────────────────
        # 1. High-probability entry filter (scoring gate before any trade)
        self.entry_filter = HighProbabilityFilter(
            min_score=settings.entry_filter.min_confidence_score
        )
        # 2. Dynamic stop-loss calculator (strategy-specific stop placement)
        self.dynamic_sl = DynamicStopLoss()
        # 3. Kelly + confidence position sizer (initialised with placeholder balance;
        #    updated at the start of each analysis cycle from order_manager)
        self.position_sizer: Optional[PositionSizer] = None   # created lazily after kite init
        # 4. Performance optimiser (loads historical journal files)
        self.perf_optimizer = get_performance_optimizer()
        # Per-position ExitManager instances: trade_id → ExitManager
        self._exit_managers: Dict[str, ExitManager] = {}

        # Telegram command handler (started lazily in start_analysis_loop)
        self._telegram_handler: Optional["TelegramCommandHandler"] = None

        # Pause flag set by Telegram /pause command — toggles self._auto_trade directly.
        # _auto_trade_paused is intentionally not used; pause/resume just flip _auto_trade.

        # Pre-market Gift Nifty prediction (set once per morning; reset each day)
        self._gift_nifty_predicted: bool = False

        # Filter block counters — incremented each time a signal is rejected by a filter.
        # Reset each new trading day along with other daily stats.
        # Keys: "ORB_strength", "ORB_trend", "ORB_RSI", "ORB_MTF", "ORB_daily_limit",
        #       "VWAP_strength", "VWAP_regime", "VWAP_5m_trend", "VWAP_daily_limit",
        #       "VIX", "IV", "position_limit"
        self._filter_blocks: Dict[str, int] = {}

        # Cached India VIX — fetched once per analysis cycle (avoids repeated yfinance calls).
        # Used to annotate signal logs even in paper mode where the VIX filter doesn't block.
        self._cached_vix: float = 0.0
        self._cached_vix_time: Optional[datetime] = None

        # AI macro-sentiment cache (PROMPT 2) — refreshed once per trading session boundary.
        # Sessions: Morning (9:15), Midday (11:30), Afternoon (13:00), Close (15:00).
        # UNFAVORABLE result suppresses new auto-trade entries for that session.
        self._ai_sentiment = None           # MarketSentimentResult
        self._ai_sentiment_session: str = ""  # last session name that was assessed

        # Error counters — incremented each time a component raises an unexpected exception.
        # Displayed in [HEARTBEAT] logs so you immediately see if something is silently crashing.
        # Reset each new trading day alongside _filter_blocks.
        # Keys: "analysis_loop", "position_monitor", "data_fetch", "gap", "orb", "vwap", "eod"
        self._error_counts: Dict[str, int] = {}

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
        """Send alert to primary (and optional secondary) Telegram channel"""
        # For error/warning messages that may contain raw API responses (curly braces,
        # underscores, etc.) switch to plain text to avoid Markdown parse failures.
        _md_safe_types = {"info", "trade_entry", "trade_exit", "gap_analysis", "paper_trade", "system"}
        _parse_mode = "Markdown" if alert_type in _md_safe_types else None

        async def _post(bot_token: str, chat_id: str, label: str) -> None:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload: dict = {"chat_id": chat_id, "text": message}
            if _parse_mode:
                payload["parse_mode"] = _parse_mode
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        url,
                        json=payload,
                        timeout=10,
                    )
                if response.status_code != 200:
                    logger.error(f"Telegram {label} send failed: {response.status_code} - {response.text}")
                else:
                    logger.debug(f"Telegram {label} alert sent: {alert_type}")
            except Exception as e:
                logger.error(f"Error sending Telegram {label} alert: {e}")

        # Primary channel
        if self.config.alert.telegram_enabled:
            if not self.config.alert.telegram_bot_token or not self.config.alert.telegram_chat_id:
                logger.warning("Primary Telegram not properly configured")
            else:
                await _post(
                    self.config.alert.telegram_bot_token,
                    self.config.alert.telegram_chat_id,
                    "primary",
                )

        # Secondary channel
        if self.config.alert.telegram2_enabled:
            if not self.config.alert.telegram2_bot_token or not self.config.alert.telegram2_chat_id:
                logger.warning("Secondary Telegram not properly configured")
            else:
                await _post(
                    self.config.alert.telegram2_bot_token,
                    self.config.alert.telegram2_chat_id,
                    "secondary",
                )
    
    async def initialize(self):
        """Initialize all bot components"""
        logger.info("Initializing Trading Bot...")
        self._state = BotState.STARTING
        
        await self._broadcast_message("Initializing bot...", "system")
        
        try:
            # Initialize browser
            await self.kite.initialize(headless=False)

            # Wire Dhan token-expired Telegram alert
            if hasattr(self.kite, '_token_expired_cb'):
                self.kite._token_expired_cb = self._send_telegram_alert

            # Initialize order manager
            self.order_manager = OrderManager(self.kite)

            # Initialise position sizer (needs account balance — from trading config)
            _account_balance = float(
                getattr(settings.trading, "account_balance", 100_000)
                or 100_000
            )
            if settings.position_sizer.enabled:
                self.position_sizer = PositionSizer(
                    account_balance=_account_balance,
                    max_risk_per_trade_pct=settings.position_sizer.max_risk_per_trade_pct,
                    kelly_fraction=settings.position_sizer.kelly_fraction,
                    strategy_stats=self.perf_optimizer.get_strategy_stats(),
                )
                logger.info(
                    f"[PositionSizer] Enabled  balance=₹{_account_balance:,.0f}  "
                    f"max_risk={settings.position_sizer.max_risk_per_trade_pct}%  "
                    f"kelly_fraction={settings.position_sizer.kelly_fraction}"
                )
            else:
                logger.info("[PositionSizer] Disabled — using default_quantity")

            # Recover any open positions from broker API (survives restarts)
            await self._recover_open_positions()

            # Always start position monitor so recovered positions get SL/target/auto-close
            # monitoring immediately, regardless of whether auto-trade or analysis loop is on.
            await self.start_position_monitor()

            # Validate all command handlers exist — catches missing methods at startup
            # rather than failing silently when a user types a command mid-session.
            missing = self._validate_command_handlers()
            if missing:
                warn = f"⚠️ BOT STARTUP WARNING: {len(missing)} missing command handler(s): {', '.join(missing)}"
                logger.error(warn)
                await self._broadcast_message(warn, "error")
                await self._send_telegram_alert(warn, "startup")

            self._state = BotState.RUNNING
            await self._broadcast_message("Bot initialized successfully!", "success")
            logger.info("Bot initialization complete")

            # Back-fill any missed EOD settlements (runs if bot was down at 3:30 PM)
            asyncio.create_task(self._catchup_missed_settlements())

            await self._send_telegram_alert("✅ NIFTY Trading Bot started successfully!", "startup")

            # ── Local AI warmup: pre-load model into RAM in the background ─────
            # This prevents the first real trading signal from paying the cold-start
            # penalty (~60s on CPU). The warmup runs async so it doesn't delay startup.
            try:
                from bot.local_ai_service import get_ai_service
                _ai = get_ai_service()
                if _ai.enabled:
                    asyncio.create_task(_ai.warmup())
                    logger.info("[LocalAI] Background warmup scheduled")
            except Exception:
                pass
            
        except Exception as e:
            self._state = BotState.ERROR
            error_msg = f"Initialization failed: {e}"
            logger.error(error_msg)
            await self._broadcast_message(error_msg, "error")
            raise

    # ------------------------------------------------------------------
    # Startup catch-up: back-fill missed EOD settlements
    # ------------------------------------------------------------------

    async def _catchup_missed_settlements(self) -> None:
        """
        On startup, scan the last 10 trading days for Dhan settlements that
        were not recorded in profit_tracking.csv (e.g. bot was down at 3:30 PM).
        For each missing day, record P&L from Dhan settlement and sync Google Sheets.
        Runs as a background task 30 s after init to avoid slowing startup.
        """
        await asyncio.sleep(30)   # Give the bot time to fully settle before hitting Dhan API

        try:
            from config import settings as _cfg
            if str(_cfg.broker).lower() != "dhan":
                return

            from datetime import date as _date, timedelta as _td
            import bot.profit_tracker as _pt

            # Load already-recorded dates from CSV
            existing_rows = _pt._read_csv_rows()
            recorded_dates = {r["date"] for r in existing_rows if r.get("date")}

            # Pull last 10 trading days from Dhan ledger settlements
            from_dt = (_date.today() - _td(days=14)).isoformat()
            to_dt   = (_date.today() - _td(days=1)).isoformat()  # exclude today (not settled yet)

            from dhanhq import dhanhq as _dhan
            _dc = _dhan(_cfg.dhan.client_id, _cfg.dhan.access_token)
            lr = await asyncio.to_thread(_dc.ledger_report, from_dt, to_dt)

            missed = []
            for row in (lr.get("data") or []):
                if row.get("narration") != "Trades Executed":
                    continue
                vdate = row.get("voucherdate", "")   # "Mar 16, 2026"
                try:
                    day = datetime.strptime(vdate, "%b %d, %Y").date()
                except Exception:
                    continue
                if day.weekday() >= 5:   # skip weekends
                    continue
                day_str = day.isoformat()
                if day_str in recorded_dates:
                    continue   # already recorded — skip

                credit = float(row.get("credit") or 0)
                debit  = float(row.get("debit")  or 0)
                net    = round(credit - debit, 2)
                missed.append((day, day_str, net))

            if not missed:
                logger.debug("Startup catch-up: no missed settlements found")
                return

            logger.info(f"Startup catch-up: {len(missed)} missed day(s) to back-fill from Dhan settlements")

            for day, day_str, net in sorted(missed):
                try:
                    _pt.record_daily_pnl(
                        gross_pnl=net,
                        total_trades=0,   # trade count not available from settlement alone
                        wins=0,
                        losses=0,
                        trade_date=day,
                        notes=f"Back-filled from Dhan settlement (bot was down at EOD)",
                    )
                    logger.info(f"Catch-up: recorded {day_str} — ₹{net:,.2f} from Dhan settlement")
                except Exception as e:
                    logger.warning(f"Catch-up: failed to record {day_str}: {e}")

            # Sync to Google Sheets after all back-fills
            try:
                _pt.sync_to_google_sheets()
                logger.info("Catch-up: Google Sheets synced ✅")
            except Exception as e:
                logger.warning(f"Catch-up: Sheets sync failed: {e}")

        except Exception as e:
            logger.debug(f"Startup catch-up failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Daily state persistence (survives same-day bot restarts)
    # ------------------------------------------------------------------

    def _save_daily_state(self) -> None:
        """Write per-index one-trade-per-day trigger flags to disk."""
        import json as _j
        today_str = str(datetime.now().date())
        data = {
            "date": today_str,
            "orb":   {k: str(v) for k, v in self._index_orb_triggered.items()  if v},
            "vwap":  {k: str(v) for k, v in self._index_vwap_triggered.items() if v},
            "trend": {k: str(v) for k, v in self._index_trend_triggered.items() if v},
            "eod":   {k: str(v) for k, v in self._index_eod_triggered.items()  if v},
            "gap":   {k: v for k, v in self._index_gap_traded.items() if v},
            "day_direction": dict(self._index_day_direction),
        }
        try:
            with open(self._daily_state_path, "w") as f:
                _j.dump(data, f)
        except Exception as e:
            logger.debug(f"Daily state save failed (non-fatal): {e}")

    def _restore_daily_state(self) -> None:
        """Re-load today's trigger flags from disk after a same-day restart."""
        import json as _j
        if not os.path.exists(self._daily_state_path):
            return
        try:
            with open(self._daily_state_path) as f:
                data = _j.load(f)
            today_str = str(datetime.now().date())
            if data.get("date") != today_str:
                # File is from a previous day — ignore it
                return
            today = datetime.now().date()
            for k, v in data.get("orb", {}).items():
                try:
                    if str(date.fromisoformat(v)) == today_str:
                        self._index_orb_triggered[k] = today
                except Exception:
                    pass
            for k, v in data.get("vwap", {}).items():
                try:
                    if str(date.fromisoformat(v)) == today_str:
                        self._index_vwap_triggered[k] = today
                except Exception:
                    pass
            for k, v in data.get("trend", {}).items():
                try:
                    if str(date.fromisoformat(v)) == today_str:
                        self._index_trend_triggered[k] = today
                except Exception:
                    pass
            for k, v in data.get("eod", {}).items():
                try:
                    if str(date.fromisoformat(v)) == today_str:
                        self._index_eod_triggered[k] = today
                except Exception:
                    pass
            self._index_gap_traded    = {k: bool(v) for k, v in data.get("gap", {}).items()}
            self._index_day_direction = data.get("day_direction", {})
            restored = (
                list(self._index_orb_triggered.keys()) +
                list(self._index_vwap_triggered.keys()) +
                list(self._index_trend_triggered.keys())
            )
            if restored:
                logger.info(
                    f"Daily state restored from previous run: "
                    f"ORB={list(self._index_orb_triggered.keys())} "
                    f"VWAP={list(self._index_vwap_triggered.keys())} "
                    f"TREND={list(self._index_trend_triggered.keys())}"
                )
        except Exception as e:
            logger.debug(f"Daily state restore failed (non-fatal): {e}")

    def _validate_command_handlers(self) -> list:
        """
        Check that every _handle_* method referenced in process_command actually
        exists on this object. Called at startup — returns list of missing names.
        Any missing handler would cause an AttributeError when that command is used.
        """
        required = [
            "_handle_buy_command",
            "_handle_sell_command",
            "_handle_close_all",
            "_handle_set_command",
            "_handle_rule_command",
            "_handle_add_rule",
            "_handle_research_command",
            "_handle_stock_command",
            "_handle_screen_command",
            "_handle_strike_command",
            "_handle_watch_command",
            "_handle_backtest",
            "_handle_regime",
            "_handle_theta",
            "_handle_mtf",
            "_handle_journal",
            "_handle_orb_command",
            "_handle_vwap_command",
            "_handle_gap_command",
            "_handle_index_command",
            "_handle_iv_command",
            "_handle_validate_command",
            "_handle_lateday_command",
            "_handle_perf_command",
            "_stop_watch",
        ]
        missing = [name for name in required if not callable(getattr(self, name, None))]
        if not missing:
            logger.info("Command handler validation: all handlers present ✓")
        return missing

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
                # Also skip if a live non-recovered position already tracks this symbol
                # (e.g. a VWAP trade placed just before crash). Prevents duplicate tracking
                # that causes perpetual LTP-unavailable loops after close_position.
                # Normalise both sides by stripping dashes and spaces before comparing.
                _sym_norm = symbol.upper().replace("-", "").replace(" ", "")
                if any(
                    t.symbol.upper().replace("-", "").replace(" ", "") == _sym_norm
                    and t.status == "OPEN"
                    for t in self.order_manager._positions.values()
                ):
                    logger.info(f"Position recovery: skipping {symbol} — already tracked under a different trade_id")
                    continue
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
            #
            # Key the flags on the actual recovered symbols' index — not on
            # _active_index (which defaults to NIFTY and would wrongly block/unblock
            # the wrong index if the bot restarted after a BANKNIFTY trade).
            total_today = closed_trades_today + recovered
            if total_today > 0:
                # Collect distinct indices represented in recovered positions
                _recovered_indices: set = set()
                for _t in self.order_manager._positions.values():
                    if _t.status == "OPEN":
                        for _idx_name in ["BANKNIFTY", "SENSEX", "NIFTY"]:
                            if _t.symbol.upper().startswith(_idx_name):
                                _recovered_indices.add(_idx_name)
                                break
                # If we can't parse the index (old format), fall back to user's selection
                if not _recovered_indices:
                    _recovered_indices.add(self._active_index.name)

                for _idx_name in _recovered_indices:
                    self._index_orb_triggered[_idx_name] = datetime.now().date()
                if hasattr(self, 'orb') and self.orb:
                    self.orb._signal_fired = True
                    from bot.orb_strategy import ORBState
                    self.orb._state = ORBState.TRIGGERED
                if total_today >= 2:
                    # Both trade slots used — block VWAP re-entry too
                    for _idx_name in _recovered_indices:
                        self._index_vwap_triggered[_idx_name] = datetime.now().date()
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
        """Login — Dhan uses a permanent API key, no interactive login needed."""
        if not hasattr(self.kite, 'login'):
            await self._broadcast_message(
                "Dhan broker authenticates via API key — no manual login required.", "system"
            )
            return True

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

        # Start Telegram command handler in background
        self._telegram_handler = TelegramCommandHandler(self)
        asyncio.create_task(self._telegram_handler.start_polling())
        
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
        """Main analysis loop - runs periodically with regime-aware strategy selection.
        
        Each cycle scans ALL three indices (NIFTY, BANKNIFTY, SENSEX) for signals.
        After every cycle the active context is restored to the user-selected index.
        """
        interval = settings.trend.analysis_interval_seconds

        logger.info(f"Starting analysis loop (interval: {interval}s, scanning NIFTY + BANKNIFTY + SENSEX)")
        _last_loop_date = None
        _tick = 0                          # counts every loop iteration
        _last_heartbeat_tick = -1          # last tick we printed a heartbeat
        _heartbeat_every = 5               # print heartbeat once every N ticks (~10 min at 120s)
        # Last-checked timestamps per strategy — updated just before each strategy is called
        _last_checked: dict = {
            "GAP":   None,
            "ORB":   None,
            "VWAP":  None,
            "TREND": None,
            "EOD":   None,
        }

        while True:
            try:
                # Reset daily flags at the start of each new trading day
                today = datetime.now().date()
                if _last_loop_date != today:
                    self._index_gap_traded = {}    # reset all per-index gap flags each new day
                    self._index_gap_direction = {}  # reset gap direction each new day
                    self._index_gap_fail_count = {}  # reset gap failure counters each new day
                    self._index_trend_triggered = {}  # reset paper TREND guard each new day
                    self._index_day_direction = {}   # reset same-day direction lock each new day
                    self._gift_nifty_predicted  = False  # reset Gift Nifty prediction each day
                    self._filter_blocks = {}            # reset filter block counters each new day
                    self._error_counts = {}             # reset error counters each new day
                    # Clear persisted daily state for the new day
                    self._save_daily_state()
                    if self.order_manager:
                        self.order_manager._reset_daily_stats_if_needed()
                    _last_loop_date = today

                now_ts = datetime.now()
                _tick += 1

                # ── Per-tick loop diagnostic ──────────────────────────────────────────
                _is_market_hrs = (
                    now_ts.weekday() <= 4
                    and (
                        (now_ts.hour == 9 and now_ts.minute >= 15)
                        or (10 <= now_ts.hour <= 14)
                        or (now_ts.hour == 15 and now_ts.minute <= 30)
                    )
                )
                _live_pos_count = len([
                    t for t in (self.order_manager._positions.values()
                                if self.order_manager else [])
                    if t.status == "OPEN"
                ])
                _paper_pos_count = len(self.paper_trader._positions) if hasattr(self.paper_trader, '_positions') else 0
                logger.debug(
                    f"{'=' * 60}\n"
                    f"  LOOP TICK {_tick} — {now_ts.strftime('%H:%M:%S')}\n"
                    f"  Market hours : {'YES' if _is_market_hrs else 'NO (pre/post-market)'}\n"
                    f"  ORB enabled  : {settings.orb.enabled}  |  "
                    f"VWAP enabled: {settings.vwap.enabled}  |  "
                    f"GAP enabled: {settings.gap.enabled}\n"
                    f"  Auto-trade   : {self._auto_trade}  |  "
                    f"VIX filter: {settings.trading.vix_filter_enabled} "
                    f"(max={settings.trading.vix_max}, current={self._cached_vix:.1f})\n"
                    f"  Positions    : live={_live_pos_count}  paper={_paper_pos_count}  "
                    f"(cap={settings.trading.max_positions})\n"
                    f"  ORB states   : "
                    + "  ".join(
                        f"{n}={self._index_orbs[n]._state.value}"
                        for n in ('NIFTY', 'BANKNIFTY', 'SENSEX')
                    ) + "\n"
                    f"  Filter blocks: {self._filter_blocks or 'none'}\n"
                    f"  Errors today : {self._error_counts or 'none'}"
                )

                # ── Heartbeat: proves the loop is alive and not silently hung ────────
                if _tick - _last_heartbeat_tick >= _heartbeat_every:
                    _last_heartbeat_tick = _tick
                    def _fmt(ts): return ts.strftime("%H:%M") if ts else "never"
                    _open_pos = len([
                        t for t in (self.order_manager._positions.values()
                                    if self.order_manager else [])
                        if t.status == "OPEN"
                    ])
                    _paper_pos = len(self.paper_trader._positions) if hasattr(self.paper_trader, '_positions') else 0
                    _err_str = (
                        " | errors: " + ", ".join(f"{k}={v}" for k, v in self._error_counts.items())
                        if self._error_counts else ""
                    )
                    logger.info(
                        f"[HEARTBEAT] tick={_tick} | {now_ts.strftime('%H:%M:%S')} "
                        f"| interval={interval}s "
                        f"| live_pos={_open_pos} paper_pos={_paper_pos} "
                        f"| last GAP={_fmt(_last_checked['GAP'])} "
                        f"ORB={_fmt(_last_checked['ORB'])} "
                        f"VWAP={_fmt(_last_checked['VWAP'])} "
                        f"TREND={_fmt(_last_checked['TREND'])} "
                        f"EOD={_fmt(_last_checked['EOD'])}"
                        + _err_str
                    )
                # Throttle: at most every 10 minutes — VIX doesn't change that fast.
                _vix_stale = (
                    self._cached_vix_time is None
                    or (now_ts - self._cached_vix_time).total_seconds() > 600
                )
                if _vix_stale and now_ts.weekday() <= 4:
                    try:
                        import yfinance as _yf
                        _vi = _yf.Ticker("^INDIAVIX").fast_info
                        _v  = float(_vi.get("lastPrice") or 0)
                        if _v <= 0:
                            _vd = _yf.Ticker("^INDIAVIX").history(period="1d", interval="1d")
                            if not _vd.empty:
                                _v = float(_vd["Close"].iloc[-1])
                        if _v > 0:
                            self._cached_vix = _v
                            self._cached_vix_time = now_ts
                            logger.info(f"India VIX = {_v:.2f} (cached for signal annotation)")
                    except Exception as _ve:
                        logger.warning(f"VIX cache fetch failed — filter may be bypassed: {_ve}")

                # ── Pre-market: Gift Nifty gap prediction (8:45–9:10 AM) ──────────
                if (
                    now_ts.weekday() <= 4
                    and now_ts.hour == 8
                    and now_ts.minute >= 45
                ):
                    await self._check_gift_nifty_premarket()

                # ── AI Macro Sentiment (PROMPT 2): refresh once per session boundary ─
                # Sessions: Morning(9:15), Midday(11:30), Afternoon(13:00), Close(15:00)
                # UNFAVORABLE result is logged so the per-signal PROMPT 1 can reference it.
                # It does NOT block trades on its own — acts as an advisory layer only
                # (PROMPT 1 already applies time/VIX/loss-fatigue rules per-signal).
                if _is_market_hrs and now_ts.weekday() <= 4:
                    try:
                        from bot.local_ai_service import get_ai_service, LocalAIService
                        _ai_svc = get_ai_service()
                        if _ai_svc.enabled and settings.ai.use_ai_sentiment:
                            _cur_session = LocalAIService._get_session(now_ts)
                            if _cur_session != self._ai_sentiment_session:
                                # Session changed — refresh sentiment in background
                                # (fire-and-forget via create_task; doesn't block the loop)
                                async def _refresh_sentiment(_session=_cur_session):
                                    _ctx = {
                                        "vix": self._cached_vix,
                                        "macro_events": "None known",
                                    }
                                    _result = await _ai_svc.analyze_market_sentiment(_ctx)
                                    self._ai_sentiment = _result
                                    self._ai_sentiment_session = _session
                                    _emoji = "✅" if _result.action == "FAVORABLE" else ("⚠️" if _result.action == "UNFAVORABLE" else "ℹ️")
                                    _msg = (
                                        f"{_emoji} AI Market Sentiment [{_session}]: "
                                        f"{_result.market_sentiment} "
                                        f"({_result.sentiment_strength:.0f}%) — "
                                        f"{_result.trading_conditions} conditions — "
                                        f"{_result.reasoning}"
                                    )
                                    logger.info(_msg)
                                    await self._broadcast_message(_msg, "analysis")
                                    if _result.action == "UNFAVORABLE" and not _result.fallback:
                                        await self._send_telegram_alert(
                                            f"⚠️ *AI Sentiment: UNFAVORABLE*\n"
                                            f"Session: {_session}\n"
                                            f"Conditions: {_result.trading_conditions}\n"
                                            f"VIX regime: {_result.volatility_regime}\n"
                                            f"{_result.reasoning}",
                                            "system",
                                        )
                                asyncio.create_task(_refresh_sentiment())
                    except Exception as _sent_exc:
                        logger.debug(f"AI sentiment refresh skipped: {_sent_exc}")

                # Gap-open window flag (shared across all index iterations below)
                # Starts at 9:20 (not 9:15) — the first 5 minutes after open have maximum
                # uncertainty: market-makers adjusting, algos rebalancing, shakeout moves.
                # Data (3 weeks): entries at 9:15 had worse fills and more wrong-direction
                # outcomes vs entries at 9:20+ when initial direction is clearer.
                # Extended to 11:00 AM so a bot restart after 9:30 still catches the gap.
                is_near_open = (
                    now_ts.weekday() <= 4  # Mon–Fri
                    and (
                        (now_ts.hour == 9 and now_ts.minute >= 20)  # 9:20–9:59 (skip first 5 min)
                        or now_ts.hour == 10                         # 10:00–10:59
                    )
                )

                # ── Scan all three indices ─────────────────────────────────────
                for idx_cfg in [NIFTY, BANKNIFTY, SENSEX]:
                    # Outside Indian market hours (pre/post market) there is no live
                    # price feed and yfinance data is stale.  Skip strategy analysis
                    # to avoid phantom signals and distorted ATR calculations.
                    # Paper exit checks still run below (outside this loop).
                    if not _is_market_hrs:
                        break

                    idx_name = idx_cfg.name

                    # Temporarily swap context to this index so every strategy
                    # method (which reads self._active_index etc.) uses the right one
                    self._active_index    = idx_cfg
                    self.analyzer         = self._index_analyzers[idx_name]
                    self.orb              = self._index_orbs[idx_name]
                    self.vwap_strat       = self._index_vwaps[idx_name]
                    if self.order_manager:
                        self.order_manager.set_active_index(idx_cfg)
                    self.kite.set_active_index(idx_cfg)
                    regime_detector.set_index(idx_cfg)
                    mtf_engine.set_index(idx_cfg)
                    self._gap_detector.set_index(idx_cfg)

                    # Restore per-index last signal so ORB direction filter is correct
                    self._last_signal = self._index_last_signal.get(idx_name)

                    # ── Step 1: Regime (per-index cache, re-fetch every 30 min) ──
                    last_regime_time = self._index_regime_time.get(idx_name)
                    cache_stale = (
                        idx_name not in self._index_regime_cache
                        or last_regime_time is None
                        or (now_ts - last_regime_time).total_seconds() >= self._regime_cache_seconds
                    )
                    if cache_stale:
                        fresh = regime_detector.analyze()
                        self._index_regime_time[idx_name] = now_ts
                        if fresh is not None:
                            self._index_regime_cache[idx_name] = fresh
                    regime_result = self._index_regime_cache.get(idx_name)
                    regime_value  = regime_result.regime.value if regime_result else None
                    # Keep convenience pointer for _check_vwap_signal
                    self._cached_regime = regime_result

                    if cache_stale and regime_result:
                        logger.info(
                            f"[{idx_name}] Regime: {regime_value} | ADX={regime_result.adx:.1f} "
                            f"| Hurst={regime_result.hurst:.3f} | Trade={regime_result.should_trade}"
                        )
                    elif cache_stale and not regime_result:
                        logger.warning(
                            f"[{idx_name}] Regime unavailable (data fetch failed) — "
                            f"strategies will run without regime filter"
                        )

                    # ── Step 1b: Gap detection (once per day, near market open) ──
                    # Paper mode: no order_manager needed (paper_buy doesn’t place real orders)
                    # Live mode: order_manager required to place the actual order
                    _gap_ok = is_near_open and settings.gap.enabled
                    if _gap_ok and (not self._auto_trade or self.order_manager):
                        _last_checked["GAP"] = datetime.now()
                        try:
                            await self._check_gap_signal()
                        except Exception:
                            self._error_counts["gap"] = self._error_counts.get("gap", 0) + 1
                            logger.exception(
                                f"[{idx_name}] _check_gap_signal raised an exception "
                                f"(count: {self._error_counts['gap']})"
                            )
                    elif _gap_ok and self._auto_trade and not self.order_manager:
                        logger.warning(
                            f"[{idx_name}] Gap check SKIPPED — order_manager not ready yet. "
                            f"Bot must be started (click Start) before market opens."
                        )

                    # ── Step 1c: Straddle strategy (paper only, 9:30–11:00) ──
                    if idx_name == "NIFTY" and not self._auto_trade:
                        _strad_price = self._paper_index_prices.get("NIFTY", 0.0)
                        if _strad_price > 0:
                            try:
                                await self._check_straddle_signal(idx_name, _strad_price, regime_value)
                            except Exception:
                                self._error_counts["straddle"] = self._error_counts.get("straddle", 0) + 1
                                logger.exception(
                                    f"[{idx_name}] _check_straddle_signal raised an exception "
                                    f"(count: {self._error_counts['straddle']})"
                                )

                    # ── Step 2: Technical trend analysis ─────────────────────────
                    _last_checked["TREND"] = datetime.now()
                    signal = None
                    try:
                        signal = self.analyzer.analyze(regime=regime_value)
                    except Exception:
                        self._error_counts["data_fetch"] = self._error_counts.get("data_fetch", 0) + 1
                        logger.exception(
                            f"[{idx_name}] TrendAnalyzer.analyze() raised an exception "
                            f"(count: {self._error_counts['data_fetch']})"
                        )

                    if signal:
                        self._last_signal = signal
                        self._index_last_signal[idx_name] = signal
                        self._last_analysis_time = datetime.now()
                        self._index_last_analysis_time[idx_name] = self._last_analysis_time
                        # Keep latest index price for paper exit tracking
                        self._paper_index_prices[idx_name] = signal.current_price

                        trade_journal.log_analysis(
                            trend=signal.trend.value,
                            strength=signal.strength,
                            price=signal.current_price,
                            rsi=signal.rsi,
                        )

                        report = self.analyzer.format_analysis_report(signal)
                        if regime_result:
                            report += f"\n  Regime: {regime_value} | Trade Signal: {'YES' if regime_result.should_trade else 'NO'}"
                        await self._broadcast_message(report, "analysis")

                        # Custom rules fire ONLY for the user's selected index so
                        # a rule "if RSI < 30 buy CE" targets NIFTY (or whichever
                        # the user picked) — not whichever index happens to satisfy
                        # the condition first across the three-index scan.
                        if idx_name == self._user_selected_index.name:
                            await self._evaluate_custom_instructions(signal)

                    # ── Step 3: Strategy selection based on regime ────────────────
                    if self._auto_trade:
                        is_ranging  = regime_value == "RANGING" if regime_value else False
                        is_trending = regime_value in ("TRENDING UP", "TRENDING DOWN") if regime_value else False
                        should_trade = regime_result.should_trade if regime_result else True

                        if is_ranging:
                            try:
                                await self._check_vwap_signal()
                            except Exception:
                                self._error_counts["vwap"] = self._error_counts.get("vwap", 0) + 1
                                logger.exception(f"[{idx_name}] _check_vwap_signal (ranging) raised an exception (count: {self._error_counts['vwap']})")
                            try:
                                await self._check_orb_signal()
                            except Exception:
                                self._error_counts["orb"] = self._error_counts.get("orb", 0) + 1
                                logger.exception(f"[{idx_name}] _check_orb_signal (ranging) raised an exception (count: {self._error_counts['orb']})")
                            if signal and signal.trend != Trend.NEUTRAL:
                                # Clear directional 5m signal during a "ranging" regime most likely
                                # means the 3-month ADX hasn't caught up to a single-day crash/rally.
                                # Allow auto-trade if signal is strong and slots are available.
                                _orb_done  = self._index_orb_triggered.get(idx_name) == datetime.now().date()
                                _vwap_done = self._index_vwap_triggered.get(idx_name) == datetime.now().date()
                                if not (_orb_done and _vwap_done) and now_ts.hour < 14:
                                    logger.info(
                                        f"[{idx_name}] Ranging-regime override: {signal.trend.value} "
                                        f"{signal.strength}% signal — executing auto-trade (ADX lag)"
                                    )
                                    try:
                                        await self._execute_auto_trade(signal)
                                    except Exception:
                                        self._error_counts["execute"] = self._error_counts.get("execute", 0) + 1
                                        logger.exception(f"[{idx_name}] _execute_auto_trade (ranging override) raised an exception (count: {self._error_counts['execute']})")
                            elif regime_result and not regime_result.should_trade:
                                logger.info(f"[{idx_name}] Ranging market: skipping multi-indicator trend trade.")
                        elif should_trade and signal and signal.trend != Trend.NEUTRAL:
                            # Per-index slot guard: if this index already fired both ORB and VWAP
                            # today, skip _execute_auto_trade to prevent a 3rd unguarded entry.
                            _orb_done  = self._index_orb_triggered.get(idx_name) == datetime.now().date()
                            _vwap_done = self._index_vwap_triggered.get(idx_name) == datetime.now().date()
                            if _orb_done and _vwap_done:
                                logger.debug(
                                    f"[{idx_name}] Auto-trade skipped — both ORB + VWAP slots already used today"
                                )
                            elif now_ts.hour >= 14:
                                # No new trend-following auto-entries after 14:00 IST.
                                # Late entries have thin liquidity, wide spreads, and
                                # insufficient time before the 15:27 force-exit.
                                logger.debug(
                                    f"[{idx_name}] Auto-trade skipped — past 14:00 IST cutoff"
                                )
                            else:
                                try:
                                    await self._execute_auto_trade(signal)
                                except Exception:
                                    self._error_counts["execute"] = self._error_counts.get("execute", 0) + 1
                                    logger.exception(f"[{idx_name}] _execute_auto_trade raised an exception (count: {self._error_counts['execute']})")
                            try:
                                await self._check_orb_signal()
                            except Exception:
                                self._error_counts["orb"] = self._error_counts.get("orb", 0) + 1
                                logger.exception(f"[{idx_name}] _check_orb_signal (trending) raised an exception (count: {self._error_counts['orb']})")
                        else:
                            if is_trending and regime_result and regime_result.adx > 50:
                                logger.info(
                                    f"[{idx_name}] Strong trend (ADX={regime_result.adx:.0f}) with neutral 5m signal "
                                    f"— running VWAP as secondary entry"
                                )
                                try:
                                    await self._check_vwap_signal()
                                except Exception:
                                    self._error_counts["vwap"] = self._error_counts.get("vwap", 0) + 1
                                    logger.exception(f"[{idx_name}] _check_vwap_signal (secondary) raised an exception (count: {self._error_counts['vwap']})")
                            try:
                                await self._check_orb_signal()
                            except Exception:
                                self._error_counts["orb"] = self._error_counts.get("orb", 0) + 1
                                logger.exception(f"[{idx_name}] _check_orb_signal (else) raised an exception (count: {self._error_counts['orb']})")
                    else:
                        # Paper mode: run ORB + VWAP analysis so signals are visible
                        # in logs/UI. Order placement is blocked inside each method.
                        _last_checked["ORB"] = datetime.now()
                        try:
                            await self._check_orb_signal()
                        except Exception:
                            self._error_counts["orb"] = self._error_counts.get("orb", 0) + 1
                            logger.exception(
                                f"[{idx_name}] _check_orb_signal raised an exception "
                                f"(count: {self._error_counts['orb']})"
                            )
                        _last_checked["VWAP"] = datetime.now()
                        try:
                            await self._check_vwap_signal()
                        except Exception:
                            self._error_counts["vwap"] = self._error_counts.get("vwap", 0) + 1
                            logger.exception(
                                f"[{idx_name}] _check_vwap_signal raised an exception "
                                f"(count: {self._error_counts['vwap']})"
                            )
                        # Also log paper trade for main trend signal if it's not NEUTRAL.
                        # One-per-day guard: once TREND fires for this index, don't refire
                        # every 60 seconds — that would inflate win-rate stats with duplicates.
                        _trend_done = self._index_trend_triggered.get(idx_name) == datetime.now().date()
                        if signal and signal.trend == Trend.NEUTRAL:
                            logger.info(
                                f"PAPER TREND [{idx_name}]: signal is NEUTRAL "
                                f"(strength={signal.strength}%) — no paper entry this cycle"
                            )
                        elif signal and signal.trend != Trend.NEUTRAL and not _trend_done:
                            _pt_opt = "CE" if signal.trend == Trend.BULLISH else "PE"

                            # ── Paper improvement 1: minimum strength ≥ 70 ───────────
                            # Paper data: TIME_STOP trades (avg +2.5%) all had strength
                            # 60–68%. Winners (avg +28%) all had strength ≥ 70%.
                            # Raising threshold filters dead-weight entries.
                            _paper_trend_skip = False
                            _min_strength_pt = float(getattr(settings.trading, 'min_signal_strength', 70.0))
                            if signal.strength < _min_strength_pt:
                                logger.info(
                                    f"PAPER TREND [{idx_name}]: skipping — "
                                    f"strength {signal.strength:.0f}% < {_min_strength_pt:.0f}% minimum "
                                    f"(paper filter from data analysis)"
                                )
                                self._filter_blocks["TREND_strength"] = self._filter_blocks.get("TREND_strength", 0) + 1
                                _paper_trend_skip = True

                            # ── Paper improvement 2: earliest entry 10:00 AM ────────
                            # Paper data: 9:15–9:50 AM entries were noisy (1 SL_HIT
                            # at -100%, multiple TIME_STOPs). Best winners entered
                            # 12:40–12:49 PM after trend was confirmed. Gate at 10:00.
                            if not _paper_trend_skip:
                                from zoneinfo import ZoneInfo as _ZI
                                if datetime.now(_ZI("Asia/Kolkata")).hour < 10:
                                    logger.info(
                                        f"PAPER TREND [{idx_name}]: skipping — "
                                        f"before 10:00 IST. Open noise filter active."
                                    )
                                    self._filter_blocks["TREND_too_early"] = self._filter_blocks.get("TREND_too_early", 0) + 1
                                    _paper_trend_skip = True

                            if not _paper_trend_skip:
                                # ── Regime-direction guard (mirrors VWAP guard) ──────────
                                # Block CE entries on TRENDING DOWN days and PE on TRENDING UP.
                                _trend_regime_blocked = False
                                _cached_regime_tr = getattr(self, "_cached_regime", None)
                                if _cached_regime_tr:
                                    _regime_val_tr = _cached_regime_tr.regime.value
                                    if _regime_val_tr == "TRENDING DOWN" and _pt_opt == "CE":
                                        logger.info(
                                            f"PAPER TREND [{idx_name}]: Skipping CE — regime is TRENDING DOWN (counter-trend)"
                                        )
                                        self._filter_blocks["TREND_regime"] = self._filter_blocks.get("TREND_regime", 0) + 1
                                        _trend_regime_blocked = True
                                    elif _regime_val_tr == "TRENDING UP" and _pt_opt == "PE":
                                        logger.info(
                                            f"PAPER TREND [{idx_name}]: Skipping PE — regime is TRENDING UP (counter-trend)"
                                        )
                                        self._filter_blocks["TREND_regime"] = self._filter_blocks.get("TREND_regime", 0) + 1
                                        _trend_regime_blocked = True
                                if not _trend_regime_blocked:
                                    # ── AI validation in paper mode ──────────────────────
                                    _paper_trend_ai_ok = True
                                    if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                                        try:
                                            from bot.ai_gateway import get_ai_gateway
                                            _gw_pt = get_ai_gateway()
                                            _gw_pt_ctx = {
                                                'option_type':        _pt_opt,
                                                'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                                                'vix':                self._cached_vix,
                                                'consecutive_losses': 0,
                                                'trades_done_today':  0,
                                                'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                                            }
                                            _gw_pt_res = await _gw_pt.validate_strategy_signal(signal, 'TREND', _gw_pt_ctx)
                                            if not _gw_pt_res['approved']:
                                                logger.info(f"PAPER TREND [{idx_name}]: AI blocked — {_gw_pt_res['reasoning']}")
                                                await self._broadcast_message(
                                                    f"🤖 PAPER TREND [{idx_name}]: AI blocked {_pt_opt} — {_gw_pt_res['reasoning']}",
                                                    "paper_trade",
                                                )
                                                _paper_trend_ai_ok = False
                                            else:
                                                logger.info(f"PAPER TREND [{idx_name}]: AI approved conf={_gw_pt_res['confidence']:.0f}%")
                                        except Exception as _ai_pt_err:
                                            logger.warning(f"PAPER TREND [{idx_name}]: AI check failed ({_ai_pt_err}), proceeding")
                                    if _paper_trend_ai_ok:
                                        logger.info(
                                            f"PAPER TREND [{idx_name}]: Would buy {_pt_opt} — "
                                            f"{signal.trend.value} {signal.strength}% — paper mode, no order placed"
                                        )
                                        await self._broadcast_message(
                                            f"📋 PAPER TREND [{idx_name}]: Would buy {_pt_opt} ATM "
                                            f"| {signal.trend.value} strength {signal.strength}% "
                                            f"| price {signal.current_price:,.0f} — paper mode",
                                            "paper_trade",
                                        )
                                        self.paper_trader.paper_buy(
                                            index_name=idx_name,
                                            index_price=signal.current_price,
                                            option_type=_pt_opt,
                                            strategy="TREND",
                                            quantity=idx_cfg.lot_size,
                                        )
                                        self._index_trend_triggered[idx_name] = datetime.now().date()
                                        # Record today's direction so ORB won't trade against it
                                        self._index_day_direction[idx_name] = _pt_opt
                                        self._save_daily_state()

                    # ── Step 4: EOD closing momentum (14:30–15:00 IST) ──────────────
                    _last_checked["EOD"] = datetime.now()
                    try:
                        await self._check_eod_signal()
                    except Exception:
                        self._error_counts["eod"] = self._error_counts.get("eod", 0) + 1
                        logger.exception(
                            f"[{idx_name}] _check_eod_signal raised an exception "
                            f"(count: {self._error_counts['eod']})"
                        )

                    # ── Step 5: Late-Day Oscillation (14:30–15:25 IST) ──────────────
                    # Only check for NIFTY (the pattern is index-specific and
                    # running it on all three would multiply position risk 3×)
                    if idx_name == "NIFTY" and settings.late_day.enabled:
                        _last_checked["LATE_DAY"] = datetime.now()
                        try:
                            await self._check_late_day_signal()
                        except Exception:
                            self._error_counts["late_day"] = (
                                self._error_counts.get("late_day", 0) + 1
                            )
                            logger.exception(
                                f"[{idx_name}] _check_late_day_signal raised an exception "
                                f"(count: {self._error_counts['late_day']})"
                            )

                # ── Paper exit check: runs AFTER all 3 indices processed ──────────
                if not self._auto_trade and self.paper_trader.has_open_positions():
                    self.paper_trader.check_exits(self._paper_index_prices)

                # ── Filter block summary — log once per cycle if any blocks fired ──
                if self._filter_blocks:
                    _vix_ann = f" | India VIX={self._cached_vix:.1f}" if self._cached_vix > 0 else ""
                    _iv_hist = iv_monitor._iv_history.get("NIFTY", {})
                    _iv_days = len(_iv_hist)
                    _iv_ann  = f" | IV history={_iv_days}d" if _iv_days > 0 else ""
                    _parts   = ", ".join(
                        f"{k}: {v}" for k, v in sorted(self._filter_blocks.items())
                    )
                    logger.info(
                        f"[FILTER BLOCKS TODAY]{_vix_ann}{_iv_ann} | {_parts}"
                    )
                # ── Straddle exit check ──────────────────────────────────────────
                if not self._auto_trade and self.straddle_strategy._positions:
                    self.straddle_strategy.check_exits(self._paper_index_prices)

                # ── Restore context to user's selected index after all scans ─────
                _usr = self._user_selected_index
                self._active_index  = _usr
                self.analyzer       = self._index_analyzers[_usr.name]
                self.orb            = self._index_orbs[_usr.name]
                self.vwap_strat     = self._index_vwaps[_usr.name]
                self.order_manager.set_active_index(_usr) if self.order_manager else None
                self.kite.set_active_index(_usr)
                regime_detector.set_index(_usr)
                mtf_engine.set_index(_usr)
                self._gap_detector.set_index(_usr)
                backtester.set_index(_usr)
                theta_clock.set_index(_usr)
                self._cached_regime         = self._index_regime_cache.get(_usr.name)
                self._last_signal           = self._index_last_signal.get(_usr.name)
                self._last_analysis_time    = self._index_last_analysis_time.get(_usr.name)

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info("Analysis loop cancelled")
                break
            except Exception as e:
                self._error_counts["analysis_loop"] = self._error_counts.get("analysis_loop", 0) + 1
                logger.exception(
                    f"Analysis loop unhandled exception "
                    f"(total crashes today: {self._error_counts['analysis_loop']})"
                )
                await self._broadcast_message(f"⚠️ Analysis error: {e}", "error")
                await asyncio.sleep(10)
    
    async def _position_monitor_loop(self):
        """Monitor open positions for stop-loss and target hits"""
        logger.info("Starting position monitoring loop")
        
        # Check positions every 15 seconds for SL/target
        # Broadcast P&L every 10 checks (150 seconds) 
        check_interval = 15
        pnl_broadcast_count = 0
        _expiry_alert_sent_date: Optional[date] = None  # track so alert fires once per day
        _profit_recorded_date: Optional[date] = None   # track EOD profit recording
        # Snapshot of the user's index — stable reference immune to analysis-loop swaps
        _usr_idx = self._user_selected_index
        # Periodic broker sync: pick up manually-placed trades every 5 min
        _last_broker_sync: Optional[datetime] = None
        _broker_sync_interval_sec = 300  # 5 minutes

        while True:
            try:
                # Keep tracking the user's current selection (may change via 'index' command)
                _usr_idx = self._user_selected_index
                now = datetime.now()

                # ── Periodic broker sync: detect manually-placed trades ───────────────
                # Runs every 5 minutes during market hours. Any open position on Dhan
                # that is NOT already tracked by the bot gets added as a RECOVERED trade
                # so SL/target/trail monitoring applies automatically.
                _market_open_dt  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
                _market_close_dt = now.replace(hour=15, minute=30, second=0, microsecond=0)
                _in_market_hours = _market_open_dt <= now <= _market_close_dt and now.weekday() <= 4
                if (
                    _in_market_hours
                    and self.order_manager
                    and (
                        _last_broker_sync is None
                        or (now - _last_broker_sync).total_seconds() >= _broker_sync_interval_sec
                    )
                ):
                    _last_broker_sync = now
                    try:
                        pos_data = await self.kite.get_kite_positions()
                        _synced = 0
                        for _bp in pos_data.get("day", []):
                            _bqty = _bp.get("quantity", 0)
                            if _bqty <= 0:
                                continue
                            _bsym = _bp.get("tradingsymbol", "")
                            if not _bsym:
                                continue
                            # Skip if already tracked (any trade_id mentioning this symbol)
                            _sym_norm = _bsym.upper().replace("-", "").replace(" ", "")
                            _already = any(
                                t.symbol.upper().replace("-", "").replace(" ", "") == _sym_norm
                                and t.status == "OPEN"
                                for t in self.order_manager._positions.values()
                            )
                            if _already:
                                continue
                            # New manual position — add it
                            _bavg = float(_bp.get("average_price") or _bp.get("buy_price") or 0)
                            if _bavg <= 0:
                                continue
                            from browser.dhan import OptionType, OrderType
                            from bot.order_manager import TradeRecord
                            import re as _re
                            _opt = OptionType.CE if _bsym.upper().endswith("CE") else OptionType.PE
                            _sm  = _re.search(r'-(\d+)-(CE|PE)$', _bsym, _re.IGNORECASE)
                            _strike = int(_sm.group(1)) if _sm else 0
                            _tid = f"RECOVERED_{_bsym}"
                            _tr = TradeRecord(
                                trade_id=_tid,
                                symbol=_bsym,
                                option_type=_opt,
                                strike=_strike,
                                order_type=OrderType.BUY,
                                quantity=abs(_bqty),
                                price=_bavg,
                                timestamp=datetime.now(),
                                status="OPEN",
                                highest_price=_bavg,
                            )
                            self.order_manager._positions[_tid] = _tr
                            _sec_id = _bp.get("security_id", "")
                            if _sec_id and hasattr(self.kite, '_position_security_ids'):
                                self.kite._position_security_ids[_bsym.upper()] = str(_sec_id)
                            _synced += 1
                            logger.info(
                                f"📥 Broker sync: added manual position {_bsym} "
                                f"x{abs(_bqty)} @ ₹{_bavg:.2f} — SL/target monitoring active"
                            )
                            await self._broadcast_message(
                                f"📥 Manual trade detected: {_bsym} x{abs(_bqty)} @ ₹{_bavg:.2f} "
                                f"— SL/target monitoring now active",
                                "system"
                            )
                        if _synced:
                            await self._send_telegram_alert(
                                f"📥 {_synced} manual position(s) picked up — bot is now managing SL/target.",
                                "system"
                            )
                    except Exception as _sync_err:
                        logger.debug(f"Broker sync error (non-critical): {_sync_err}")


                if now.weekday() <= 4:  # Mon–Fri only
                    today_date = now.date()
                    active_expiry_weekday = _usr_idx.expiry_weekday  # use stable user-index ref
                    is_expiry_day = (today_date.weekday() == active_expiry_weekday)
                    market_close = now.replace(
                        hour=settings.trading.market_close_hour,
                        minute=settings.trading.market_close_minute,
                        second=0, microsecond=0
                    )
                    alert_window_start = market_close - timedelta(minutes=45)
                    alert_window_end   = market_close - timedelta(minutes=10)
                    if (
                        is_expiry_day
                        and alert_window_start <= now <= alert_window_end
                        and _expiry_alert_sent_date != today_date
                    ):
                        _expiry_alert_sent_date = today_date
                        open_count = len([
                            t for t in (self.order_manager._positions.values()
                                        if self.order_manager else [])
                            if t.status == "OPEN"
                        ])
                        msg = (
                            f"⚠️ EXPIRY DAY ALERT ({_usr_idx.display_name}) — "
                            f"45 minutes to market close. "
                            f"{open_count} bot position(s) open. "
                            f"Manually verify ALL positions — option contracts expire today!"
                        )
                        await self._broadcast_message(msg, "warning")
                        await self._send_telegram_alert(msg, "system")
                        trade_journal.log_alert(msg)
                        logger.warning(msg)

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
                        _day_pnl = self.order_manager._daily_pnl
                        _day_sign = "📈" if _day_pnl >= 0 else "📉"
                        await self._send_telegram_alert(
                            f"🛑 Daily loss limit hit — {ok}/{len(results)} positions closed.\n"
                            f"{_day_sign} Day P&L: ₹{_day_pnl:,.2f}",
                            "trade_exit"
                        )

                # ── Hard time exit: close pre-13:00 positions at 13:00 IST ──────────
                # Theta accelerates sharply in the illiquid 13:00–14:30 window.
                # EOD positions (entered 14:30+) are excluded by the timestamp check.
                # When time_exit_only_losers=True, profitable positions are left to run.
                _hard_exit_h   = getattr(settings.trading, 'hard_time_exit_hour', 0)
                _only_losers   = getattr(settings.trading, 'time_exit_only_losers', False)
                _min_profit_th = getattr(settings.trading, 'time_exit_min_profit', 50.0)
                if _hard_exit_h > 0 and self.order_manager and now.weekday() <= 4:
                    _today_dt = now.date()
                    _theta_victims = [
                        t for t in self.order_manager._positions.values()
                        if (
                            t.status == "OPEN"
                            and t.timestamp.date() == _today_dt
                            and t.timestamp.hour < _hard_exit_h
                            and now.hour >= _hard_exit_h
                            # Minimum 15-minute hold: avoids closing a 12:59 entry 1 minute later
                            and (now - t.timestamp).total_seconds() >= 15 * 60
                            # Smart exit: skip profitable positions when only_losers is on.
                            # Use mark-to-market P&L (current_price - entry) * qty because
                            # t.pnl is only set at close time — for open positions it is None/0,
                            # which caused ALL positions to be closed regardless of profit.
                            and not (
                                _only_losers
                                and (
                                    (self._current_prices.get(t.symbol, 0.0) - t.price) * t.quantity
                                    if self._current_prices.get(t.symbol, 0.0) > 0
                                    else (t.pnl or 0.0)
                                ) >= _min_profit_th
                            )
                        )
                    ]
                    if _theta_victims:
                        logger.info(
                            f"Hard time exit at {_hard_exit_h}:00 IST — "
                            f"closing {len(_theta_victims)} position(s) to avoid theta decay"
                        )
                        await self._broadcast_message(
                            f"⏱️ Hard time exit ({_hard_exit_h}:00 IST) — "
                            f"closing {len(_theta_victims)} position(s) to avoid theta decay",
                            "system"
                        )
                        for _vt in _theta_victims:
                            _vt_px = self._current_prices.get(_vt.symbol, 0.0)
                            _vt_res = await self.order_manager.close_position(
                                trade_id=_vt.trade_id, exit_price=_vt_px
                            )
                            if _vt_res.success:
                                _pnl_s = f"₹{_vt.pnl:,.2f}" if _vt.pnl is not None else "N/A"
                                _day_pnl = self.order_manager._daily_pnl
                                _day_sign = "📈" if _day_pnl >= 0 else "📉"
                                await self._send_telegram_alert(
                                    f"⏱️ Hard time exit at {_hard_exit_h}:00\n"
                                    f"{_vt.symbol} | Trade P&L: {_pnl_s}\n"
                                    f"{_day_sign} Day P&L: ₹{_day_pnl:,.2f}",
                                    "trade_exit"
                                )
                            else:
                                logger.warning(f"Hard time exit failed for {_vt.trade_id}: {_vt_res.message}")

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
                        # Capture EOD positions before close (pnl is set during close_position)
                        _eod_open = [
                            t for t in self.order_manager._positions.values()
                            if t.status == "OPEN" and getattr(t, "source", "") == "EOD"
                        ]
                        results = await self.order_manager.close_all_positions()
                        ok = sum(1 for r in results if r.success)
                        # Record EOD outcomes (pnl is set inside close_position)
                        for _et in _eod_open:
                            if _et.pnl is not None:
                                _eod_tracker.record(_et.pnl > 0)
                        await self._broadcast_message(
                            f"Auto-close: {ok}/{len(results)} positions closed.", "system"
                        )
                        _day_pnl = self.order_manager._daily_pnl
                        _day_sign = "📈" if _day_pnl >= 0 else "📉"
                        await self._send_telegram_alert(
                            f"⏰ Market-close auto-exit: {ok}/{len(results)} positions closed.\n"
                            f"{_day_sign} Day P&L: ₹{_day_pnl:,.2f}",
                            "trade_exit"
                        )

                # ── EOD profit recording: snapshot P&L at 15:30 IST ─────────────
                if now.weekday() <= 4:  # Mon–Fri only
                    if now.hour == 15 and now.minute >= 30 and _profit_recorded_date != now.date():
                        _profit_recorded_date = now.date()
                        try:
                            # ── Source 1: Dhan settlement (most accurate) ─────
                            # Uses the actual settled amount from Dhan ledger —
                            # works even if bot was restarted mid-day.
                            gross = None
                            _settle_note = ""
                            try:
                                from config import settings as _cfg2
                                if str(_cfg2.broker).lower() == "dhan":
                                    from dhanhq import dhanhq as _dhan2
                                    _dc2 = _dhan2(_cfg2.dhan.client_id, _cfg2.dhan.access_token)
                                    today_iso = now.date().isoformat()
                                    lr = await asyncio.to_thread(
                                        _dc2.ledger_report, today_iso, today_iso
                                    )
                                    for row in (lr.get("data") or []):
                                        if row.get("narration") == "Trades Executed":
                                            credit = float(row.get("credit") or 0)
                                            debit  = float(row.get("debit")  or 0)
                                            gross  = round(credit - debit, 2)
                                            _settle_note = "Dhan settlement"
                                            break
                            except Exception as _sl_err:
                                logger.debug(f"EOD settlement fetch failed: {_sl_err}")

                            # ── Source 2: in-memory order_manager (fallback) ──
                            if gross is None:
                                if self.order_manager:
                                    _s     = self.order_manager._daily_stats
                                    gross  = float(self.order_manager._daily_pnl)
                                    trades = int(_s.total_trades)
                                    wins   = int(_s.winning_trades)
                                    losses = int(_s.losing_trades)
                                    _settle_note = "in-memory"
                                elif hasattr(self, '_paper_trader') and self._paper_trader:
                                    summary = self._paper_trader.get_summary()
                                    gross   = float(summary.get("total_pnl", 0))
                                    trades  = int(summary.get("total_trades", 0))
                                    wins    = int(summary.get("wins", 0))
                                    losses  = int(summary.get("losses", 0))
                                    _settle_note = "paper_trader"
                                else:
                                    gross, trades, wins, losses = 0.0, 0, 0, 0
                                    _settle_note = "no source"
                            else:
                                # Dhan settlement has the gross; get trade counts
                                # from order_manager for win/loss stats
                                if self.order_manager:
                                    _s     = self.order_manager._daily_stats
                                    trades = int(_s.total_trades)
                                    wins   = int(_s.winning_trades)
                                    losses = int(_s.losing_trades)
                                else:
                                    trades, wins, losses = 0, 0, 0

                            logger.info(f"EOD P&L source: {_settle_note} | gross ₹{gross:,.2f}")
                            profit_tracker.record_daily_pnl(
                                gross_pnl=gross,
                                total_trades=trades,
                                wins=wins,
                                losses=losses,
                                notes=f"Auto-recorded at 15:30 ({_settle_note})",
                            )
                            await self._broadcast_message(
                                f"📊 EOD profit recorded — gross ₹{gross:,.2f} | {trades} trades",
                                "system"
                            )
                            logger.info(f"EOD profit recorded: gross ₹{gross:,.2f}, trades {trades}")
                            # Sync today's ledger rows (with P&L) to Google Sheets
                            try:
                                from datetime import date as _date
                                import os as _os, json as _json
                                today_str = _date.today().isoformat()
                                journal_dir = _os.path.join(
                                    _os.path.dirname(_os.path.dirname(__file__)), "journals"
                                )
                                sheet_rows = []
                                fname = f"journal_{today_str}.json"
                                fpath = _os.path.join(journal_dir, fname)
                                if _os.path.exists(fpath):
                                    with open(fpath) as _jf:
                                        entries = _json.load(_jf)
                                    for e in entries:
                                        if e.get("event_type") != "TRADE":
                                            continue
                                        d = e.get("details", {})
                                        action = d.get("action", "")
                                        if action not in ("SELL", "BUY"):
                                            continue
                                        sym = d.get("symbol", "") or d.get("customSymbol", "")
                                        if not sym:
                                            sym = e.get("direction", "") or "—"
                                        price = float(d.get("price") or d.get("premium") or d.get("tradedPrice") or 0)
                                        pnl_val = None
                                        if action == "SELL":
                                            pnl_val = d.get("net_pnl") or d.get("gross_pnl") or d.get("pnl")
                                            if pnl_val is not None:
                                                pnl_val = float(pnl_val)
                                        opt = "CE" if "CE" in sym or "CALL" in sym else ("PE" if "PE" in sym or "PUT" in sym else "")
                                        sheet_rows.append({
                                            "date":        today_str,
                                            "time":        e.get("timestamp", "")[:19].replace("T", " "),
                                            "symbol":      sym,
                                            "strike":      float(d.get("strike") or d.get("drvStrikePrice") or 0),
                                            "option_type": opt,
                                            "action":      action,
                                            "premium":     price,
                                            "quantity":    int(d.get("quantity") or d.get("tradedQuantity") or 0),
                                            "pnl":         pnl_val,
                                            "source":      "journal_eod",
                                        })
                                if sheet_rows:
                                    profit_tracker.sync_trades_to_google_sheets(sheet_rows)
                            except Exception as _ts_err:
                                logger.warning(f"EOD trade sync to Sheets failed: {_ts_err}")
                            # Send EOD summary to Telegram
                            _eod_sign = "📈" if gross >= 0 else "📉"
                            _wr_str   = f"{wins}/{trades}" if trades else "0/0"
                            _eod_tg = (
                                f"{_eod_sign} *End of Day Summary*\n"
                                f"Date: {now.strftime('%d %b %Y')}\n"
                                f"Total P&L: ₹{gross:,.2f}\n"
                                f"Trades: {trades} (W: {wins} / L: {losses})\n"
                                f"Win Rate: {int(wins/trades*100) if trades else 0}%"
                            )
                            await self._send_telegram_alert(_eod_tg, "system")
                        except Exception as _pt_err:
                            logger.error(f"EOD profit recording failed: {_pt_err}")

                # Check if we have open positions
                if not self.order_manager or not self.order_manager._positions:
                    await asyncio.sleep(check_interval)
                    continue
                
                # Get current market prices
                # Use the last signal cached by the main analysis loop — do NOT call
                # analyze() here because it internally calls fetch_data() (yfinance),
                # meaning we'd hammer the API every 15 s just for a status-display value.
                # Also, gating position monitoring on analyze() succeeding means SL/time-stop
                # checks silently skip whenever yfinance is slow or rate-limited.
                signal = getattr(self, '_last_signal', None)
                
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
                                # Cache last-known good price on the trade so we can
                                # fall back to it when LTP feed is temporarily unavailable.
                                trade._last_known_price = price
                            else:
                                # LTP unavailable this cycle — fall back to last known price
                                # so SL/target checks continue instead of silently skipping.
                                _fallback = getattr(trade, '_last_known_price', 0.0)
                                if _fallback > 0:
                                    current_prices[trade.symbol] = _fallback
                                    logger.debug(
                                        f"LTP unavailable for {trade.symbol} — "
                                        f"using last known price ₹{_fallback:.2f}"
                                    )
                                else:
                                    logger.debug(
                                        f"LTP unavailable for {trade.symbol} and no cached price — "
                                        f"skipping SL/target check this cycle"
                                    )
                        except Exception as e:
                            # On broker connection error, fall back to last known price
                            _fallback = getattr(trade, '_last_known_price', 0.0)
                            if _fallback > 0:
                                current_prices[trade.symbol] = _fallback
                                logger.debug(f"Broker error for {trade.symbol}, using cached ₹{_fallback:.2f}: {e}")
                            else:
                                logger.debug(f"Could not fetch price for {trade.symbol}: {e}")
                
                # Cache prices for get_status()
                self._current_prices = current_prices

                # ── Theta drain warning: log if theta killing a stagnant position ──
                if self.order_manager and getattr(settings.trading, 'theta_exit_enabled', True):
                    for _td_trade in list(self.order_manager._positions.values()):
                        if _td_trade.status != "OPEN":
                            continue
                        _td_price = current_prices.get(_td_trade.symbol, 0.0)
                        if _td_price <= 0 or _td_trade.price <= 0:
                            continue
                        try:
                            _opt_type = (_td_trade.option_type.value
                                         if hasattr(_td_trade.option_type, 'value')
                                         else str(_td_trade.option_type))
                            _should_warn, _warn_msg = iv_monitor.check_theta_drain(
                                symbol=self._active_index.name,
                                strike=_td_trade.strike,
                                option_type=_opt_type,
                                spot=signal.current_price if signal else _td_trade.price * 100,
                                entry_price=_td_trade.price,
                                current_price=_td_price,
                                quantity=_td_trade.quantity,
                            )
                            if _should_warn:
                                logger.warning(_warn_msg)
                                await self._broadcast_message(_warn_msg, "alert")
                        except Exception:
                            pass

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
                            elif tier_label == "TIER2":
                                # Both tiers now done — trailing stop will switch to tight mode
                                trade_ref.tier2_exited = True
                            locked_pct = settings.trading.take_profit_tier_1_percent if tier_label == "TIER1" \
                                else settings.trading.take_profit_tier_2_percent
                            _day_pnl = self.order_manager._daily_pnl
                            _day_sign = "📈" if _day_pnl >= 0 else "📉"
                            msg = (
                                f"💰 {tier_label} profit exit: {trade_ref.symbol} "
                                f"— sold {tier_qty} units @ {locked_pct:.0f}% gain | "
                                f"₹{exit_price:.2f}"
                            )
                            tg_tier_msg = f"{msg}\n{_day_sign} Day P&L: ₹{_day_pnl:,.2f}"
                            await self._broadcast_message(msg, "trade_success")
                            await self._send_telegram_alert(tg_tier_msg, "trade_exit")
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
                            _pnl_at_attempt = ((exit_price - trade_ref.price) * trade_ref.quantity
                                               if trade_ref and trade_ref.price > 0 else 0.0)

                            # Retry close up to 3 times on transient broker errors (e.g. DH-905 Invalid IP).
                            # Each retry waits 2s to allow IP/session to recover.
                            # IMPORTANT: before each retry verify position still exists — if a previous
                            # attempt succeeded on Dhan but returned a timeout, we must NOT sell again
                            # (that would create a naked short position).
                            _max_close_attempts = 3
                            result = None
                            for _attempt in range(1, _max_close_attempts + 1):
                                # Guard: if position was already removed by a previous attempt, stop.
                                if trade_id not in self.order_manager._positions:
                                    logger.info(f"Close retry {_attempt}: position {trade_id} already closed — skipping")
                                    result = None
                                    break
                                result = await self.order_manager.close_position(
                                    trade_id=trade_id, exit_price=exit_price
                                )
                                if result.success:
                                    break
                                _is_transient = any(
                                    kw in (result.message or "")
                                    for kw in ("Invalid IP", "DH-905", "timeout", "connection", "network")
                                )
                                if _is_transient and _attempt < _max_close_attempts:
                                    logger.warning(
                                        f"Close attempt {_attempt}/{_max_close_attempts} failed for {trade_id} "
                                        f"(transient error: {result.message[:60]}) — retrying in 2s"
                                    )
                                    await asyncio.sleep(2)
                                    # Refresh exit price in case premium moved during retry window
                                    try:
                                        _fresh = await self.kite.get_instrument_price(trade_ref.symbol)
                                        if _fresh and _fresh > 0:
                                            exit_price = _fresh
                                    except Exception:
                                        pass
                                else:
                                    break  # permanent error or max retries — stop

                            if result and result.success:
                                await self._broadcast_message(
                                    f"✅ Position auto-closed: {result.message}",
                                    "trade_success"
                                )
                                pnl_display = f"₹{trade_ref.pnl:,.2f}" if trade_ref and trade_ref.pnl is not None else "N/A"
                                _day_pnl = self.order_manager._daily_pnl
                                _day_sign = "📈" if _day_pnl >= 0 else "📉"
                                tg_message = (
                                    f"✅ *Position Closed*\n"
                                    f"{trade_ref.symbol if trade_ref else trade_id}\n"
                                    f"Trade P&L: {pnl_display}\n{result.message}\n"
                                    f"{_day_sign} Day P&L: ₹{_day_pnl:,.2f}"
                                )
                                await self._send_telegram_alert(tg_message, "trade_exit")
                                logger.info(f"Auto-closed position {trade_id}: {result.message}")
                                # Track EOD strategy win/loss for performance monitoring
                                if trade_ref and getattr(trade_ref, "source", "") == "EOD":
                                    _eod_tracker.record(bool(trade_ref.pnl and trade_ref.pnl > 0))
                                # Feed closed trade into performance optimizer for Kelly updates
                                if trade_ref:
                                    try:
                                        self.perf_optimizer.add_trade(trade_ref)
                                        if self.position_sizer:
                                            self.position_sizer.update_stats(
                                                self.perf_optimizer.get_strategy_stats()
                                            )
                                    except Exception:
                                        pass
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
                                _symbol = trade_ref.symbol if trade_ref and hasattr(trade_ref, 'symbol') else trade_id
                                _pnl_str = f"₹{trade_ref.pnl:+,.0f}" if trade_ref and trade_ref.pnl else "unknown"
                                # ── Profit Protection: close failed while in profit ────────────────────
                                # Tighten the trailing stop to 3% from peak so the NEXT
                                # monitoring cycle exits near the high instead of bleeding
                                # all the way to SL while waiting for IP to recover.
                                if trade_ref and _pnl_at_attempt > 0:
                                    trade_ref.profit_protection = True
                                    logger.warning(
                                        f"⚠️ Profit protection activated for {_symbol}: "
                                        f"close failed at ₹{_pnl_at_attempt:+,.0f} profit — "
                                        f"trail tightened to 3% from peak"
                                    )
                                    await self._broadcast_message(
                                        f"⚠️ PROFIT PROTECTION ON: {_symbol} — "
                                        f"close failed at ₹{_pnl_at_attempt:+,.0f}, "
                                        f"trail locked 3% from peak. Close manually if needed!",
                                        "trade_error"
                                    )
                                await self._send_telegram_alert(
                                    f"🚨 URGENT — EXIT ORDER FAILED ({_max_close_attempts} attempts)\n"
                                    f"Symbol : {_symbol}\n"
                                    f"P&L    : {_pnl_str}\n"
                                    f"Error  : {(result.message or '')[:80]}\n"
                                    f"Profit Protection: {'ON — trail at 3%' if (trade_ref and trade_ref.profit_protection) else 'OFF'}\n"
                                    f"➡️ CLOSE MANUALLY on Dhan app NOW!",
                                    "trade_error"
                                )
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
                self._error_counts["position_monitor"] = self._error_counts.get("position_monitor", 0) + 1
                logger.exception(
                    f"Position monitor unhandled exception "
                    f"(total: {self._error_counts['position_monitor']})"
                )
                await self._broadcast_message(f"⚠️ Position monitor error: {e}", "error")
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

        # VIX guard — same filter applied to all auto-entries
        if settings.trading.vix_filter_enabled and self._cached_vix > settings.trading.vix_max:
            msg = (f"ORB: Skipped live entry — India VIX {self._cached_vix:.1f} "
                   f"> {settings.trading.vix_max} (auto-entry blocked)")
            logger.info(msg)
            await self._broadcast_message(msg, "alert")
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
            source="ORB",
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

        # ── India VIX filter: skip auto-entries when premiums are too expensive ──
        # Applies to all non-manual sources. Manual trades always go through.
        _vix_value = 0.0  # captured here so it can be stamped on the trade record below
        if source != "Manual" and getattr(settings.trading, 'vix_filter_enabled', False):
            try:
                import yfinance as _yf
                _vix_info  = _yf.Ticker("^INDIAVIX").fast_info
                _vix_value = float(_vix_info.get("lastPrice") or 0)
                if _vix_value <= 0:
                    _vd = _yf.Ticker("^INDIAVIX").history(period="1d", interval="1d")
                    if not _vd.empty:
                        _vix_value = float(_vd["Close"].iloc[-1])
                _vix_max = getattr(settings.trading, 'vix_max', 20.0)
                if _vix_value > _vix_max:
                    _msg = (
                        f"Entry skipped [{source}] — India VIX {_vix_value:.1f} > {_vix_max:.1f}. "
                        f"Premiums too expensive for option buying."
                    )
                    logger.info(_msg)
                    await self._broadcast_message(_msg, "alert")
                    self._filter_blocks["VIX"] = self._filter_blocks.get("VIX", 0) + 1
                    return False
                elif _vix_value > 0:
                    logger.debug(f"VIX filter passed: India VIX = {_vix_value:.1f} (max {_vix_max:.1f})")
            except Exception as _vix_err:
                logger.warning(f"VIX filter skipped (fetch failed): {_vix_err}")

        # ── IV Percentile filter: block entry when ATM IV is historically expensive ──
        # Applies to all non-manual sources. Needs 5+ days of IV history to activate.
        _iv_check_result = None
        if source != "Manual":
            try:
                _iv_check_result = iv_monitor.check_entry(
                    symbol=self._active_index.name,
                    strike=int(requested_strike) if (requested_strike and requested_strike != "ATM") else int(signal.current_price),
                    option_type=option_type,
                    spot=signal.current_price,
                    quantity=quantity or self.order_manager.get_active_quantity(),
                )
                if not _iv_check_result.allowed:
                    _msg = f"Entry skipped [{source}] — {_iv_check_result.reason}"
                    logger.info(_msg)
                    await self._broadcast_message(_msg, "alert")
                    self._filter_blocks["IV"] = self._filter_blocks.get("IV", 0) + 1
                    return False
                elif _iv_check_result.iv_pct > 0:
                    logger.info(
                        f"IV check [{source}]: {_iv_check_result.reason}"
                        + (f" | Δ={_iv_check_result.greeks.delta:+.3f}"
                           f" θ=₹{_iv_check_result.greeks.theta_daily:+.2f}/day"
                           f" ν=₹{_iv_check_result.greeks.vega:.2f}/1%"
                           if _iv_check_result.greeks else "")
                    )
            except Exception as _iv_err:
                logger.debug(f"IV percentile check skipped: {_iv_err}")

        # ── Spot price sanity check ────────────────────────────────────────────
        # If yfinance returns bad/stale data for ^BSESN the signal.current_price
        # can come back as NIFTY's price (~22900), producing an impossible SENSEX
        # strike that is never in the instrument master → perpetual DH-905 loop.
        # strike_range is defined in IndexConfig for all three indices.
        _smin, _smax = self._active_index.strike_range
        if not (_smin <= signal.current_price <= _smax):
            _price_err = (
                f"[{source}] {self._active_index.name} {option_type} aborted — "
                f"spot price {signal.current_price:,.0f} is outside valid range "
                f"{_smin:,}–{_smax:,}. Likely stale/wrong yfinance data for "
                f"{self._active_index.yahoo_symbol}. Skipping this cycle."
            )
            logger.error(_price_err)
            await self._broadcast_message(_price_err, "error")
            return False

        # Step 1: Perform deep research
        await self._broadcast_message(
            f"🔍 Performing deep research before {option_type} trade...",
            "system"
        )
        
        try:
            analysis = self.research.analyze_option_chain(
                spot_price=signal.current_price,
                trend=signal.trend.value,
                trend_strength=signal.strength,
                rsi=signal.rsi,
                strike_interval=self._active_index.strike_interval,
            )
        except Exception:
            logger.exception(
                f"[RESEARCH] analyze_option_chain() failed "
                f"— aborting {option_type} trade entry"
            )
            return
        
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
            quantity=quantity,
            source=source,
        )
        
        if result.success:
            await self._broadcast_message(
                f"✅ {source} trade executed: {result.message}",
                "trade_success"
            )
            # Stamp VIX at entry on the newly created trade record (analytics / journal)
            if _vix_value > 0:
                for _t in self.order_manager._positions.values():
                    if (_t.status == "OPEN"
                            and _t.option_type.value == option_type
                            and _t.strike == final_strike):
                        _t.vix_at_entry = _vix_value
                        # Stamp Greeks & IV percentile if the IV check ran successfully
                        if _iv_check_result and _iv_check_result.iv_pct > 0:
                            _t.iv_pct_at_entry = _iv_check_result.iv_pct
                            _t.iv_percentile_at_entry = _iv_check_result.iv_percentile
                            if _iv_check_result.greeks:
                                _t.delta_at_entry       = _iv_check_result.greeks.delta
                                _t.theta_daily_at_entry = _iv_check_result.greeks.theta_daily
                                _t.vega_at_entry        = _iv_check_result.greeks.vega
                        break
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

            # ── Mark the appropriate daily slot so this index can't fire again ──
            if source in ("Auto", "Manual", "GAP", "Rule"):
                _today = datetime.now().date()
                _idx   = self._active_index.name
                if source == "Auto":
                    # Auto trend-trades consume BOTH slots so the analysis loop
                    # cannot open a second same-session entry after SL hits and
                    # the position closes — prevents double-loss whipsaw.
                    self._index_orb_triggered[_idx] = _today
                    self._index_vwap_triggered[_idx] = _today
                    logger.info(f"Auto trade consumed both ORB + VWAP slots for {_idx} today")
                    # Lock day direction so ORB won't trade opposite side after TREND fires
                    self._index_day_direction[_idx] = option_type
                    logger.info(f"Auto [{_idx}]: day direction locked to {option_type}")
                else:
                    if self._index_orb_triggered.get(_idx) != _today:
                        self._index_orb_triggered[_idx] = _today
                        logger.info(f"{source} trade marked ORB slot as used for {_idx} today")
                    elif self._index_vwap_triggered.get(_idx) != _today:
                        self._index_vwap_triggered[_idx] = _today
                        logger.info(f"{source} trade marked VWAP slot as used for {_idx} today")

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
    
    # ------------------------------------------------------------------
    # Gift Nifty pre-market prediction
    # ------------------------------------------------------------------

    async def _check_gift_nifty_premarket(self) -> None:
        """
        Runs once pre-market (8:45–9:10 AM) per day.
        Fetches Gift Nifty (NSE Singapore) to estimate the expected opening gap.
        Result is logged and sent via Telegram so you can pre-plan before 9:15.
        """
        if self._gift_nifty_predicted:
            return

        try:
            import yfinance as yf

            # NIFTY last close
            nifty_df = await asyncio.to_thread(
                lambda: yf.Ticker("^NSEI").history(period="2d", interval="1d")
            )
            if nifty_df.empty or len(nifty_df) < 2:
                logger.debug("Gift Nifty: could not fetch NIFTY previous close")
                return
            prev_close = float(nifty_df["Close"].iloc[-1])

            # Gift Nifty (NSE Singapore) — ticker varies; try both
            gift_price = 0.0
            for ticker in ("NIFTY.SG", "^NIFTYIT"):
                try:
                    _df = await asyncio.to_thread(
                        lambda t=ticker: yf.Ticker(t).history(period="1d", interval="1d")
                    )
                    if not _df.empty:
                        gift_price = float(_df["Close"].iloc[-1])
                        break
                except Exception:
                    continue

            # Fallback: if Gift Nifty unavailable, use NIFTY futures premium estimate
            if gift_price <= 0:
                logger.debug("Gift Nifty: live data unavailable — skipping prediction")
                self._gift_nifty_predicted = True
                return

            gap_pct = (gift_price - prev_close) / prev_close * 100
            direction = "UP ⬆️" if gap_pct > 0 else "DOWN ⬇️"
            strength  = "STRONG" if abs(gap_pct) > 1.5 else ("MODERATE" if abs(gap_pct) > 0.75 else "FLAT")
            action    = (
                "→ Pre-plan PE at market open" if gap_pct <= -1.5
                else "→ Pre-plan CE at market open" if gap_pct >= 1.5
                else "→ Wait for ORB direction"
            )

            msg = (
                f"🌅 *Pre-Market Gap Prediction*\n"
                f"NIFTY prev close: {prev_close:,.0f}\n"
                f"Gift Nifty: {gift_price:,.0f}\n"
                f"Expected gap: {gap_pct:+.2f}% {direction} ({strength})\n"
                f"{action}"
            )
            logger.info(
                f"Pre-market: NIFTY prev={prev_close:.0f} | Gift={gift_price:.0f} "
                f"| expected gap {gap_pct:+.2f}% ({strength})"
            )
            await self._send_telegram_alert(msg, "premarket")
            self._gift_nifty_predicted = True

        except Exception as exc:
            logger.debug(f"Gift Nifty check error: {exc}")

    # ------------------------------------------------------------------
    # Straddle strategy signal check
    # ------------------------------------------------------------------

    async def _check_straddle_signal(
        self,
        idx_name: str,
        idx_price: float,
        regime: Optional[str],
    ) -> None:
        """
        Evaluate straddle entry conditions for the given index.
        Fires only during 9:30–11:00 AM when market is flat and VIX is suitable.
        """
        try:
            # Fetch VIX for entry filter
            import yfinance as yf
            _vix_info = yf.Ticker("^INDIAVIX").fast_info
            vix = float(_vix_info.get("lastPrice") or 0)
            if vix <= 0:
                _df = await asyncio.to_thread(
                    lambda: yf.Ticker("^INDIAVIX").history(period="1d", interval="1d")
                )
                vix = float(_df["Close"].iloc[-1]) if not _df.empty else 0.0

            # Get today's gap % from the gap detector
            gap = self._gap_detector.analyze()
            gap_pct = gap.gap_pct if gap else 0.0

            pos = self.straddle_strategy.enter_paper(
                index_name=idx_name,
                index_level=idx_price,
                vix=vix,
                gap_pct=gap_pct,
                regime=regime,
            )
            if pos:
                tg_msg = (
                    f"📋 *PAPER STRADDLE ENTRY*\n"
                    f"Index: {idx_name} | Level: {idx_price:,.0f}\n"
                    f"CE {pos.ce_leg.strike} @₹{pos.ce_leg.premium_entry} | "
                    f"PE {pos.pe_leg.strike} @₹{pos.pe_leg.premium_entry}\n"
                    f"Total cost: ₹{pos.total_premium_entry}\n"
                    f"VIX: {vix:.1f} | Gap: {gap_pct:+.2f}%"
                )
                await self._send_telegram_alert(tg_msg, "paper_entry")

        except Exception as exc:
            logger.debug(f"Straddle signal check error ({idx_name}): {exc}")

    async def _check_gap_signal(self):
        """
        Run gap detection once per trading day near market open.

        Decision flow:
          1. Fetch gap via GapDetector.analyze() (cached per-index per-day)
          2. If GapConfig.use_playbook → use GapPlaybook to auto-select
               FADE (exhaustion/weak-volume) or CONTINUATION (breakaway/high-volume)
          3. Otherwise fall back to legacy STRONG/MODERATE classification:
               Strong gap  → trade immediately
               Moderate gap → alert only, let ORB confirm
          4. Max 1 gap trade per index per day
        """
        if self._index_gap_traded.get(self._active_index.name):
            return

        try:
            gap = self._gap_detector.analyze()
            if not gap:
                return

            report = self._gap_detector.format_report(gap)
            await self._broadcast_message(report, "gap_analysis")
            await self._send_telegram_alert(report, "gap_analysis")
            logger.info(f"Gap analysis done: {gap.gap_type.value} {gap.gap_pct:+.2f}%")

            # No trade needed for neutral gaps — mark as done so we don't re-send every cycle
            if gap.gap_type == GapType.NEUTRAL:
                self._index_gap_traded[self._active_index.name] = True
                return

            # Record gap direction for this index so ORB won't fade the day's gap
            self._index_gap_direction[self._active_index.name] = (
                "UP" if gap.gap_pct > 0 else "DOWN"
            )

            # ── Playbook path (enhanced strategy selection) ───────────────
            cfg     = settings.gap
            use_pb  = getattr(cfg, "use_playbook", True)
            gap_info_dict = {
                "gap_pct":    gap.gap_pct,
                "gap_points": gap.gap_points,
                "direction":  "UP" if gap.gap_pct > 0 else "DOWN",
                "prev_close": gap.prev_close,
                "open_price": gap.open_price,
            }
            # Enrich gap_info with category from GapDetector rich result
            rich = self._gap_detector.get_gap_info()
            if rich:
                gap_info_dict["category"] = rich.category.value
                gap_info_dict["strength"] = rich.strength.value

            if use_pb and rich:
                from datetime import datetime as _dt
                now_ist = _dt.now()
                context = {
                    "weekday":          now_ist.weekday(),
                    "time_since_open":  (now_ist.hour * 60 + now_ist.minute) - (9 * 60 + 15),
                    "current_price":    gap.open_price,     # best proxy pre-live data
                    "open_price":       gap.open_price,
                    "volume_ratio":     1.0,                # default — real ratio not available at open
                    "trend_direction":  "NEUTRAL",
                    "is_near_sr":       False,
                    "sr_level":         0,
                    "trend_alignment":  "WITH",
                    "fill_probability": 55.0,
                }
                # Try to get VIX from cache
                try:
                    if self._cached_vix > 0:
                        context["vix"] = self._cached_vix
                except Exception:
                    pass
                # Try to get live trend direction; keep reference for AI validation + RSI check
                _gap_live_sig = None
                try:
                    sig_live = self.analyzer.analyze()
                    if sig_live:
                        _gap_live_sig = sig_live
                        context["current_price"] = sig_live.current_price
                        context["trend_direction"] = sig_live.trend.value
                        trend_val = sig_live.trend.value
                        direction = gap_info_dict.get("direction", "UP")
                        if (direction == "UP"   and trend_val == "BULLISH") or \
                           (direction == "DOWN"  and trend_val == "BEARISH"):
                            context["trend_alignment"] = "WITH"
                        elif trend_val != "NEUTRAL":
                            context["trend_alignment"] = "AGAINST"
                except Exception:
                    pass

                playbook = GapPlaybook()
                rec      = playbook.select_strategy(gap_info_dict, context)
                strategy_chosen = rec.get("strategy", "SKIP")
                confidence      = rec.get("confidence", 0)
                reasoning       = rec.get("reasoning", [])

                pb_msg = (
                    f"📋 **GapPlaybook [{self._active_index.name}]**: "
                    f"strategy={strategy_chosen}  confidence={confidence}%  "
                    f"gap={gap.gap_pct:+.2f}%  category={gap_info_dict.get('category','?')}\n"
                    + "\n".join(f"   • {r}" for r in reasoning)
                )
                await self._broadcast_message(pb_msg, "gap_analysis")
                logger.info(
                    f"GapPlaybook [{self._active_index.name}]: strategy={strategy_chosen} "
                    f"confidence={confidence}%  reasons={reasoning}"
                )

                if strategy_chosen == "SKIP":
                    logger.info(f"GapPlaybook: SKIP — {reasoning}")
                    return

                # Map playbook decision to option direction
                gap_dir = gap_info_dict.get("direction", "UP")
                if strategy_chosen == "FADE":
                    opt_direction = "PE" if gap_dir == "UP" else "CE"
                else:  # CONTINUATION
                    opt_direction = "CE" if gap_dir == "UP" else "PE"

                if not self._auto_trade:
                    # ── AI validation in paper mode ──────────────────────
                    _paper_gap_ai_ok = True
                    if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                        try:
                            from bot.ai_gateway import get_ai_gateway
                            _gw_pg = get_ai_gateway()
                            _fake_sig_pg = self.analyzer.analyze()
                            if _fake_sig_pg:
                                _gw_pg_ctx = {
                                    'option_type':        opt_direction,
                                    'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                                    'vix':                self._cached_vix,
                                    'consecutive_losses': 0,
                                    'trades_done_today':  0,
                                    'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                                }
                                _gw_pg_res = await _gw_pg.validate_strategy_signal(_fake_sig_pg, f'GAP_{strategy_chosen[:4]}', _gw_pg_ctx)
                                if not _gw_pg_res['approved']:
                                    logger.info(f"PAPER GAP [{strategy_chosen}]: AI blocked — {_gw_pg_res['reasoning']}")
                                    await self._broadcast_message(
                                        f"🤖 PAPER GAP [{strategy_chosen}]: AI blocked {opt_direction} — {_gw_pg_res['reasoning']}",
                                        "paper_trade",
                                    )
                                    _paper_gap_ai_ok = False
                                else:
                                    logger.info(f"PAPER GAP [{strategy_chosen}]: AI approved conf={_gw_pg_res['confidence']:.0f}%")
                        except Exception as _ai_pg_err:
                            logger.warning(f"PAPER GAP [{strategy_chosen}]: AI check failed ({_ai_pg_err}), proceeding")
                    if _paper_gap_ai_ok:
                        note = (
                            f"📊 PAPER GAP [{strategy_chosen}]: "
                            f"{gap.gap_pct:+.2f}%  {gap_info_dict.get('category','?')} gap — "
                            f"would enter {opt_direction}  confidence={confidence}%"
                        )
                        await self._broadcast_message(note, "paper_trade")
                        logger.info(
                            f"PAPER GAP [{strategy_chosen}]: Would enter {opt_direction} "
                            f"gap={gap.gap_pct:+.2f}% — paper mode"
                        )
                        # Use today's open price as paper entry (we'd enter at market open)
                        _gap_px = gap.open_price or context.get("current_price", 0.0)
                        self.paper_trader.paper_buy(
                            index_name=self._active_index.name,
                            index_price=_gap_px,
                            option_type=opt_direction,
                            strategy=f"GAP_{strategy_chosen[:4]}",   # GAP_FADE / GAP_CONT
                            quantity=self._active_index.lot_size,
                        )
                        self._index_gap_traded[self._active_index.name] = True
                        # Record today's direction so ORB won't trade against it
                        self._index_day_direction[self._active_index.name] = opt_direction
                    else:
                        self._index_gap_traded[self._active_index.name] = True
                    return

                # Live order
                if not self.order_manager:
                    return
                can_trade, reason = self.order_manager.can_place_order()
                if not can_trade:
                    logger.info(f"Gap [{strategy_chosen}] trade blocked: {reason}")
                    return

                # Use gap.open_price (fetched by GapDetector from the correct per-index
                # ^yahoo_symbol) to avoid cross-index contamination where signal_live.current_price
                # might carry NIFTY's price (~23800) into a SENSEX strike calculation (~77000).
                _spot = gap.open_price if gap.open_price > 0 else 0
                _sr   = getattr(self._active_index, 'strike_range', None)
                if _spot <= 0 or (_sr and not (_sr[0] <= _spot <= _sr[1])):
                    logger.error(
                        f"Gap [{self._active_index.name}]: spot price {_spot:.0f} invalid or "
                        f"outside strike_range {_sr} — aborting to prevent wrong strike"
                    )
                    return
                strike_interval = self._active_index.strike_interval
                atm_strike      = round(_spot / strike_interval) * strike_interval

                from browser.dhan import OptionType
                opt = OptionType.CE if opt_direction == "CE" else OptionType.PE
                qty = int(self._active_index.lot_size * getattr(cfg, "quantity_multiplier", 0.5))
                qty = max(qty, self._active_index.lot_size)  # floor at one lot

                # ── RSI extreme block: CONTINUATION PE/CE at crash/rally extremes ──
                # GAP FADE (CE after gap-down, PE after gap-up) is intentionally counter-trend
                # so RSI extreme is acceptable. CONTINUATION chases the move — dangerous.
                if _gap_live_sig:
                    _gap_rsi = getattr(_gap_live_sig, 'rsi', 50)
                    _is_continuation = (strategy_chosen == "CONTINUATION")
                    if _is_continuation and opt_direction == "PE" and _gap_rsi < 25:
                        msg = (f"GAP [{strategy_chosen}]: Skipping PE — RSI {_gap_rsi:.1f} < 25 "
                               f"extreme oversold, chasing crash too late")
                        logger.info(msg)
                        await self._broadcast_message(msg, "alert")
                        self._index_gap_traded[self._active_index.name] = True
                        self._save_daily_state()
                        return
                    if _is_continuation and opt_direction == "CE" and _gap_rsi > 75:
                        msg = (f"GAP [{strategy_chosen}]: Skipping CE — RSI {_gap_rsi:.1f} > 75 "
                               f"extreme overbought, chasing rally too late")
                        logger.info(msg)
                        await self._broadcast_message(msg, "alert")
                        self._index_gap_traded[self._active_index.name] = True
                        self._save_daily_state()
                        return

                # ── AI Gateway (PROMPT 1): final validation before any order ─────
                if settings.ai.enabled and settings.ai.use_ai_signal_validation and _gap_live_sig:
                    from bot.ai_gateway import get_ai_gateway
                    _gw = get_ai_gateway()
                    _ds  = self.order_manager._daily_stats if self.order_manager else None
                    _gw_ctx = {
                        'option_type':        opt_direction,
                        'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                        'vix':                self._cached_vix,
                        'consecutive_losses': getattr(self.order_manager, '_consecutive_losses', 0) if self.order_manager else 0,
                        'trades_done_today':  getattr(_ds, 'total_trades', 0),
                        'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                    }
                    _gw_res = await _gw.validate_strategy_signal(_gap_live_sig, 'GAP', _gw_ctx)
                    if not _gw_res['approved']:
                        logger.info(f"GAP: AI gateway blocked — {_gw_res['reasoning']}")
                        await self._broadcast_message(
                            f"GAP: AI blocked — {_gw_res['reasoning']}", "alert"
                        )
                        self._filter_blocks["GAP_ai_gateway"] = self._filter_blocks.get("GAP_ai_gateway", 0) + 1
                        return
                    logger.info(
                        f"✅ GAP AI-APPROVED: conf={_gw_res['confidence']:.0f}% "
                        f"action={_gw_res['action']} | {_gw_res['reasoning'][:80]}"
                    )
                    _ai_conf_gap = _gw_res['confidence']
                else:
                    _ai_conf_gap = None

                _orig_sl     = cfg.trading.stop_loss_percentage if hasattr(cfg, 'trading') else settings.trading.stop_loss_percentage
                _orig_target = cfg.trading.target_percentage    if hasattr(cfg, 'trading') else settings.trading.target_percentage
                _sl  = cfg.stop_loss_pct
                _tgt = cfg.target_pct
                settings.trading.stop_loss_percentage = _sl
                settings.trading.target_percentage    = _tgt
                try:
                    result = await self.order_manager.manual_order(
                        option_type=opt.value,
                        strike=atm_strike,
                        order_type="BUY",
                        quantity=qty,
                        source=f"GAP_{strategy_chosen[:4]}",
                    )
                finally:
                    settings.trading.stop_loss_percentage = _orig_sl
                    settings.trading.target_percentage    = _orig_target

                if result.success:
                    self._index_gap_traded[self._active_index.name] = True
                    _idx = self._active_index.name
                    _today_d = datetime.now().date()
                    if self._index_orb_triggered.get(_idx) != _today_d:
                        self._index_orb_triggered[_idx] = _today_d
                        logger.info(f"GAP trade marked ORB slot used for {_idx}")
                    # Lock day direction so ORB/TREND can't trade opposite side
                    self._index_day_direction[_idx] = opt_direction
                    logger.info(f"GAP [{_idx}]: day direction locked to {opt_direction}")
                    msg = (
                        f"🚀 **Gap Trade [{strategy_chosen}] Executed**\n"
                        f"  Category: {gap_info_dict.get('category','?')}\n"
                        f"  Gap: {gap.gap_pct:+.2f}% | Confidence: {confidence}%\n"
                        f"  Bought {opt.value} @ strike {atm_strike} × {qty} units\n"
                        f"  Order: {result.order_id}"
                        + (f"\n🤖 AI Confidence: {_ai_conf_gap:.0f}%" if _ai_conf_gap is not None else "")
                    )
                    await self._broadcast_message(msg, "trade_success")
                    await self._send_telegram_alert(msg, "trade_entry")
                    logger.info(
                        f"Gap [{strategy_chosen}] trade placed: {opt.value} "
                        f"{atm_strike} × {qty}"
                    )
                else:
                    logger.error(f"Gap [{strategy_chosen}] trade failed: {result.message}")
                    await self._broadcast_message(
                        f"❌ Gap trade failed: {result.message}", "trade_error"
                    )
                    # Mark gap as done immediately on any failure and persist to disk.
                    # fail_count was in-memory only and reset on every restart, so the
                    # old '3 failures' guard never fired — each restart re-attempted the
                    # gap, creating duplicate orders.  If the order fails (DH-905, etc.)
                    # it will fail on every retry; mark done and stop.
                    _idx_f = self._active_index.name
                    self._index_gap_traded[_idx_f] = True
                    self._save_daily_state()
                    logger.warning(f"Gap [{_idx_f}]: order failed — marked done for today to prevent restart duplicates")
                    _safe_err = (result.message
                        .replace('_', r'\_').replace('*', r'\*')
                        .replace('[', r'\[').replace('`', r'\`'))
                    await self._send_telegram_alert(
                        f"⚠️ Gap trade for {_idx_f} failed — retries stopped for today.\n"
                        f"Error: {_safe_err}\n"
                        f"Check DH-905 IP whitelist or DH-901 token expiry.",
                        "system"
                    )
                return   # ← end of playbook path

            # ── Legacy path (use_playbook=False or no rich info) ──────────
            # Paper mode: log + track signal, then stop (no real order)
            if not self._auto_trade:
                opt_type = "CE" if gap.trade_direction == "CE" else "PE"
                if gap.wait_for_confirmation:
                    note = (
                        f"📊 PAPER GAP: Moderate gap ({gap.gap_pct:+.2f}%) — "
                        f"would wait for ORB confirmation before entering {opt_type}."
                    )
                    await self._broadcast_message(note, "paper_trade")
                    self._index_gap_traded[self._active_index.name] = True
                else:
                    # ── AI validation in paper mode ──────────────────────
                    _paper_gap_leg_ai_ok = True
                    if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                        try:
                            from bot.ai_gateway import get_ai_gateway
                            _gw_pgl = get_ai_gateway()
                            _sig_pgl = self.analyzer.analyze()
                            if _sig_pgl:
                                _gw_pgl_ctx = {
                                    'option_type':        opt_type,
                                    'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                                    'vix':                self._cached_vix,
                                    'consecutive_losses': 0,
                                    'trades_done_today':  0,
                                    'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                                }
                                _gw_pgl_res = await _gw_pgl.validate_strategy_signal(_sig_pgl, 'GAP', _gw_pgl_ctx)
                                if not _gw_pgl_res['approved']:
                                    logger.info(f"PAPER GAP (legacy): AI blocked — {_gw_pgl_res['reasoning']}")
                                    await self._broadcast_message(
                                        f"🤖 PAPER GAP: AI blocked {opt_type} — {_gw_pgl_res['reasoning']}",
                                        "paper_trade",
                                    )
                                    _paper_gap_leg_ai_ok = False
                                else:
                                    logger.info(f"PAPER GAP (legacy): AI approved conf={_gw_pgl_res['confidence']:.0f}%")
                        except Exception as _ai_pgl_err:
                            logger.warning(f"PAPER GAP (legacy): AI check failed ({_ai_pgl_err}), proceeding")
                    _gap_px = gap.open_price or 0.0
                    if _paper_gap_leg_ai_ok:
                        logger.info(
                            f"PAPER GAP: Would enter {opt_type} on strong {gap.gap_type.value} "
                            f"gap {gap.gap_pct:+.2f}% — paper mode, no order placed"
                        )
                        await self._broadcast_message(
                            f"📋 PAPER GAP: Would enter {opt_type} | {gap.gap_type.value} "
                            f"gap {gap.gap_pct:+.2f}% | open {_gap_px:,.0f} — paper mode",
                            "paper_trade",
                        )
                        self.paper_trader.paper_buy(
                            index_name=self._active_index.name,
                            index_price=_gap_px,
                            option_type=opt_type,
                            strategy="GAP",
                            quantity=self._active_index.lot_size,
                        )
                    self._index_gap_traded[self._active_index.name] = True
                return

            # Moderate gap — just alert; ORB loop will confirm
            if gap.wait_for_confirmation:
                note = (
                    f"📊 Moderate gap detected ({gap.gap_pct:+.2f}%). "
                    f"Waiting for ORB confirmation to enter {gap.trade_direction}."
                )
                await self._broadcast_message(note, "gap_analysis")
                return

            # Strong gap — enter immediately (legacy path)
            if not self.order_manager:
                return

            can_trade, reason = self.order_manager.can_place_order()
            if not can_trade:
                logger.info(f"Gap trade blocked: {reason}")
                return

            # Use gap.open_price (correct per-index symbol) to prevent NIFTY price leaking
            # into SENSEX strike calculation.
            _spot_leg = gap.open_price if gap.open_price > 0 else 0
            _sr_leg   = getattr(self._active_index, 'strike_range', None)
            if _spot_leg <= 0 or (_sr_leg and not (_sr_leg[0] <= _spot_leg <= _sr_leg[1])):
                logger.error(
                    f"Gap (legacy) [{self._active_index.name}]: spot price {_spot_leg:.0f} invalid "
                    f"or outside strike_range {_sr_leg} — aborting to prevent wrong strike"
                )
                return
            strike_interval = self._active_index.strike_interval
            atm_strike      = round(_spot_leg / strike_interval) * strike_interval

            from browser.dhan import OptionType
            from config import settings as cfg
            opt = OptionType.CE if gap.trade_direction == "CE" else OptionType.PE
            qty = int(self._active_index.lot_size * cfg.gap.quantity_multiplier)

            # ── AI Gateway (PROMPT 1): final validation before any order ─────
            if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                from bot.ai_gateway import get_ai_gateway
                _gw = get_ai_gateway()
                _ds  = self.order_manager._daily_stats if self.order_manager else None
                _gw_ctx = {
                    'option_type':        gap.trade_direction,
                    'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                    'vix':                self._cached_vix,
                    'consecutive_losses': getattr(self.order_manager, '_consecutive_losses', 0) if self.order_manager else 0,
                    'trades_done_today':  getattr(_ds, 'total_trades', 0),
                    'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                }
                _gw_res = await _gw.validate_strategy_signal(signal, 'GAP', _gw_ctx)
                if not _gw_res['approved']:
                    logger.info(f"GAP (legacy): AI gateway blocked — {_gw_res['reasoning']}")
                    await self._broadcast_message(
                        f"GAP: AI blocked — {_gw_res['reasoning']}", "alert"
                    )
                    self._filter_blocks["GAP_ai_gateway"] = self._filter_blocks.get("GAP_ai_gateway", 0) + 1
                    return
                logger.info(
                    f"✅ GAP (legacy) AI-APPROVED: conf={_gw_res['confidence']:.0f}% "
                    f"action={_gw_res['action']} | {_gw_res['reasoning'][:80]}"
                )
                _ai_conf_gap_leg = _gw_res['confidence']
            else:
                _ai_conf_gap_leg = None

            _orig_sl     = cfg.trading.stop_loss_percentage
            _orig_target = cfg.trading.target_percentage
            cfg.trading.stop_loss_percentage = cfg.gap.stop_loss_pct
            cfg.trading.target_percentage    = cfg.gap.target_pct
            try:
                result = await self.order_manager.manual_order(
                    option_type=opt.value,
                    strike=atm_strike,
                    order_type="BUY",
                    quantity=qty,
                    source="GAP",
                )
            finally:
                cfg.trading.stop_loss_percentage = _orig_sl
                cfg.trading.target_percentage    = _orig_target

            if result.success:
                self._index_gap_traded[self._active_index.name] = True
                _idx = self._active_index.name
                _today = datetime.now().date()
                if self._index_orb_triggered.get(_idx) != _today:
                    self._index_orb_triggered[_idx] = _today
                    logger.info(f"GAP trade marked ORB slot as used for {_idx} today")
                msg = (
                    f"🚀 **Gap Trade Executed**\n"
                    f"  Gap: {gap.gap_type.value} ({gap.gap_pct:+.2f}%)\n"
                    f"  Bought {opt.value} @ strike {atm_strike} x {qty} units\n"
                    f"  Order: {result.order_id}"
                    + (f"\n🤖 AI Confidence: {_ai_conf_gap_leg:.0f}%" if _ai_conf_gap_leg is not None else "")
                )
                await self._broadcast_message(msg, "trade_success")
                await self._send_telegram_alert(msg, "trade_entry")
                logger.info(f"Gap trade placed: {opt.value} {atm_strike} x {qty}")
            else:
                logger.error(f"Gap trade failed: {result.message}")
                await self._broadcast_message(f"❌ Gap trade failed: {result.message}", "trade_error")
                # Mark gap as done immediately on any failure — same fix as playbook path.
                _idx_f = self._active_index.name
                self._index_gap_traded[_idx_f] = True
                self._save_daily_state()
                logger.warning(f"Gap [{_idx_f}]: order failed — marked done for today to prevent restart duplicates")
                _safe_err_leg = (result.message
                    .replace('_', r'\_').replace('*', r'\*')
                    .replace('[', r'\[').replace('`', r'\`'))
                await self._send_telegram_alert(
                    f"⚠️ Gap trade for {_idx_f} failed — retries stopped for today.\n"
                    f"Error: {_safe_err_leg}\n"
                    f"Check DH-905 IP whitelist or DH-901 token expiry.",
                    "system"
                )

        except Exception as e:
            logger.error(f"Gap signal check error: {e}")

    async def _check_orb_signal(self):
        """Check ORB strategy and execute trade if a breakout is detected."""
        # Engine-level one-trade-per-day guard: survives ORB resets and index switches
        if self._index_orb_triggered.get(self._active_index.name) == datetime.now().date():
            logger.debug(f"ORB [{self._active_index.name}]: already triggered today — skipping")
            self._filter_blocks["ORB_daily_limit"] = self._filter_blocks.get("ORB_daily_limit", 0) + 1
            return
        # ── Early direction-lock guard (paper & live) ─────────────────────────
        # If day direction is locked AND the ORB slot is already consumed (e.g.
        # GAP trade took it), skip the expensive analyze() call entirely.
        # Counter-direction blocking when slot is still available is handled by
        # Guard 3b (after analyze()) so same-direction ORB is not unfairly blocked.
        _idx_name_orb = self._active_index.name
        _today_dir = self._index_day_direction.get(_idx_name_orb, "")
        if _today_dir and self._index_orb_triggered.get(_idx_name_orb) == datetime.now().date():
            # Slot consumed + direction known → nothing ORB can add here
            logger.debug(
                f"ORB [{_idx_name_orb}]: skipping — direction locked to {_today_dir} "
                f"and ORB slot already consumed today"
            )
            self._filter_blocks["ORB_direction_lock"] = self._filter_blocks.get("ORB_direction_lock", 0) + 1
            return
        try:
            orb_signal = self.orb.analyze()
            if not orb_signal:
                logger.debug(
                    f"ORB [{self._active_index.name}]: analyze() returned no signal "
                    f"(state={self.orb._state.value}, "
                    f"range={'built' if self.orb._range_high > 0 else 'not built'}, "
                    f"fired={self.orb._signal_fired})"
                )
                return

            # ── Guard 1: Minimum strength threshold ───────────────────────────
            # 65% = base 60 + any decisive gap bonus (no volume required).
            # Volume confirmation adds +20 pts on top — rewarded but not mandatory
            # since yfinance index volume data is unreliable for Indian indices.
            if orb_signal.strength < 65:
                logger.info(
                    f"ORB: Skipping — strength {orb_signal.strength:.0f}% < 65% threshold"
                    + (f" | India VIX={self._cached_vix:.1f}" if self._cached_vix > 0 else "")
                )
                await self._broadcast_message(
                    f"ORB: Signal skipped — strength {orb_signal.strength:.0f}% below 65% minimum",
                    "alert"
                )
                self._filter_blocks["ORB_strength"] = self._filter_blocks.get("ORB_strength", 0) + 1
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
                    self._filter_blocks["ORB_trend"] = self._filter_blocks.get("ORB_trend", 0) + 1
                    return

                if orb_signal.direction == "SHORT" and _trend == "BULLISH":
                    logger.info("ORB: Skipping SHORT — overall trend is BULLISH (counter-trend)")
                    await self._broadcast_message(
                        "ORB: SHORT signal skipped — market trend is BULLISH", "alert"
                    )
                    self._filter_blocks["ORB_trend"] = self._filter_blocks.get("ORB_trend", 0) + 1
                    return

                # ── Guard 2b: Gap-day direction guard ─────────────────────────
                # If a non-neutral gap was detected today, don't let ORB fade it.
                # e.g. +1.5% gap-up day: even if 5m turns neutral, a SHORT ORB
                # signal below the opening range is still fading the bullish gap.
                _today_gap_dir = self._index_gap_direction.get(self._active_index.name, "")
                if _today_gap_dir == "UP" and orb_signal.direction == "SHORT":
                    logger.info(
                        f"ORB: Skipping SHORT — gap-UP day "
                        f"({self._active_index.name}), ORB counter-gap fade blocked"
                    )
                    await self._broadcast_message(
                        f"ORB: PE signal skipped — today is a gap-UP day "
                        f"({self._active_index.name}), no counter-gap short",
                        "alert",
                    )
                    self._filter_blocks["ORB_gap_dir"] = self._filter_blocks.get("ORB_gap_dir", 0) + 1
                    return
                if _today_gap_dir == "DOWN" and orb_signal.direction == "LONG":
                    logger.info(
                        f"ORB: Skipping LONG — gap-DOWN day "
                        f"({self._active_index.name}), ORB counter-gap fade blocked"
                    )
                    await self._broadcast_message(
                        f"ORB: CE signal skipped — today is a gap-DOWN day "
                        f"({self._active_index.name}), no counter-gap long",
                        "alert",
                    )
                    self._filter_blocks["ORB_gap_dir"] = self._filter_blocks.get("ORB_gap_dir", 0) + 1
                    return

                # ── Guard 3: Higher bar on NEUTRAL days ───────────────────────
                # ORB base strength = 60 (always) + 20 (volume) + up to 20 (gap).
                # On NEUTRAL 5m days allow any breakout with at least 65% strength
                # (volume-confirmed OR a decisive gap ≥1% outside the range).
                # The old 80% bar effectively required volume confirmation every time,
                # but yfinance index volume is unreliable → most signals were silently
                # blocked even with legitimate breakouts.
                if _trend == "NEUTRAL" and orb_signal.strength < 65:
                    logger.info(
                        f"ORB: Skipping — NEUTRAL trend requires ≥65% strength "
                        f"(got {orb_signal.strength:.0f}%)"
                    )
                    await self._broadcast_message(
                        f"ORB: Signal skipped — NEUTRAL market needs ≥65% strength "
                        f"(got {orb_signal.strength:.0f}%)",
                        "alert"
                    )
                    self._filter_blocks["ORB_strength"] = self._filter_blocks.get("ORB_strength", 0) + 1
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

            # ── Guard 3b: Same-day direction lock ─────────────────────────────────
            # If TREND or GAP already fired the opposite direction today on this index,
            # block ORB to avoid trading both sides (Mar 24: GAP fired CE, ORB fired PE → both tiny losses)
            _today_first_dir = self._index_day_direction.get(self._active_index.name, "")
            if _today_first_dir and _today_first_dir != option_type:
                logger.info(
                    f"ORB [{self._active_index.name}]: Skipping {option_type} — "
                    f"today's direction already locked to {_today_first_dir} (set by TREND/GAP)"
                )
                await self._broadcast_message(
                    f"ORB: {option_type} blocked — today's direction locked to {_today_first_dir} "
                    f"(prevents trading both sides on {self._active_index.name})",
                    "alert",
                )
                self._filter_blocks["ORB_direction_lock"] = self._filter_blocks.get("ORB_direction_lock", 0) + 1
                return

            # ── Guard 4: RSI quality filter ─────────────────────────────────────────
            # Uses TRADING_PE_MIN_RSI_ENTRY and TRADING_CE_MAX_RSI_ENTRY (same as
            # _execute_auto_trade) so all strategies apply identical RSI bounds.
            _rsi = self._last_signal.rsi if self._last_signal else 50
            _pe_rsi_floor_orb = float(getattr(settings.trading, 'pe_min_rsi_entry', 35))
            _ce_rsi_ceil_orb  = float(getattr(settings.trading, 'ce_max_rsi_entry', 70))
            if option_type == "CE" and _rsi >= _ce_rsi_ceil_orb:
                logger.info(
                    f"ORB: Skipping LONG — RSI {_rsi:.1f} overbought (≥{_ce_rsi_ceil_orb:.0f})"
                )
                await self._broadcast_message(
                    f"ORB: CE signal skipped — RSI {_rsi:.1f} ≥{_ce_rsi_ceil_orb:.0f} (overbought)",
                    "alert"
                )
                self._filter_blocks["ORB_RSI"] = self._filter_blocks.get("ORB_RSI", 0) + 1
                return
            if option_type == "PE" and _rsi <= _pe_rsi_floor_orb:
                # On strongly trending crash days the RSI stays depressed all session.
                # Allow the trade if the ORB breakout is high-conviction (≥85%) and
                # the regime confirms a downtrend — in that case use a tighter floor of 20.
                _regime_now = getattr(self, "_cached_regime", None)
                _is_trending_down = _regime_now and "TRENDING DOWN" in str(_regime_now.regime.value)
                _relaxed_floor = 20.0
                if orb_signal.strength >= 85 and _is_trending_down and _rsi > _relaxed_floor:
                    logger.info(
                        f"ORB: Allowing SHORT despite RSI {_rsi:.1f} — "
                        f"high-conviction breakout ({orb_signal.strength:.0f}%) in TRENDING DOWN regime "
                        f"(relaxed RSI floor: {_relaxed_floor})"
                    )
                else:
                    logger.info(
                        f"ORB: Skipping SHORT — RSI {_rsi:.1f} oversold (≤{_pe_rsi_floor_orb:.0f})"
                    )
                    await self._broadcast_message(
                        f"ORB: PE signal skipped — RSI {_rsi:.1f} ≤{_pe_rsi_floor_orb:.0f} (oversold, exhausted breakdown)",
                        "alert"
                    )
                    self._filter_blocks["ORB_RSI"] = self._filter_blocks.get("ORB_RSI", 0) + 1
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
                        self._filter_blocks["ORB_MTF"] = self._filter_blocks.get("ORB_MTF", 0) + 1
                        return
                    if option_type == "PE" and _15m.trend == "BULLISH":
                        logger.info(
                            f"ORB: Skipping SHORT — 15m trend is BULLISH — no MTF confluence"
                        )
                        await self._broadcast_message(
                            "ORB: PE signal skipped — 15m chart is BULLISH (no confluence)",
                            "alert"
                        )
                        self._filter_blocks["ORB_MTF"] = self._filter_blocks.get("ORB_MTF", 0) + 1
                        return
            except Exception as _mtf_err:
                logger.warning(f"ORB: MTF check failed ({_mtf_err}), skipping MTF guard")

            # Paper mode: log signal but skip order placement
            if not self._auto_trade:
                # ── AI validation in paper mode ──────────────────────
                _paper_orb_ai_ok = True
                if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                    try:
                        from bot.ai_gateway import get_ai_gateway
                        _gw_po = get_ai_gateway()
                        _sig_po = self._last_signal or self.analyzer.analyze()
                        if _sig_po:
                            _gw_po_ctx = {
                                'option_type':        option_type,
                                'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                                'vix':                self._cached_vix,
                                'consecutive_losses': 0,
                                'trades_done_today':  0,
                                'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                            }
                            _gw_po_res = await _gw_po.validate_strategy_signal(_sig_po, 'ORB', _gw_po_ctx)
                            if not _gw_po_res['approved']:
                                logger.info(f"PAPER ORB [{self._active_index.name}]: AI blocked — {_gw_po_res['reasoning']}")
                                await self._broadcast_message(
                                    f"🤖 PAPER ORB: AI blocked {option_type} — {_gw_po_res['reasoning']}",
                                    "paper_trade",
                                )
                                _paper_orb_ai_ok = False
                            else:
                                logger.info(f"PAPER ORB [{self._active_index.name}]: AI approved conf={_gw_po_res['confidence']:.0f}%")
                    except Exception as _ai_po_err:
                        logger.warning(f"PAPER ORB: AI check failed ({_ai_po_err}), proceeding")
                if _paper_orb_ai_ok:
                    logger.info(
                        f"[SIGNAL] ORB [{self._active_index.name}]: passed all filters → "
                        f"PAPER BUY {option_type} | strength={orb_signal.strength:.0f}% "
                        f"dir={orb_signal.direction} rsi={_rsi:.1f}"
                    )
                    await self._broadcast_message(
                        f"📋 PAPER ORB: Would buy {option_type} | strength {orb_signal.strength:.0f}% "
                        f"| range {self.orb._range_low:,.0f}–{self.orb._range_high:,.0f} (paper mode)",
                        "paper_trade"
                    )
                    self.paper_trader.paper_buy(
                        index_name=self._active_index.name,
                        index_price=self._last_signal.current_price if self._last_signal else 0.0,
                        option_type=option_type,
                        strategy="ORB",
                        quantity=self._active_index.lot_size,
                    )
                    self._index_orb_triggered[self._active_index.name] = datetime.now().date()
                    self._save_daily_state()
                return

            # ── AI Gateway (PROMPT 1): live-trade validation only ─────────────
            if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                from bot.ai_gateway import get_ai_gateway
                _gw = get_ai_gateway()
                _ds  = self.order_manager._daily_stats if self.order_manager else None
                _gw_ctx = {
                    'option_type':        option_type,
                    'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                    'vix':                self._cached_vix,
                    'consecutive_losses': getattr(self.order_manager, '_consecutive_losses', 0) if self.order_manager else 0,
                    'trades_done_today':  getattr(_ds, 'total_trades', 0),
                    'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                }
                _gw_res = await _gw.validate_strategy_signal(self._last_signal, 'ORB', _gw_ctx)
                if not _gw_res['approved']:
                    logger.info(f"🤖 ORB SKIP (AI): {_gw_res['reasoning']}")
                    await self._broadcast_message(
                        f"🤖 AI SKIP [ORB]: {_gw_res['reasoning']}\n"
                        f"Confidence: {_gw_res['confidence']:.0f}%",
                        "alert"
                    )
                    self._filter_blocks["ORB_ai_gateway"] = self._filter_blocks.get("ORB_ai_gateway", 0) + 1
                    return
                logger.info(
                    f"✅ ORB AI-APPROVED: conf={_gw_res['confidence']:.0f}% "
                    f"action={_gw_res['action']} | {_gw_res['reasoning'][:80]}"
                )
                await self._broadcast_message(
                    f"✅ AI-VALIDATED [ORB]: {_gw_res['reasoning']}\n"
                    f"Confidence: {_gw_res['confidence']:.0f}%",
                    "analysis"
                )

            # Fast-path: place ORB trade directly at ATM — skip slow research
            # Research adds 10-15s latency; by then the breakout candle may reverse.
            order_ok = await self._execute_orb_direct(
                signal=self._last_signal,
                option_type=option_type,
                orb_signal=orb_signal,
            )

            if order_ok:
                self._index_orb_triggered[self._active_index.name] = datetime.now().date()
                # Lock day direction so TREND/VWAP can't trade opposite side today
                self._index_day_direction[self._active_index.name] = option_type
                logger.info(f"ORB [{self._active_index.name}]: day direction locked to {option_type}")
                self._save_daily_state()

        except Exception as e:
            logger.error(f"ORB signal check error: {e}")

    async def _check_vwap_signal(self):
        """Check VWAP mean-reversion strategy and execute if a signal fires."""
        # Engine-level one-trade-per-day guard: survives VWAP resets and bot restarts
        if self._index_vwap_triggered.get(self._active_index.name) == datetime.now().date():
            logger.debug(f"VWAP [{self._active_index.name}]: already triggered today — skipping")
            self._filter_blocks["VWAP_daily_limit"] = self._filter_blocks.get("VWAP_daily_limit", 0) + 1
            return
        try:
            vwap_signal = self.vwap_strat.analyze()
            if not vwap_signal:
                logger.debug(
                    f"VWAP [{self._active_index.name}]: analyze() returned no signal "
                    f"(state={self.vwap_strat._state.value}, "
                    f"long_fired={self.vwap_strat._long_fired}, "
                    f"short_fired={self.vwap_strat._short_fired})"
                )
                return

            # ── Minimum confidence gate ───────────────────────────────────────
            # With tightened thresholds the minimum achievable score is ~53%;
            # require ≥60% so that at least one of: volume spike OR a meaningful
            # RSI extreme must be present (not just barely crossing the threshold).
            if vwap_signal.strength < 60:
                logger.info(
                    f"VWAP [{self._active_index.display_name}]: Skipping — "
                    f"confidence {vwap_signal.strength:.0f}% below 60% minimum"
                    + (f" | India VIX={self._cached_vix:.1f}" if self._cached_vix > 0 else "")
                )
                await self._broadcast_message(
                    f"VWAP: Signal skipped — confidence {vwap_signal.strength:.0f}% "
                    f"below 60% threshold (need volume spike or deep RSI extreme)",
                    "alert"
                )
                self._filter_blocks["VWAP_strength"] = self._filter_blocks.get("VWAP_strength", 0) + 1
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
                    self._filter_blocks["VWAP_regime"] = self._filter_blocks.get("VWAP_regime", 0) + 1
                    return
                if regime_val == "TRENDING UP" and vwap_signal.direction == "SHORT":
                    logger.info(
                        "VWAP: Skipping SHORT (PE) — regime is TRENDING UP (counter-trend)"
                    )
                    self._filter_blocks["VWAP_regime"] = self._filter_blocks.get("VWAP_regime", 0) + 1
                    return

            # ── 5m trend direction guard (catches crash/rally in RANGING regime) ───
            # The regime guard above only fires for TRENDING regimes. On crash days
            # still classified RANGING (3-month ADX lags), the 5m signal is already
            # BEARISH — don't let VWAP mean-reversion buy CE into a falling market.
            if self._last_signal:
                _5m_trend = self._last_signal.trend.value
                if vwap_signal.direction == "LONG" and _5m_trend == "BEARISH":
                    logger.info(
                        f"VWAP: Skipping LONG (CE) — 5m signal is BEARISH (counter-trend, crash day)"
                    )
                    await self._broadcast_message(
                        "VWAP: LONG skipped — 5m trend is BEARISH (market falling, don't buy CE)",
                        "alert",
                    )
                    self._filter_blocks["VWAP_5m_trend"] = self._filter_blocks.get("VWAP_5m_trend", 0) + 1
                    return
                if vwap_signal.direction == "SHORT" and _5m_trend == "BULLISH":
                    logger.info(
                        f"VWAP: Skipping SHORT (PE) — 5m signal is BULLISH (counter-trend, rally day)"
                    )
                    await self._broadcast_message(
                        "VWAP: SHORT skipped — 5m trend is BULLISH (market rising, don't buy PE)",
                        "alert",
                    )
                    self._filter_blocks["VWAP_5m_trend"] = self._filter_blocks.get("VWAP_5m_trend", 0) + 1
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

            # ── RSI extreme block: VWAP ───────────────────────────────────────
            # Uses TRADING_PE_MIN_RSI_ENTRY / TRADING_CE_MAX_RSI_ENTRY for consistency.
            _vwap_rsi = vwap_signal.rsi
            _pe_rsi_floor_vwap = float(getattr(settings.trading, 'pe_min_rsi_entry', 35))
            _ce_rsi_ceil_vwap  = float(getattr(settings.trading, 'ce_max_rsi_entry', 70))
            if option_type == "PE" and _vwap_rsi < _pe_rsi_floor_vwap:
                msg = (f"VWAP [{self._active_index.name}]: Skipping PE — RSI {_vwap_rsi:.1f} < {_pe_rsi_floor_vwap:.0f} "
                       f"oversold, PE entry blocked")
                logger.info(msg)
                await self._broadcast_message(msg, "alert")
                self._filter_blocks["VWAP_RSI_extreme"] = self._filter_blocks.get("VWAP_RSI_extreme", 0) + 1
                return
            if option_type == "CE" and _vwap_rsi > _ce_rsi_ceil_vwap:
                msg = (f"VWAP [{self._active_index.name}]: Skipping CE — RSI {_vwap_rsi:.1f} > {_ce_rsi_ceil_vwap:.0f} "
                       f"overbought, CE entry blocked")
                logger.info(msg)
                await self._broadcast_message(msg, "alert")
                self._filter_blocks["VWAP_RSI_extreme"] = self._filter_blocks.get("VWAP_RSI_extreme", 0) + 1
                return

            # Paper mode: log signal but skip order placement
            if not self._auto_trade:
                # ── AI validation in paper mode ──────────────────────
                _paper_vwap_ai_ok = True
                if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                    try:
                        from bot.ai_gateway import get_ai_gateway
                        _gw_pv = get_ai_gateway()
                        _sig_pv = self._last_signal or self.analyzer.analyze()
                        if _sig_pv:
                            _gw_pv_ctx = {
                                'option_type':        option_type,
                                'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                                'vix':                self._cached_vix,
                                'consecutive_losses': 0,
                                'trades_done_today':  0,
                                'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                            }
                            _gw_pv_res = await _gw_pv.validate_strategy_signal(_sig_pv, 'VWAP', _gw_pv_ctx)
                            if not _gw_pv_res['approved']:
                                logger.info(f"PAPER VWAP [{self._active_index.name}]: AI blocked — {_gw_pv_res['reasoning']}")
                                await self._broadcast_message(
                                    f"🤖 PAPER VWAP: AI blocked {option_type} — {_gw_pv_res['reasoning']}",
                                    "paper_trade",
                                )
                                _paper_vwap_ai_ok = False
                            else:
                                logger.info(f"PAPER VWAP [{self._active_index.name}]: AI approved conf={_gw_pv_res['confidence']:.0f}%")
                    except Exception as _ai_pv_err:
                        logger.warning(f"PAPER VWAP: AI check failed ({_ai_pv_err}), proceeding")
                if _paper_vwap_ai_ok:
                    logger.info(
                        f"[SIGNAL] VWAP [{self._active_index.name}]: passed all filters → "
                        f"PAPER BUY {option_type} | dev={vwap_signal.deviation_pct:+.2f}% "
                        f"rsi={vwap_signal.rsi:.1f} strength={vwap_signal.strength:.0f}%"
                    )
                    await self._broadcast_message(
                        f"📋 PAPER VWAP: Would buy {option_type} | deviation {vwap_signal.deviation_pct:+.2f}% "
                        f"| price {vwap_signal.current_price:,.0f} VWAP {vwap_signal.vwap:,.2f} "
                        f"| RSI {vwap_signal.rsi:.1f} strength {vwap_signal.strength:.0f}% (paper mode)",
                        "paper_trade"
                    )
                    self.paper_trader.paper_buy(
                        index_name=self._active_index.name,
                        index_price=vwap_signal.current_price,
                        option_type=option_type,
                        strategy="VWAP",
                        quantity=self._active_index.lot_size,
                    )
                    self._index_vwap_triggered[self._active_index.name] = datetime.now().date()
                    self._save_daily_state()
                return

            # ── AI Gateway (PROMPT 1): live-trade validation only ─────────────
            if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                from bot.ai_gateway import get_ai_gateway
                _gw = get_ai_gateway()
                _ds  = self.order_manager._daily_stats if self.order_manager else None
                _gw_ctx = {
                    'option_type':        option_type,
                    'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                    'vix':                self._cached_vix,
                    'consecutive_losses': getattr(self.order_manager, '_consecutive_losses', 0) if self.order_manager else 0,
                    'trades_done_today':  getattr(_ds, 'total_trades', 0),
                    'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                }
                _gw_res = await _gw.validate_strategy_signal(self._last_signal, 'VWAP', _gw_ctx)
                if not _gw_res['approved']:
                    logger.info(f"🤖 VWAP SKIP (AI): {_gw_res['reasoning']}")
                    await self._broadcast_message(
                        f"🤖 AI SKIP [VWAP]: {_gw_res['reasoning']}\n"
                        f"Confidence: {_gw_res['confidence']:.0f}%",
                        "alert"
                    )
                    self._filter_blocks["VWAP_ai_gateway"] = self._filter_blocks.get("VWAP_ai_gateway", 0) + 1
                    return
                logger.info(
                    f"✅ VWAP AI-APPROVED: conf={_gw_res['confidence']:.0f}% "
                    f"action={_gw_res['action']} | {_gw_res['reasoning'][:80]}"
                )
                await self._broadcast_message(
                    f"✅ AI-VALIDATED [VWAP]: {_gw_res['reasoning']}\n"
                    f"Confidence: {_gw_res['confidence']:.0f}%",
                    "analysis"
                )
                # Boost signal strength when AI agrees
                _orig_strength = vwap_signal.strength
                vwap_signal.strength = _gw_res['boosted_strength']
                logger.info(
                    f"VWAP: strength boosted {_orig_strength:.0f}% → {vwap_signal.strength:.0f}%"
                )
                _ai_conf_for_tg = _gw_res['confidence']
            else:
                _ai_conf_for_tg = None

            order_ok = await self._execute_option_with_research(
                signal=self._last_signal,
                option_type=option_type,
                requested_strike="ATM",
                quantity=None,
                source="VWAP"
            )

            if order_ok:
                self._index_vwap_triggered[self._active_index.name] = datetime.now().date()
                self._save_daily_state()
                tg_msg = (
                    f"VWAP REVERSION {vwap_signal.direction}\n"
                    f"Entry: {vwap_signal.current_price:.2f}\n"
                    f"VWAP: {vwap_signal.vwap:.2f}  Dev: {vwap_signal.deviation_pct:+.2f}%\n"
                    f"SL: {vwap_signal.stop_loss:.2f}  Target: {vwap_signal.target:.2f}\n"
                    f"RSI: {vwap_signal.rsi:.1f}  Strength: {vwap_signal.strength:.0f}%"
                    + (f"\n🤖 AI Confidence: {_ai_conf_for_tg:.0f}%" if _ai_conf_for_tg is not None else "")
                )
                await self._send_telegram_alert(tg_msg, "trade_entry")

        except Exception as e:
            logger.error(f"VWAP signal check error: {e}")

    async def _check_eod_signal(self):
        """
        End-of-Day closing momentum: fires once between 14:30–15:00 IST.

        After 2:30 PM institutional traders begin squaring off, which amplifies
        the direction of the last completed 15-minute candle through the close.
        A strong (non-doji) candle → buy CE or PE in that direction.
        The position runs until the 15:27 IST force-exit auto-closes it.
        """
        cfg = settings.eod
        if not cfg.enabled:
            return

        # ── Paper-mode guard ─────────────────────────────────────────────────
        # EOD is 0/2 in paper data (both small losses, direction conflicted with
        # intraday momentum). Disable in paper mode until we have ≥10 live samples
        # to validate the pattern is reliable.
        if not self._auto_trade:
            logger.debug("EOD: skipped in paper mode — strategy not yet validated (<10 live samples)")
            return

        _today = datetime.now().date()
        _idx   = self._active_index.name

        # Once-per-day per-index guard (date-based — auto-resets on a new calendar day)
        if self._index_eod_triggered.get(_idx) == _today:
            return

        now = datetime.now()
        entry_start = now.replace(hour=cfg.entry_start_hour, minute=cfg.entry_start_minute, second=0, microsecond=0)
        entry_end   = now.replace(hour=cfg.entry_end_hour,   minute=cfg.entry_end_minute,   second=0, microsecond=0)
        if not (entry_start <= now <= entry_end):
            return

        if not self.order_manager:
            return

        can_trade, reason = self.order_manager.can_place_order()
        if not can_trade:
            logger.info(f"EOD [{self._active_index.display_name}]: blocked — {reason}")
            return

        # Both daily slots already consumed — no room for another trade
        _orb_done  = self._index_orb_triggered.get(_idx) == _today
        _vwap_done = self._index_vwap_triggered.get(_idx) == _today
        if _orb_done and _vwap_done:
            return

        try:
            import yfinance as yf
            import pandas as pd
            from zoneinfo import ZoneInfo
            _ist = ZoneInfo("Asia/Kolkata")

            ticker = yf.Ticker(self._active_index.yahoo_symbol)
            data   = ticker.history(period="1d", interval="15m")

            if data is None or data.empty or len(data) < 2:
                logger.debug(f"EOD [{self._active_index.display_name}]: insufficient 15m data")
                return

            # Convert index to IST so we can identify the last COMPLETED candle
            # precisely by timestamp, regardless of yfinance lag behaviour.
            # A 15m candle stamped at T is complete only after T + 15 min.
            # Using iloc[-2] is unreliable: when yfinance lags and omits the
            # currently-forming candle, iloc[-2] returns an extra candle too old.
            try:
                if hasattr(data.index, "tz") and data.index.tz:
                    data_ist = data.copy()
                    data_ist.index = data_ist.index.tz_convert(_ist)
                else:
                    data_ist = data.copy()
                    data_ist.index = data_ist.index.tz_localize(_ist)
                now_ist = datetime.now(_ist)
                completed = data_ist[
                    data_ist.index + pd.Timedelta(minutes=15) <= pd.Timestamp(now_ist)
                ]
                if completed.empty:
                    logger.debug(f"EOD [{self._active_index.display_name}]: no completed 15m candle yet")
                    return
                last = completed.iloc[-1]
                candle_ts = completed.index[-1]
                logger.debug(
                    f"EOD [{self._active_index.display_name}]: reading completed candle "
                    f"@ {candle_ts.strftime('%H:%M')} IST"
                )
            except Exception as _tz_err:
                logger.warning(f"EOD: IST candle filter failed ({_tz_err}) — falling back to iloc[-2]")
                last = data.iloc[-2]

            candle_open  = float(last["Open"])
            candle_close = float(last["Close"])
            candle_high  = float(last["High"])
            candle_low   = float(last["Low"])

            candle_range = candle_high - candle_low
            if candle_range < 1:
                return  # degenerate candle (data issue)

            body     = abs(candle_close - candle_open)
            body_pct = body / candle_range

            if body_pct < cfg.min_body_pct:
                logger.info(
                    f"EOD [{self._active_index.display_name}]: Skipping — "
                    f"candle body {body_pct:.0%} < {cfg.min_body_pct:.0%} (doji/indecision)"
                )
                await self._broadcast_message(
                    f"EOD: No signal — last 15m candle is indecision "
                    f"({body_pct:.0%} body, need ≥{cfg.min_body_pct:.0%}). Waiting for next candle.",
                    "alert"
                )
                # Don't mark triggered — the next 15m candle may be decisive
                return

            is_bullish  = candle_close > candle_open
            option_type = "CE" if is_bullish else "PE"
            dir_label   = "BULLISH ↑" if is_bullish else "BEARISH ↓"

            # Trend-direction alignment: skip if counter to the 5m overall trend
            if self._last_signal:
                trend = self._last_signal.trend.value
                if option_type == "CE" and trend == "BEARISH":
                    logger.info("EOD: CE skipped — overall 5m trend is BEARISH")
                    await self._broadcast_message(
                        "EOD: CE signal skipped — overall 5m trend is BEARISH", "alert"
                    )
                    self._index_eod_triggered[_idx] = _today  # don't retry
                    self._save_daily_state()
                    return
                if option_type == "PE" and trend == "BULLISH":
                    logger.info("EOD: PE skipped — overall 5m trend is BULLISH")
                    await self._broadcast_message(
                        "EOD: PE signal skipped — overall 5m trend is BULLISH", "alert"
                    )
                    self._index_eod_triggered[_idx] = _today
                    self._save_daily_state()
                    return

            report = (
                f"\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  EOD CLOSING MOMENTUM  —  {self._active_index.display_name}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  Last 15m candle  : {dir_label}\n"
                f"  Open → Close     : {candle_open:,.2f} → {candle_close:,.2f}\n"
                f"  High / Low       : {candle_high:,.2f} / {candle_low:,.2f}\n"
                f"  Body strength    : {body_pct:.0%} of range\n"
                f"  Signal           : BUY {option_type}\n"
                f"  Exit             : 15:27 IST force-exit\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            await self._broadcast_message(report, "analysis")

            if not self._last_signal:
                signal = self.analyzer.analyze()
                if not signal:
                    return
                self._last_signal = signal

            # Mark triggered BEFORE placing to prevent double-fire on concurrent cycles
            self._index_eod_triggered[_idx] = _today
            self._save_daily_state()
            if not self._auto_trade:
                logger.info(
                    f"PAPER EOD: Would buy {option_type} ATM — "
                    f"{dir_label} candle body {body_pct:.0%} "
                    f"({self._active_index.display_name}) — paper mode, no order placed"
                )
                await self._broadcast_message(
                    f"📋 PAPER EOD: Would buy {option_type} ATM | {dir_label} "
                    f"| body {body_pct:.0%} | open→close {candle_open:,.0f}→{candle_close:,.0f} "
                    f"({self._active_index.display_name}) — paper mode",
                    "paper_trade",
                )
                self.paper_trader.paper_buy(
                    index_name=self._active_index.name,
                    index_price=candle_close,
                    option_type=option_type,
                    strategy="EOD",
                    quantity=self._active_index.lot_size,
                )
                return

            # ── AI Gateway (PROMPT 1): live-trade validation only ─────────────
            if settings.ai.enabled and settings.ai.use_ai_signal_validation:
                from bot.ai_gateway import get_ai_gateway
                _gw = get_ai_gateway()
                _ds  = self.order_manager._daily_stats if self.order_manager else None
                _gw_ctx = {
                    'option_type':        option_type,
                    'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                    'vix':                self._cached_vix,
                    'consecutive_losses': getattr(self.order_manager, '_consecutive_losses', 0) if self.order_manager else 0,
                    'trades_done_today':  getattr(_ds, 'total_trades', 0),
                    'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
                }
                _gw_res = await _gw.validate_strategy_signal(self._last_signal, 'EOD', _gw_ctx)
                if not _gw_res['approved']:
                    logger.info(f"🤖 EOD SKIP (AI): {_gw_res['reasoning']}")
                    await self._broadcast_message(
                        f"🤖 AI SKIP [EOD]: {_gw_res['reasoning']}\n"
                        f"Confidence: {_gw_res['confidence']:.0f}%",
                        "alert"
                    )
                    self._filter_blocks["EOD_ai_gateway"] = self._filter_blocks.get("EOD_ai_gateway", 0) + 1
                    # Reset triggered so next cycle can retry
                    self._index_eod_triggered.pop(_idx, None)
                    return
                logger.info(
                    f"✅ EOD AI-APPROVED: conf={_gw_res['confidence']:.0f}% "
                    f"action={_gw_res['action']} | {_gw_res['reasoning'][:80]}"
                )
                await self._broadcast_message(
                    f"✅ AI-VALIDATED [EOD]: {_gw_res['reasoning']}\n"
                    f"Confidence: {_gw_res['confidence']:.0f}%",
                    "analysis"
                )
                _ai_conf_eod = _gw_res['confidence']
            else:
                _ai_conf_eod = None

            logger.warning(
                f"⚠️  EOD [{self._active_index.display_name}]: strategy is EXPERIMENTAL "
                f"(validated <10 days). Monitor results — disable if win rate < 55% over 20+ days."
            )
            order_ok = await self._execute_option_with_research(
                signal=self._last_signal,
                option_type=option_type,
                requested_strike="ATM",
                quantity=None,
                source="EOD",
            )

            if order_ok:
                # Consume the next available daily slot
                if not _orb_done:
                    self._index_orb_triggered[_idx] = _today
                else:
                    self._index_vwap_triggered[_idx] = _today
                await self._send_telegram_alert(
                    f"EOD Closing Momentum\n"
                    f"Last 15m: {dir_label}  (body {body_pct:.0%})\n"
                    f"Open→Close: {candle_open:,.0f}→{candle_close:,.0f}\n"
                    f"BUY {option_type} ATM  —  {self._active_index.display_name}\n"
                    f"Exit: 15:27 IST"
                    + (f"\n🤖 AI Confidence: {_ai_conf_eod:.0f}%" if _ai_conf_eod is not None else ""),
                    "trade_entry",
                )
            else:
                # Order failed — reset so the next cycle can retry
                self._index_eod_triggered.pop(_idx, None)

        except Exception as e:
            logger.error(f"EOD signal check error: {e}")

    async def _check_late_day_signal(self):
        """
        Late-Day Oscillation strategy: fires at 14:45, 15:00, 15:15 IST.

        Detects the 15-min oscillation direction and trades
        the mean-reversion (opposite direction).

        Risk profile: 0.25× size | 0.15% SL | 0.25% target | 12-min hold
        Validation: run analysis/late_day_oscillation_validator.py first.
        """
        cfg = settings.late_day
        if not cfg.enabled:
            return

        now = datetime.now()

        # Gather market context for filters
        market_data = {
            "vix": self._cached_vix,
            "intraday_range_pct": 0.0,
        }

        # Calculate intraday range from cached signal (if available)
        if self._last_signal:
            _sig = self._last_signal
            try:
                import yfinance as yf
                from zoneinfo import ZoneInfo
                _ist = ZoneInfo("Asia/Kolkata")
                _t = yf.Ticker(self._active_index.yahoo_symbol)
                _d = _t.history(period="1d", interval="5m")
                if _d is not None and not _d.empty:
                    day_high = float(_d["High"].max())
                    day_low  = float(_d["Low"].min())
                    ref_price = float(_d["Open"].iloc[0])
                    if ref_price > 0:
                        market_data["intraday_range_pct"] = (
                            (day_high - day_low) / ref_price * 100
                        )
            except Exception:
                pass  # non-critical — filter will be skipped gracefully

        if not self.late_day_strat.should_activate(now, market_data):
            return

        # ── Manage any existing position first ───────────────────────────────
        if self.late_day_strat.active_position is not None:
            # We need the option LTP to manage the position.
            # In paper mode use the reference price as a proxy (real mode would
            # query broker LTP).
            _option_proxy = (
                self._last_signal.current_price
                if self._last_signal else 0.0
            )
            if _option_proxy > 0:
                exit_action = self.late_day_strat.manage_position(now, _option_proxy)
                if exit_action == "EXIT":
                    pos = self.late_day_strat.active_position
                    if pos:
                        direction = pos.direction
                        # Paper mode: close via paper_trader
                        if not self._auto_trade:
                            await self._broadcast_message(
                                f"📋 PAPER LATE-DAY EXIT: Closing {direction} position "
                                f"(time/SL/target trigger) — paper mode",
                                "paper_trade",
                            )
                            # Mark position closed
                            self.late_day_strat.manage_position(now, _option_proxy)
                        else:
                            # Live mode: close existing position via order_manager
                            if self.order_manager:
                                await self.order_manager.close_all_positions()
                    return

        # ── Look for new entry signal ─────────────────────────────────────────
        try:
            import yfinance as yf
            from zoneinfo import ZoneInfo
            _ist = ZoneInfo("Asia/Kolkata")

            ticker = yf.Ticker(self._active_index.yahoo_symbol)
            candles = ticker.history(period="1d", interval="5m")

            if candles is None or candles.empty:
                return

            # Ensure IST-aware index
            if hasattr(candles.index, "tz") and candles.index.tz:
                candles.index = candles.index.tz_convert(_ist)
            else:
                candles.index = candles.index.tz_localize(_ist)

            # Only use candles from 14:30 onwards
            candles = candles[
                (candles.index.hour == 14) & (candles.index.minute >= 30) |
                (candles.index.hour == 15) & (candles.index.minute <= 25)
            ]

        except Exception as e:
            logger.debug(f"Late-day: candle fetch failed ({e})")
            return

        if candles.empty or len(candles) < 3:
            return

        current_price = (
            float(candles["Close"].iloc[-1])
            if not candles.empty
            else (self._last_signal.current_price if self._last_signal else 0.0)
        )
        if current_price <= 0:
            return

        signal = self.late_day_strat.detect_cycle_signal(now, current_price, candles)
        if signal is None:
            return

        # ── Signal generated — display report ────────────────────────────────
        report = (
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  LATE-DAY OSCILLATION  —  NIFTY\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  Cycle            : {signal.cycle}/3 "
            f"({['14:45', '15:00', '15:15'][min(signal.cycle - 1, 2)]} IST)\n"
            f"  Last 15m move    : {signal.last_move_direction}\n"
            f"  Expected reversal: {'UP → BUY CE' if signal.direction == 'CE' else 'DOWN → BUY PE'}\n"
            f"  Entry (index)    : ₹{current_price:,.2f}\n"
            f"  Target (option)  : +{signal.target_pct:.2f}% on LTP\n"
            f"  Stop (option)    : -{signal.stop_loss_pct:.2f}% on LTP\n"
            f"  Exit time        : {signal.exit_time} IST\n"
            f"  Confidence       : {signal.confidence}%\n"
            f"  Size             : {cfg.position_size_multiplier:.2f}× normal\n"
            f"  Reasoning        : {signal.reasoning}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await self._broadcast_message(report, "analysis")

        # ── Check global trade guards ─────────────────────────────────────────
        if self.order_manager:
            can_trade, reason = self.order_manager.can_place_order()
            if not can_trade:
                logger.info(f"Late-day [{signal.direction}]: blocked — {reason}")
                return

        # ── Paper mode ────────────────────────────────────────────────────────
        if not self._auto_trade:
            logger.info(
                f"[SIGNAL] LATE_DAY NIFTY: {signal.direction} ATM | "
                f"cycle {signal.cycle} | confidence {signal.confidence}% | paper mode"
            )
            await self._broadcast_message(
                f"📋 PAPER LATE-DAY: Would buy {signal.direction} ATM | "
                f"cycle {signal.cycle}/3 | confidence {signal.confidence}% | "
                f"index ₹{current_price:,.0f} | exit {signal.exit_time} — paper mode",
                "paper_trade",
            )
            # Register the paper position with a synthetic option LTP
            # (5% of index price is a rough ATM option proxy for paper mode)
            synthetic_ltp = round(current_price * 0.002, 2)
            self.late_day_strat.register_position(signal, synthetic_ltp)
            from bot.index_config import NIFTY as _NIFTY_CFG
            from math import floor as _floor
            # Apply position_size_multiplier so paper qty matches live qty exactly
            _paper_ld_qty = max(1, _floor(_NIFTY_CFG.lot_size * cfg.position_size_multiplier))
            self.paper_trader.paper_buy(
                index_name  = "NIFTY",
                index_price = current_price,
                option_type = signal.direction,
                strategy    = "LATE_DAY",
                quantity    = _paper_ld_qty,
            )
            return

        # ── Live mode ─────────────────────────────────────────────────────────
        if not self.order_manager:
            return

        from math import floor
        from bot.index_config import NIFTY as _NIFTY_CFG
        qty_multiplier = cfg.position_size_multiplier
        # Always derive from NIFTY lot_size — LATE_DAY is a NIFTY-only strategy.
        # Using settings.trading.default_quantity caused wrong qty when the user
        # ran 'set qty N' for other indices, or when the .env default differed.
        base_qty  = _NIFTY_CFG.lot_size
        trade_qty = max(1, floor(base_qty * qty_multiplier))

        if not self._last_signal:
            _s = self.analyzer.analyze()
            if not _s:
                return
            self._last_signal = _s

        # ── AI Gateway (PROMPT 1): live-trade validation only ─────────────
        _ai_conf_lateday = None
        if settings.ai.enabled and settings.ai.use_ai_signal_validation:
            from bot.ai_gateway import get_ai_gateway
            _gw = get_ai_gateway()
            _ds  = self.order_manager._daily_stats if self.order_manager else None
            _gw_ctx = {
                'option_type':        signal.direction,
                'regime':             (self._cached_regime.regime.value if self._cached_regime else 'UNKNOWN'),
                'vix':                self._cached_vix,
                'consecutive_losses': getattr(self.order_manager, '_consecutive_losses', 0) if self.order_manager else 0,
                'trades_done_today':  getattr(_ds, 'total_trades', 0),
                'max_trades_per_day': getattr(settings.trading, 'max_trades_per_day', 5),
            }
            _gw_res = await _gw.validate_strategy_signal(self._last_signal, 'LATE_DAY', _gw_ctx)
            if not _gw_res['approved']:
                logger.info(f"🤖 LATE_DAY SKIP (AI): {_gw_res['reasoning']}")
                await self._broadcast_message(
                    f"🤖 AI SKIP [LATE-DAY]: {_gw_res['reasoning']}\n"
                    f"Confidence: {_gw_res['confidence']:.0f}%",
                    "alert"
                )
                self._filter_blocks["LATEDAY_ai_gateway"] = self._filter_blocks.get("LATEDAY_ai_gateway", 0) + 1
                return
            logger.info(
                f"✅ LATE_DAY AI-APPROVED: conf={_gw_res['confidence']:.0f}% "
                f"action={_gw_res['action']} | {_gw_res['reasoning'][:80]}"
            )
            await self._broadcast_message(
                f"✅ AI-VALIDATED [LATE-DAY]: {_gw_res['reasoning']}\n"
                f"Confidence: {_gw_res['confidence']:.0f}%",
                "analysis"
            )
            _ai_conf_lateday = _gw_res['confidence']

        order_ok = await self._execute_option_with_research(
            signal         = self._last_signal,
            option_type    = signal.direction,
            requested_strike = "ATM",
            quantity       = trade_qty,
            source         = "LATE_DAY",
        )

        if order_ok:
            # Approximate option LTP from broker (use VWAP proxy if unavailable)
            _approx_ltp = round(current_price * 0.002, 2)
            self.late_day_strat.register_position(signal, _approx_ltp)
            await self._send_telegram_alert(
                f"⚡ Late-Day Oscillation\n"
                f"Cycle {signal.cycle}/3  |  NIFTY\n"
                f"Direction: {signal.direction}  ({signal.last_move_direction} fade)\n"
                f"Entry (index): {current_price:,.0f}\n"
                f"Confidence: {signal.confidence}%\n"
                f"Exit: {signal.exit_time} IST\n"
                f"Qty: {trade_qty}"
                + (f"\n🤖 AI Confidence: {_ai_conf_lateday:.0f}%" if _ai_conf_lateday is not None else ""),
                "trade_entry",
            )

    async def _build_chart_data(self, signal) -> dict:
        """Build chart_data dict for PROMPT 3 (pattern recognition).

        Fetches the last 10 five-minute candles for the active index via
        yfinance and assembles them with the technical indicator values that
        are already present in *signal* (a TrendSignal).  Used exclusively
        by ``_execute_auto_trade`` before calling
        ``LocalAIService.recognize_patterns()``.
        """
        import yfinance as yf

        candles: list = []
        pivot = r1 = s1 = 0.0

        try:
            ticker = yf.Ticker(self._active_index.yahoo_symbol)
            df = await asyncio.to_thread(
                lambda: ticker.history(period="1d", interval="5m")
            )
            if df is not None and not df.empty:
                last10 = df.tail(10).reset_index()
                for _, row in last10.iterrows():
                    ts_val = row.get("Datetime", row.get("Date", ""))
                    candles.append({
                        "timestamp": str(ts_val)[:16],
                        "open":   round(float(row["Open"]),  2),
                        "high":   round(float(row["High"]),  2),
                        "low":    round(float(row["Low"]),   2),
                        "close":  round(float(row["Close"]), 2),
                        "volume": int(row.get("Volume", 0)),
                    })
                # Classic pivot formula derived from today's full session range
                ph = float(df["High"].max())
                pl = float(df["Low"].min())
                pc = float(df["Close"].iloc[-1])
                pivot = (ph + pl + pc) / 3
                r1 = 2 * pivot - pl
                s1 = 2 * pivot - ph
        except Exception as exc:
            logger.debug(f"[_build_chart_data] yfinance fetch failed: {exc}")

        boll_upper = getattr(signal, "bollinger_upper", 0.0) or 0.0
        boll_mid   = getattr(signal, "bollinger_mid",   0.0) or 0.0
        boll_lower = getattr(signal, "bollinger_lower", 0.0) or 0.0

        return {
            "candles": candles,
            "rsi":     f"{signal.rsi:.1f}",
            "macd":    f"{signal.macd:.4f} / signal={signal.macd_signal:.4f}",
            "bollinger_bands": (
                f"upper={boll_upper:.0f} mid={boll_mid:.0f} lower={boll_lower:.0f}"
                if boll_upper > 0 else "N/A"
            ),
            "atr":    f"{getattr(signal, 'atr', 0.0) or 0.0:.1f}",
            "volume": candles[-1]["volume"] if candles else 0,
            "r1":    round(r1, 2),
            "pivot": round(pivot, 2),
            "s1":    round(s1, 2),
        }

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

        # ── Minimum signal strength filter ───────────────────────────────────
        # Paper data (33 trades): TIME_STOP exits (55% of all trades, avg +2.5%)
        # were concentrated in the 60–68% strength band.  Winning trades (avg
        # +28%) all had strength ≥ 70%.  Raising the floor eliminates dead-weight
        # entries that hold a slot without contributing meaningful P&L.
        _min_strength = float(getattr(settings.trading, 'min_signal_strength', 70.0))
        if signal.strength < _min_strength:
            msg = (
                f"Auto-trade blocked — signal strength {signal.strength:.0f}% < {_min_strength:.0f}% minimum "
                f"(paper data: sub-{_min_strength:.0f} signals mostly TIME_STOP exits, avg +2.5%)"
            )
            logger.info(msg)
            await self._broadcast_message(msg, "alert")
            self._filter_blocks["strength_floor"] = self._filter_blocks.get("strength_floor", 0) + 1
            return

        # ── Opening-noise time gate ─────────────────────────────────────────
        # Paper data: 9:15–9:50 AM entries included a SL_HIT at -100% and several
        # TIME_STOPs.  Best winners all entered after 10:00 AM once the opening
        # auction dust settled.  Block the first 45 minutes of the session.
        from zoneinfo import ZoneInfo as _ZI_at
        if datetime.now(_ZI_at("Asia/Kolkata")).hour < 10:
            msg = (
                f"Auto-trade blocked — before 10:00 IST "
                f"(opening noise filter; paper data: 9:15-9:50 entries had SL-HIT + TIME_STOPs)"
            )
            logger.info(msg)
            await self._broadcast_message(msg, "alert")
            self._filter_blocks["opening_noise"] = self._filter_blocks.get("opening_noise", 0) + 1
            return

        # ── RSI extreme-zone filter ───────────────────────────────────────────
        # RSI < 40 when buying PE = market already oversold → high bounce risk
        # RSI > 65 when buying CE = market already overbought → pullback risk
        # These are the most common cause of correct-direction but losing trades.
        _rsi_now = getattr(signal, 'rsi', 50)
        _pe_rsi_floor = float(getattr(settings.trading, 'pe_min_rsi_entry', 40))
        _ce_rsi_ceil  = float(getattr(settings.trading, 'ce_max_rsi_entry', 65))
        if option_type == "PE" and _rsi_now < _pe_rsi_floor:
            msg = (
                f"Auto PE blocked — RSI {_rsi_now:.1f} < {_pe_rsi_floor:.0f} "
                f"(market already oversold, high bounce risk)"
            )
            logger.info(msg)
            await self._broadcast_message(msg, "alert")
            self._filter_blocks["RSI_extreme"] = self._filter_blocks.get("RSI_extreme", 0) + 1
            return
        if option_type == "CE" and _rsi_now > _ce_rsi_ceil:
            msg = (
                f"Auto CE blocked — RSI {_rsi_now:.1f} > {_ce_rsi_ceil:.0f} "
                f"(market already overbought, pullback risk)"
            )
            logger.info(msg)
            await self._broadcast_message(msg, "alert")
            self._filter_blocks["RSI_extreme"] = self._filter_blocks.get("RSI_extreme", 0) + 1
            return

        # ── Regime-direction guard ────────────────────────────────────────────
        # Block CE entries on TRENDING DOWN days and PE on TRENDING UP days.
        # A "BULLISH" 5-min signal on a day-long bearish trend is often a short
        # bounce that gets sold — the regime tells us the bigger-picture context.
        _regime_val = (
            self._cached_regime.regime.value if self._cached_regime else "UNKNOWN"
        )
        if _regime_val == "TRENDING DOWN" and option_type == "CE":
            msg = (
                f"Auto CE blocked — regime is TRENDING DOWN "
                f"(price may be bouncing in a downtrend, not reversing)"
            )
            logger.info(msg)
            await self._broadcast_message(msg, "alert")
            self._filter_blocks["regime_direction"] = self._filter_blocks.get("regime_direction", 0) + 1
            return
        if _regime_val == "TRENDING UP" and option_type == "PE":
            msg = (
                f"Auto PE blocked — regime is TRENDING UP "
                f"(price may be pulling back in an uptrend, not reversing)"
            )
            logger.info(msg)
            await self._broadcast_message(msg, "alert")
            self._filter_blocks["regime_direction"] = self._filter_blocks.get("regime_direction", 0) + 1
            return

        # ── Local AI signal validation (PROMPT 1) ────────────────────────────
        # Calls the locally-hosted LLM (Ollama / LM Studio) to validate confluence,
        # time suitability, R:R, and loss-fatigue before placing the order.
        # If AI is disabled (AI_ENABLED=false) or unreachable, falls back to EXECUTE.
        try:
            from bot.local_ai_service import get_ai_service
            _ai = get_ai_service()
            if _ai.enabled and settings.ai.use_ai_signal_validation:
                _daily_stats = self.order_manager._daily_stats
                _market_ctx = {
                    "option_type":        option_type,
                    "regime":             (
                        self._cached_regime.regime.value
                        if self._cached_regime else "UNKNOWN"
                    ),
                    "vix":                self._cached_vix,
                    "consecutive_losses": getattr(self.order_manager, "_consecutive_losses", 0),
                    "trades_done_today":  getattr(_daily_stats, "total_trades", 0),
                    "max_trades_per_day": getattr(settings.trading, "max_trades_per_day", 5),
                    # Include macro sentiment from PROMPT 2 (advisory context)
                    "macro_sentiment":    (
                        self._ai_sentiment.market_sentiment
                        if self._ai_sentiment and not self._ai_sentiment.fallback
                        else "UNKNOWN"
                    ),
                    "macro_conditions":   (
                        self._ai_sentiment.trading_conditions
                        if self._ai_sentiment and not self._ai_sentiment.fallback
                        else "Unknown"
                    ),
                }
                _ai_result = await _ai.validate_signal(signal, _market_ctx)

                if _ai_result.fallback:
                    logger.info(
                        f"[LocalAI] Fallback — {_ai_result.reasoning}"
                    )
                elif _ai_result.suggested_action == "SKIP":
                    skip_msg = (
                        f"🤖 AI SKIP [{option_type}] — confidence={_ai_result.confidence:.0f}% "
                        f"confluence={_ai_result.confluence_count}/4 | {_ai_result.reasoning}"
                    )
                    logger.info(skip_msg)
                    await self._broadcast_message(skip_msg, "alert")
                    return
                elif _ai_result.suggested_action == "WAIT":
                    wait_msg = (
                        f"🤖 AI WAIT [{option_type}] — {_ai_result.reasoning}"
                    )
                    logger.info(wait_msg)
                    await self._broadcast_message(wait_msg, "alert")
                    return
                else:
                    # AI agrees — boost signal strength so downstream confidence
                    # thresholds are met more easily on high-quality setups.
                    _boost = getattr(settings.ai, "confidence_boost", 1.4)
                    _old_str = signal.strength
                    signal.strength = min(100.0, signal.strength * _boost)
                    logger.info(
                        f"[LocalAI] EXECUTE approved — confidence={_ai_result.confidence:.0f}% "
                        f"confluence={_ai_result.confluence_count}/4 "
                        f"sentiment={_ai_result.market_sentiment} | "
                        f"strength boosted {_old_str:.0f}% → {signal.strength:.0f}% (×{_boost})"
                    )
        except Exception as _ai_exc:
            logger.warning(f"[LocalAI] Validation skipped due to error: {_ai_exc}")

        # ── PROMPT 3: Chart Pattern Recognition ──────────────────────────────
        # Runs on every auto-trade entry attempt after PROMPT 1 approves.
        # AVOID result stops the trade; BUY_CE/BUY_PE must agree with option_type.
        try:
            from bot.local_ai_service import get_ai_service
            _ai3 = get_ai_service()
            if _ai3.enabled and settings.ai.use_ai_pattern_recognition:
                _chart_data = await self._build_chart_data(signal)
                _p3 = await _ai3.recognize_patterns(_chart_data)

                if not _p3.fallback:
                    _p3_msg = (
                        f"🔍 AI Patterns: {_p3.strongest_pattern or 'none'} | "
                        f"{_p3.overall_assessment} | {_p3.recommended_action} | "
                        f"{_p3.risk_assessment} | top conf={_p3.best_confidence:.0f}%"
                    )
                    logger.info(_p3_msg)
                    await self._broadcast_message(_p3_msg, "analysis")

                    if _p3.recommended_action == "AVOID":
                        skip_msg = (
                            f"🤖 AI Pattern AVOID — {_p3.risk_assessment} | "
                            f"strongest: {_p3.strongest_pattern}"
                        )
                        logger.info(skip_msg)
                        await self._broadcast_message(skip_msg, "alert")
                        return

                    # Direction mismatch: pattern says opposite side → skip
                    _agrees = (
                        (option_type == "CE" and _p3.recommended_action == "BUY_CE")
                        or (option_type == "PE" and _p3.recommended_action == "BUY_PE")
                        or _p3.recommended_action == "WAIT"   # WAIT = no opinion, let trade proceed
                    )
                    if not _agrees and _p3.best_confidence >= 60:
                        skip_msg = (
                            f"🤖 AI Pattern mismatch SKIP — pattern says {_p3.recommended_action} "
                            f"but signal wants {option_type} (conf={_p3.best_confidence:.0f}%)"
                        )
                        logger.info(skip_msg)
                        await self._broadcast_message(skip_msg, "alert")
                        return
        except Exception as _p3_exc:
            logger.warning(f"[LocalAI] Pattern recognition skipped due to error: {_p3_exc}")

        # ── PROMPT 4: Pre-Trade Risk Assessment ──────────────────────────────
        # Final gate before order placement.
        # REJECT / SKIP → abort;  REDUCE_SIZE → warn and proceed at normal sizing.
        try:
            from bot.local_ai_service import get_ai_service
            _ai4 = get_ai_service()
            if _ai4.enabled and settings.ai.use_ai_risk_assessment:
                _p4_stats   = self.order_manager._daily_stats
                _p4_acc_bal = float(
                    (self.position_sizer.account_balance if self.position_sizer else None)
                    or getattr(settings.trading, "account_balance", 100_000)
                )
                _p4_pnl      = self.order_manager._daily_pnl
                _p4_max_loss = float(getattr(settings.trading, "max_daily_loss", 6000.0))
                _p4_total_tr = _p4_stats.total_trades
                _p4_win_rate = (
                    (_p4_stats.winning_trades / _p4_total_tr * 100)
                    if _p4_total_tr > 0 else 0.0
                )
                # Rough ATM option-price proxy: 0.2% of index + ATR contribution
                _p4_atr       = getattr(signal, "atr", 0.0) or 0.0
                _p4_entry     = round(signal.current_price * 0.002 + _p4_atr * 0.01, 2)
                _p4_sl        = round(_p4_entry * 0.5, 2)
                _p4_tgt       = round(_p4_entry * 2.0, 2)
                # Count how many indicators agree with the trade direction
                _p4_bullish = (option_type == "CE")
                _p4_vwap    = getattr(signal, "vwap", 0.0) or 0.0
                _p4_confl   = sum([
                    (signal.rsi > 55) if _p4_bullish else (signal.rsi < 45),
                    signal.macd > signal.macd_signal,
                    (
                        (_p4_vwap > 0 and signal.current_price > _p4_vwap)
                        if _p4_bullish
                        else (_p4_vwap > 0 and signal.current_price < _p4_vwap)
                    ),
                    getattr(signal, "supertrend_direction", "NEUTRAL") == (
                        "BULLISH" if _p4_bullish else "BEARISH"
                    ),
                ])
                _p4_entry_ctx = {
                    "option_type":        option_type,
                    "direction":          signal.trend.value,
                    "strike":             0,          # ATM — not resolved yet
                    "entry_price":        _p4_entry,
                    "stop_loss":          _p4_sl,
                    "target":             _p4_tgt,
                    "quantity":           1,
                    "account_balance":    _p4_acc_bal,
                    "daily_pnl":          _p4_pnl,
                    "max_daily_loss":     _p4_max_loss,
                    "consecutive_losses": self.order_manager._consecutive_losses,
                    "today_win_rate":     _p4_win_rate,
                    "confluence_count":   _p4_confl,
                }
                _p4 = await _ai4.assess_trade_risk(signal, _p4_entry_ctx)

                if not _p4.fallback:
                    _p4_msg = (
                        f"🛡 AI Risk: {_p4.overall_assessment} | "
                        f"level={_p4.risk_level} | rec={_p4.recommendation} | "
                        f"size_adj=×{_p4.suggested_size_adjustment:.2f} | "
                        f"conf={_p4.confidence:.0f}%"
                    )
                    logger.info(_p4_msg)
                    await self._broadcast_message(_p4_msg, "analysis")

                    if _p4.overall_assessment == "REJECT" or _p4.recommendation == "SKIP":
                        reject_msg = (
                            f"🤖 AI Risk REJECT — {_p4.risk_level} | "
                            + (_p4.final_notes or "risk criteria not met")
                        )
                        logger.info(reject_msg)
                        await self._broadcast_message(reject_msg, "alert")
                        return

                    if _p4.recommendation == "REDUCE_SIZE":
                        reduce_msg = (
                            f"🤖 AI Risk REDUCE_SIZE ×{_p4.suggested_size_adjustment:.2f} — "
                            + (_p4.final_notes or "proceed with caution")
                        )
                        logger.info(reduce_msg)
                        await self._broadcast_message(reduce_msg, "alert")
        except Exception as _p4_exc:
            logger.warning(f"[LocalAI] Risk assessment skipped due to error: {_p4_exc}")

        # Execute with deep research
        # ── VIX-based quantity scaling ─────────────────────────────────────────────
        # At extreme VIX, option premiums are inflated and moves are violent.
        # Reduce size to limit capital at risk on any single trade.
        #   VIX > 25 → 1 lot only  (minimum exposure)
        #   VIX > 20 → halve lots  (half exposure)
        #   VIX ≤ 20 → full lots   (normal)
        _vix_now      = self._cached_vix
        _vix_high     = float(getattr(settings.trading, 'vix_high_threshold', 20.0))
        _vix_extreme  = float(getattr(settings.trading, 'vix_extreme_threshold', 25.0))
        _base_qty     = self.order_manager.get_active_quantity()  # = index lot_size
        _lot_size     = self._active_index.lot_size
        if _vix_now > _vix_extreme:
            _scaled_qty = _lot_size   # 1 lot regardless of how many lots base is
            logger.warning(
                f"VIX EXTREME {_vix_now:.1f} > {_vix_extreme:.0f} — "
                f"capping at 1 lot ({_scaled_qty} units)"
            )
            await self._broadcast_message(
                f"⚠️ VIX {_vix_now:.1f} (EXTREME) — position size capped at 1 lot ({_scaled_qty} units)",
                "alert"
            )
        elif _vix_now > _vix_high:
            _lots        = max(1, _base_qty // _lot_size)          # how many lots normally
            _halved_lots = max(1, _lots // 2)                      # halved, minimum 1
            _scaled_qty  = _halved_lots * _lot_size
            logger.info(
                f"VIX HIGH {_vix_now:.1f} > {_vix_high:.0f} — "
                f"halving from {_base_qty} → {_scaled_qty} units ({_halved_lots} lot(s))"
            )
            await self._broadcast_message(
                f"⚠️ VIX {_vix_now:.1f} (HIGH) — quantity halved: {_base_qty} → {_scaled_qty} units",
                "alert"
            )
        else:
            _scaled_qty = None   # pass None = use default (full lot)

        await self._execute_option_with_research(
            signal=signal,
            option_type=option_type,
            requested_strike="ATM",
            quantity=_scaled_qty,
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
                return "Screenshot is not supported with the current broker (Dhan)"
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

            elif action == "lateday" or action == "late_day" or action == "ld":
                return self._handle_lateday_command(parts)

            elif action in ("perf", "performance", "optimize", "optimise", "stats"):
                return self._handle_perf_command(parts)

            elif action == "index" or action == "switch" or action == "filter":
                return await self._handle_index_command(parts)

            elif action == "iv":
                return self._handle_iv_command(parts)

            elif action == "validate":
                return await self._handle_validate_command(parts)
            
            elif action == "forceclose":
                return await self._handle_force_close(parts)

            else:
                return f"Unknown command: {action}. Type 'help' for available commands."
                
        except AttributeError as e:
            # Specific case: a _handle_* method is missing (usually dead-code bug).
            # Give a clear actionable message and alert via Telegram.
            msg = str(e)
            logger.error(f"Command handler missing: {msg}")
            if "_handle_" in msg:
                handler_name = msg.split("'")[-2] if "'" in msg else msg
                alert = (
                    f"⚠️ BOT BUG: missing command handler\n"
                    f"Command: '{command}'\n"
                    f"Error: {msg}\n"
                    f"Fix: define or restore `{handler_name}` in engine.py"
                )
                import asyncio
                asyncio.create_task(self._send_telegram_alert(alert, "error"))
            return f"⚠️ Command handler missing: {msg}. A Telegram alert has been sent."
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
    
    async def _handle_force_close(self, parts: List[str]) -> str:
        """
        Force-mark one or all positions as CLOSED in bot memory without placing
        a broker SELL order. Use when the broker lookup is broken (e.g. wrong
        index after a switch) and you've already closed the position manually
        on the broker app.

        Usage:
          forceclose all            - mark all open positions as closed
          forceclose <trade_id>     - mark a specific trade as closed
          forceclose <symbol>       - e.g.  forceclose NIFTY22550PE
        """
        if not self.order_manager:
            return "Order manager not initialized"

        positions = {
            tid: t for tid, t in self.order_manager._positions.items()
            if t.status == "OPEN"
        }
        if not positions:
            return "No open positions to force-close."

        target_arg = parts[1].upper() if len(parts) > 1 else "all"
        closed = []

        for tid, trade in list(positions.items()):
            if target_arg == "ALL" or tid.upper() == target_arg or target_arg in trade.symbol.upper():
                trade.status = "CLOSED"
                # Best-effort P&L: use last known price if available
                try:
                    ltp = await self.kite.get_instrument_price(trade.symbol) or 0.0
                except Exception:
                    ltp = 0.0
                if ltp > 0 and trade.price > 0:
                    pnl = (ltp - trade.price) * trade.quantity
                    trade.pnl = (trade.pnl or 0.0) + pnl
                    self.order_manager._daily_pnl += pnl
                    self.order_manager._save_daily_pnl()
                closed.append(f"{tid} ({trade.symbol})")
                logger.warning(
                    f"[forceclose] {tid} {trade.symbol} marked CLOSED in bot memory "
                    f"(broker order NOT placed — close manually on broker app if not done)"
                )

        if not closed:
            return f"No open position matched '{target_arg}'. Open positions: {', '.join(positions.keys())}"

        msg = f"Force-closed {len(closed)} position(s) in bot memory: {', '.join(closed)}\n⚠️ Verify these are also closed on your broker app!"
        await self._broadcast_message(msg, "alert")
        await self._send_telegram_alert(msg, "trade_error")
        return msg

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
            rsi=signal.rsi,
            strike_interval=self._active_index.strike_interval,
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
            # Clear cache for the active index so we get a fresh read
            self._gap_detector._cached_by_index.pop(self._active_index.name, None)
            analysis = self._gap_detector.analyze()
            if not analysis:
                return "Gap analysis unavailable — market may not be open yet or data fetch failed."
            return self._gap_detector.format_report(analysis)
        except Exception as e:
            return f"Gap analysis error: {e}"

    def _handle_lateday_command(self, parts: List[str]) -> str:
        """
        Handle 'lateday' (also: late_day / ld) command.

        Subcommands:
          lateday            — show full status
          lateday on         — enable strategy (validates safety limits)
          lateday off        — disable strategy
          lateday skip       — skip the next cycle checkpoint
          lateday history    — show today's trade stats
          lateday validate   — remind user to run the validator script
        """
        sub = parts[1].lower() if len(parts) > 1 else "status"

        if sub in ("status", ""):
            return self.late_day_strat.format_status()

        if sub in ("on", "enable"):
            try:
                settings.late_day.enabled = True
                settings.late_day.validate_config()
                return (
                    "⚡ Late-Day Oscillation ENABLED.\n"
                    "  Active window : 14:30–15:25 IST\n"
                    "  Checkpoints   : 14:45 / 15:00 / 15:15\n"
                    "  Force exit    : 15:25 IST\n"
                    "  Position size : 0.25× normal\n\n"
                    "⚠️  WARNING: Only trade after running the validator:\n"
                    "  python analysis/late_day_oscillation_validator.py"
                )
            except ValueError as e:
                settings.late_day.enabled = False
                return f"⚠️ Cannot enable: {e}"

        if sub in ("off", "disable"):
            settings.late_day.enabled = False
            return "Late-Day Oscillation DISABLED."

        if sub == "skip":
            skipped = self.late_day_strat.skip_next_cycle()
            if skipped:
                return f"Skipped late-day cycle at {skipped}."
            return "No upcoming cycle to skip (all done for today, or outside the 14:45–15:15 window)."

        if sub == "history":
            s = self.late_day_strat
            done = s.today_wins + s.today_losses
            wr   = f"{s.today_wins / done * 100:.0f}%" if done else "N/A"
            return (
                f"Late-Day Oscillation — Today's History\n"
                f"  Cycles attempted : {s.today_cycles}\n"
                f"  Wins / Losses    : {s.today_wins} / {s.today_losses}\n"
                f"  Win rate         : {wr}\n"
                f"  Consec. losses   : {s.consecutive_losses}\n"
                f"  Approx. P&L %    : {s.today_pnl:+.2f}%"
            )

        if sub == "validate":
            return (
                "Run the validation script BEFORE enabling this strategy:\n\n"
                "  cd 'D:\\Users\\sundlnu\\VS Code BOT\\BOT'\n"
                "  $env:PYTHONUTF8='1'\n"
                "  .venv\\Scripts\\python analysis\\late_day_oscillation_validator.py\n\n"
                "Enable only if:\n"
                "  • Pattern frequency ≥ 60%\n"
                "  • p-value < 0.05 (statistically significant)\n"
                "  • Win rate ≥ 52% after slippage\n"
            )

        return (
            "Usage:\n"
            "  lateday            — show status\n"
            "  lateday on/off     — enable/disable\n"
            "  lateday skip       — skip next cycle checkpoint\n"
            "  lateday history    — today's trade stats\n"
            "  lateday validate   — show validator instructions"
        )

    def _handle_perf_command(self, parts: List[str]) -> str:
        """
        Handle 'perf' / 'performance' / 'stats' / 'optimize' command.

        Sub-commands:
          perf              — full performance report
          perf reload       — reload journals from disk
          perf strategy     — per-strategy breakdown only
          perf recs         — recommendations only
          perf sizer        — show current position sizer settings
        """
        sub = parts[1].lower() if len(parts) > 1 else "report"

        if sub == "reload":
            n = self.perf_optimizer.load_journals()
            # Update Kelly data in position sizer
            if self.position_sizer:
                self.position_sizer.update_stats(
                    self.perf_optimizer.get_strategy_stats()
                )
            return (
                f"Reloaded {n} trades from journals/.\n"
                f"Total trades in memory: {self.perf_optimizer.total_trades()}"
            )

        if sub in ("strategy", "strategies", "by_strategy"):
            strat_analysis = self.perf_optimizer.analyze_by_strategy()
            if not strat_analysis:
                return "No trade history loaded. Run 'perf reload' first."
            lines = ["━━ Per-Strategy Performance ━━━━━━━━━━━━━━━━━━━"]
            for strat, s in strat_analysis.items():
                lines.append(
                    f"  {strat:<20} WR={s['win_rate']:.0f}%  "
                    f"PF={s['profit_factor']:.2f}  n={s['total_trades']}"
                )
                lines.append(f"    → {s['recommendation']}")
            return "\n".join(lines)

        if sub in ("recs", "recommendations", "suggest"):
            recs = self.perf_optimizer.generate_recommendations()
            if not recs:
                return "No recommendations yet (need ≥ 10 trades per strategy)."
            lines = ["━━ Recommendations ━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
            for r in recs[:8]:
                lines.append(f"  [{r['priority']}] {r['action']}: {r['item']}")
                lines.append(f"       {r['reason']}")
                lines.append(f"       {r['impact']}")
            return "\n".join(lines)

        if sub == "sizer":
            ps_cfg = settings.position_sizer
            filter_cfg = settings.entry_filter
            balance = (
                self.position_sizer.account_balance
                if self.position_sizer
                else getattr(settings.trading, "account_balance", 0)
            )
            return (
                "━━ Position Sizer / Filter Settings ━━━━━━━━━━━━━\n"
                f"  Sizer enabled       : {ps_cfg.enabled}\n"
                f"  Max risk per trade  : {ps_cfg.max_risk_per_trade_pct:.1f}%\n"
                f"  Kelly fraction      : {ps_cfg.kelly_fraction:.2f}\n"
                f"  Min Kelly trades    : {ps_cfg.min_kelly_trades}\n"
                f"  Max qty multiplier  : {ps_cfg.max_qty_multiplier}×\n"
                f"\n"
                f"  Filter enabled      : {filter_cfg.enabled}\n"
                f"  Min score required  : {filter_cfg.min_confidence_score}/100\n"
                f"\n"
                f"  Account balance     : ₹{balance:,.0f}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

        # Default: full report
        return self.perf_optimizer.generate_report()

    def _handle_iv_command(self, parts: List[str]) -> str:
        """
        Handle 'iv' chat commands:
          iv status       — show IV history days per symbol
          iv backfill     — seed .iv_history.json from 30 days of India VIX data
          iv backfill 60  — backfill N days
        """
        sub = parts[1].lower() if len(parts) > 1 else "status"

        if sub == "status":
            hist = iv_monitor._iv_history
            if not hist:
                return "IV history: empty. Run 'iv backfill' to seed with VIX history."
            lines = ["📊 IV History Status"]
            for sym, entries in sorted(hist.items()):
                days = len(entries)
                latest = max(entries.keys()) if entries else "—"
                lines.append(f"  {sym}: {days} days (latest: {latest})")
            wr = _eod_tracker.win_rate()
            n = _eod_tracker.count()
            eod_line = (
                f"{wr * 100:.1f}% over {n} trades"
                if wr >= 0
                else f"{n} trades (need {_eod_tracker._MIN_TRADES}+ for reliable stats)"
            )
            lines.append(f"\n📈 EOD win rate: {eod_line}")
            return "\n".join(lines)

        if sub == "backfill":
            days = 30
            if len(parts) > 2:
                try:
                    days = int(parts[2])
                except ValueError:
                    pass
            written = iv_monitor.backfill_from_vix(days=days)
            if written > 0:
                return (
                    f"✅ IV backfill complete: {written} new date-entries written from India VIX.\n"
                    f"You can now enable the IV filter: TRADING_IV_FILTER_ENABLED=true"
                )
            return "IV backfill: no new entries needed (history already complete)."

        return "Usage: iv status | iv backfill [days=30]"

    async def _handle_validate_command(self, parts: List[str]) -> str:
        """
        Handle 'validate [index]' command.
        Runs the strategy validator and returns a summary.
        Example: validate      → validate NIFTY
                 validate BANKNIFTY
        """
        idx_name = parts[1].upper() if len(parts) > 1 else self._active_index.name
        idx = get_index(idx_name)
        if not idx:
            return f"Unknown index '{idx_name}'. Use: validate NIFTY | BANKNIFTY | SENSEX"

        await self._broadcast_message(
            f"🔬 Running strategy validator for {idx.display_name} — this takes ~10 seconds...",
            "system",
        )
        try:
            # Run in executor so it doesn't block the event loop
            import asyncio
            from bot.strategy_validator import StrategyValidator
            loop = asyncio.get_event_loop()
            report = await loop.run_in_executor(
                None,
                lambda: StrategyValidator(index=idx).run_all()
            )
            return report.summary()
        except Exception as e:
            logger.error(f"validate command error: {e}")
            return f"Strategy validation failed: {e}"

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
        self._user_selected_index = idx   # survives analysis-loop context swaps
        # Switch to the pre-created per-index instances (keeps each index's state intact)
        self.analyzer   = self._index_analyzers[idx.name]
        self.orb        = self._index_orbs[idx.name]
        self.vwap_strat = self._index_vwaps[idx.name]
        self.order_manager.set_active_index(idx)
        self.kite.set_active_index(idx)  # propagate to broker so place_order uses correct index

        # Update advanced modules with the new index
        backtester.set_index(idx)
        regime_detector.set_index(idx)
        mtf_engine.set_index(idx)
        theta_clock.set_index(idx)
        self._gap_detector.set_index(idx)

        self._last_signal = self._index_last_signal.get(idx.name)
        self._cached_regime = self._index_regime_cache.get(idx.name)
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
