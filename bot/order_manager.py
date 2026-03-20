"""
Order Manager Module

Handles order placement logic, position tracking, and risk management.
"""

import asyncio
import json
import os
import re
from typing import Optional, Dict, List
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from loguru import logger

import sys
sys.path.append('..')
from config import settings
from browser.dhan import OrderType, OptionType, OrderResult
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from browser.zerodha import ZerodhaKite
from bot.trend_analyzer import TrendSignal, Trend
from bot.index_config import IndexConfig, NIFTY, get_index


@dataclass
class TradeRecord:
    """Record of a completed trade"""
    trade_id: str
    symbol: str
    option_type: OptionType
    strike: int
    order_type: OrderType
    quantity: int
    price: float
    timestamp: datetime
    pnl: Optional[float] = None
    status: str = "OPEN"
    highest_price: float = 0.0   # tracks peak for trailing-stop
    tier1_exited: bool = False   # True after tier-1 partial exit fires
    tier2_exited: bool = False   # True after tier-2 partial exit fires — triggers tighter trail
    gtt_id: Optional[int] = None  # Kite GTT stop-loss order ID (exchange-level safety net)
    lot_size: int = 1               # index lot size at entry — used for partial-exit calc
    source: str = "Manual"          # GAP | ORB | VWAP | Auto | EOD | Rule | Manual
    vix_at_entry: float = 0.0       # India VIX at time of entry (0 = not captured / manual trade)
    slippage: float = 0.0           # fill_price − pre_order_ltp in ₹; positive = paid more than expected
    # Greeks & IV at entry — populated by iv_monitor after each successful entry
    iv_pct_at_entry: float = 0.0        # ATM IV % at entry (e.g. 14.5)
    iv_percentile_at_entry: float = 0.0 # IV percentile rank vs last 30 days (0–100; -1 = unknown)
    delta_at_entry: float = 0.0         # Option delta (CE: 0–1, PE: -1–0)
    theta_daily_at_entry: float = 0.0   # Daily theta in ₹ per unit at entry (negative)
    vega_at_entry: float = 0.0          # Vega: ₹ change per 1% IV move per unit


@dataclass
class DailyStats:
    """Daily trading statistics"""
    date: date
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    realized_pnl: float = 0.0


class OrderManager:
    """
    Manages order placement and risk management.
    
    Features:
    - Position tracking
    - Stop-loss and target management
    - Daily loss limits
    - Trade history
    """
    
    def __init__(self, kite):
        self.kite = kite
        self.config = settings.trading
        
        # Track current index for lot size calculation
        self._active_index: IndexConfig = NIFTY
        
        # Active positions and trades
        self._positions: Dict[str, TradeRecord] = {}
        self._trade_history: List[TradeRecord] = []
        
        # Daily tracking
        self._daily_stats = DailyStats(date=date.today())
        self._daily_pnl: float = 0.0
        
        # Trade counter for IDs
        self._trade_counter = 0

        # Safety guardrails (consecutive losses / cool-off / trade frequency)
        self._consecutive_losses: int = 0
        self._trading_paused_until: Optional[datetime] = None
        self._last_trade_time: Optional[datetime] = None

        # Per-strike loss counter: (option_type, strike) -> loss count today
        # Prevents re-entering the same losing strike more than MAX_STRIKE_LOSSES times
        self._strike_loss_count: Dict[tuple, int] = {}
        MAX_STRIKE_LOSSES = 2  # configurable; stored as class-level default
        self._max_strike_losses: int = MAX_STRIKE_LOSSES

        # Net qty tracker for over-sell guard: (option_type, strike) -> net qty held
        self._held_qty: Dict[tuple, int] = {}

        # Persist daily P&L to disk so restarts don't reset the daily-loss limit.
        # File: <workspace>/.daily_pnl.json  →  {"date": "YYYY-MM-DD", "pnl": -1053.0}
        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._pnl_file = os.path.join(_base, ".daily_pnl.json")
        self._daily_pnl = self._load_daily_pnl()
    
    def set_active_index(self, index_config: IndexConfig):
        """Set the active trading index (NIFTY, BANKNIFTY, SENSEX)"""
        self._active_index = index_config
        logger.info(f"📊 OrderManager switched to {index_config.display_name} (lot_size: {index_config.lot_size})")
    
    def get_active_quantity(self) -> int:
        """Get the trading quantity for the current index (uses lot size)"""
        # Use the index's lot_size as the default quantity
        return self._active_index.lot_size
        
    def _generate_trade_id(self) -> str:
        """Generate unique trade ID"""
        self._trade_counter += 1
        return f"TRD_{datetime.now().strftime('%Y%m%d')}_{self._trade_counter:04d}"
    
    def _load_daily_pnl(self) -> float:
        """Load today's realized P&L from disk (survives bot restarts)."""
        try:
            if os.path.exists(self._pnl_file):
                with open(self._pnl_file, "r") as f:
                    data = json.load(f)
                if data.get("date") == date.today().isoformat():
                    pnl = float(data.get("pnl", 0.0))
                    if pnl != 0.0:
                        logger.info(f"Loaded persisted daily P&L: ₹{pnl:,.2f}")
                    return pnl
        except Exception as e:
            logger.debug(f"Could not load daily P&L file: {e}")
        return 0.0

    def _save_daily_pnl(self):
        """Persist today's realized P&L to disk."""
        try:
            with open(self._pnl_file, "w") as f:
                json.dump({"date": date.today().isoformat(), "pnl": self._daily_pnl}, f)
        except Exception as e:
            logger.debug(f"Could not save daily P&L file: {e}")

    def _reset_daily_stats_if_needed(self):
        """Reset daily stats if it's a new day"""
        today = date.today()
        if self._daily_stats.date != today:
            logger.info(f"New trading day - resetting daily stats")
            self._daily_stats = DailyStats(date=today)
            self._daily_pnl = 0.0
            self._save_daily_pnl()  # write 0.0 for the new day
            self._consecutive_losses = 0
            self._trading_paused_until = None
            self._strike_loss_count = {}
            self._held_qty = {}
    
    def can_place_order(self) -> tuple[bool, str]:
        """
        Check if we can place a new order based on risk rules.

        Returns:
            Tuple of (can_trade, reason)
        """
        self._reset_daily_stats_if_needed()

        # ── Market hours / AMO window ─────────────────────────────────────────
        if not self._is_market_hours():
            # Allow order if AMO is enabled and we're in the AMO window
            if self.config.amo_enabled and self._is_amo_window():
                # AMO: skip per-day limits that don't apply pre-market
                logger.debug("can_place_order: GATE[market_hours] PASS via AMO window")
                return True, "AMO window"
            logger.debug("can_place_order: GATE[market_hours] BLOCK — outside 9:15–15:30 IST and AMO not applicable")
            return False, "Outside market hours"

        # ── Consecutive-loss cool-off ─────────────────────────────────────────
        if self._trading_paused_until and datetime.now() < self._trading_paused_until:
            mins_left = int((self._trading_paused_until - datetime.now()).total_seconds() / 60) + 1
            reason = (
                f"Trading paused after {self._consecutive_losses} consecutive losses. "
                f"Resumes in {mins_left} min."
            )
            logger.warning(f"can_place_order: GATE[cool_off] BLOCK — {reason}")
            return False, reason

        # ── Daily trade limit ────────────────────────────────────────────────
        if self._daily_stats.total_trades >= self.config.max_trades_per_day:
            reason = f"Daily trade limit reached ({self.config.max_trades_per_day} trades)"
            logger.warning(f"can_place_order: GATE[daily_limit] BLOCK — {reason}")
            return False, reason

        # ── Minimum time between trades ──────────────────────────────────────
        # Skip cooldown when adding a hedge leg (already have >=1 open position);
        # only enforce for fresh entries when all positions are flat.
        active_positions = len([p for p in self._positions.values() if p.status == "OPEN"])
        if self._last_trade_time and active_positions == 0:
            elapsed_min = (datetime.now() - self._last_trade_time).total_seconds() / 60
            if elapsed_min < self.config.min_time_between_trades_minutes:
                reason = (
                    f"Too soon after last trade ({elapsed_min:.1f} min elapsed, "
                    f"min {self.config.min_time_between_trades_minutes} min required)"
                )
                logger.debug(f"can_place_order: GATE[min_gap] BLOCK — {reason}")
                return False, reason

        # ── Max concurrent positions ───────────────────────────────────────────
        if active_positions >= self.config.max_positions:
            reason = f"Max positions reached ({self.config.max_positions})"
            logger.warning(f"can_place_order: GATE[max_positions] BLOCK — {reason}")
            return False, reason

        # ── Daily loss limit ─────────────────────────────────────────────────
        if self._daily_pnl <= -self.config.max_daily_loss:
            reason = f"Daily loss limit reached (₹{self.config.max_daily_loss:,.0f})"
            logger.warning(f"can_place_order: GATE[daily_loss] BLOCK — daily_pnl=₹{self._daily_pnl:,.0f} limit=₹{self.config.max_daily_loss:,.0f}")
            return False, reason

        logger.debug(
            f"can_place_order: ALL GATES PASS — "
            f"trades={self._daily_stats.total_trades}/{self.config.max_trades_per_day} "
            f"positions={active_positions}/{self.config.max_positions} "
            f"daily_pnl=₹{self._daily_pnl:,.0f}"
        )
        return True, "OK"

    def can_enter_strike(self, option_type: str, strike: int) -> tuple[bool, str]:
        """
        Check if we're allowed to re-enter a specific strike today.
        Blocks entry if we've already taken _max_strike_losses losses on it.
        """
        key = (option_type.upper(), strike)
        losses = self._strike_loss_count.get(key, 0)
        if losses >= self._max_strike_losses:
            return False, (
                f"⛔ {option_type.upper()} {strike}: already lost {losses}x today on this strike "
                f"(max {self._max_strike_losses}). Choose a different strike."
            )
        return True, "OK"

    def can_sell_qty(self, option_type: str, strike: int, qty: int) -> tuple[bool, str]:
        """
        Over-sell guard: ensure a SELL does not exceed the qty we hold for this strike.
        """
        key = (option_type.upper(), strike)
        held = self._held_qty.get(key, 0)
        if held <= 0:
            return False, (
                f"⛔ No open position in {option_type.upper()} {strike} to sell. "
                f"Rejecting to prevent naked short."
            )
        if qty > held:
            return False, (
                f"⛔ Sell qty {qty} > held qty {held} for {option_type.upper()} {strike}. "
                f"Capped at {held} to prevent naked short. Use qty {held}."
            )
        return True, "OK"
    
    def _is_market_hours(self) -> bool:
        """Check if current time is within market hours (9:15–15:30 weekdays)"""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now.weekday() > 4:
            return False
        market_open = now.replace(
            hour=self.config.market_open_hour,
            minute=self.config.market_open_minute,
            second=0
        )
        market_close = now.replace(
            hour=self.config.market_close_hour,
            minute=self.config.market_close_minute,
            second=0
        )
        return market_open <= now <= market_close

    @staticmethod
    def _is_amo_window() -> bool:
        """Dhan AMO window: weekdays 17:00–23:59 and 00:00–09:08"""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now.weekday() > 4:
            return False
        mins = now.hour * 60 + now.minute
        return mins >= 17 * 60 or mins < 9 * 60 + 8
    
    async def execute_trade_signal(
        self,
        signal: TrendSignal,
        strike: int = None,
        quantity: int = None
    ) -> Optional[OrderResult]:
        """
        Execute a trade based on trend signal.
        
        Args:
            signal: TrendSignal from analyzer
            strike: Override strike price (uses suggestion if None)
            quantity: Override quantity (uses default if None)
            
        Returns:
            OrderResult or None if trade not executed
        """
        # Check if we can trade
        can_trade, reason = self.can_place_order()
        if not can_trade:
            logger.warning(f"Cannot place order: {reason}")
            return None
        
        # Determine option type from signal
        if signal.trend == Trend.BULLISH:
            option_type = OptionType.CE
        elif signal.trend == Trend.BEARISH:
            option_type = OptionType.PE
        else:
            logger.info("Neutral trend - no trade executed")
            return None
        
        # Get strike price using index-appropriate interval (50 for NIFTY, 100 for BANKNIFTY/SENSEX)
        if strike is None:
            strike_interval = self._active_index.strike_interval
            strike = round(signal.current_price / strike_interval) * strike_interval
        
        # Strike-loss guard: block re-entry if already lost on this strike today
        can_enter, enter_reason = self.can_enter_strike(option_type.value, strike)
        if not can_enter:
            logger.warning(enter_reason)
            return None
        
        quantity = quantity or self.get_active_quantity()
        
        logger.info(f"Executing signal: {option_type.value} {strike} x {quantity}")
        
        # Place order via broker
        result = await self.kite.place_order(
            option_type=option_type,
            strike=strike,
            order_type=OrderType.BUY,
            quantity=quantity
        )
        
        if result.success:
            option_symbol = f"{self._active_index.name}{strike}{option_type.value}"

            # Wait for the order to settle then fetch the real fill price from
            # the broker's positions API — same approach as manual_order().
            # A stale or zero LTP at this point would leave trade.price=0, causing
            # check_stop_loss_targets to skip ALL SL/target monitoring for the trade.
            await asyncio.sleep(2)
            option_ltp = 0.0
            try:
                pos_data = await self.kite.get_kite_positions()
                for p in pos_data.get("day", []):
                    sym = p.get("tradingsymbol", "")
                    if (str(strike) in sym and option_type.value in sym
                            and (p.get("quantity") or 0) != 0):
                        avg = float(p.get("average_price") or 0)
                        if avg > 0:
                            option_ltp = avg
                            break
            except Exception as e:
                logger.debug(f"execute_trade_signal: broker fill fetch failed: {e}")

            if option_ltp == 0.0:
                try:
                    option_ltp = await self.kite.get_instrument_price(option_symbol) or 0.0
                except Exception:
                    pass

            if option_ltp == 0.0:
                logger.warning(
                    f"execute_trade_signal: could not resolve fill price for {option_symbol} — "
                    f"SL/target monitoring will not fire until price data arrives"
                )

            trade = TradeRecord(
                trade_id=self._generate_trade_id(),
                symbol=option_symbol,
                option_type=option_type,
                strike=strike,
                order_type=OrderType.BUY,
                quantity=quantity,
                price=option_ltp,
                timestamp=datetime.now(),
                status="OPEN",
                lot_size=self._active_index.lot_size,
            )
            self._positions[trade.trade_id] = trade
            self._daily_stats.total_trades += 1
            self._last_trade_time = datetime.now()
            # Track held qty for over-sell guard
            _hkey = (option_type.value, strike)
            self._held_qty[_hkey] = self._held_qty.get(_hkey, 0) + quantity
            logger.info(f"Trade recorded: {trade.trade_id} | fill=₹{option_ltp:.2f}")

            # Place GTT exchange-level stop-loss on the option premium (survives bot crash/restart)
            if self.config.use_gtt and option_ltp > 0:
                exchange = "BFO" if self._active_index.exchange == "BSE" else "NFO"
                gtt_id = await self.kite.place_gtt(
                    symbol=trade.symbol,
                    exchange=exchange,
                    entry_price=option_ltp,
                    stop_loss_pct=self.config.stop_loss_percentage,
                    quantity=quantity,
                )
                trade.gtt_id = gtt_id

        return result
    
    async def manual_order(
        self,
        option_type: str,  # "CE" or "PE"
        strike: int,
        order_type: str = "BUY",  # "BUY" or "SELL"
        quantity: int = None,
        source: str = "Manual",     # GAP | ORB | VWAP | Auto | EOD | Rule | Manual
    ) -> OrderResult:
        """
        Place a manual order (from chat command).
        
        Args:
            option_type: "CE" or "PE"
            strike: Strike price
            order_type: "BUY" or "SELL"
            quantity: Number of units
            
        Returns:
            OrderResult
        """
        # Parse enums
        try:
            opt_type = OptionType[option_type.upper()]
            ord_type = OrderType[order_type.upper()]
        except KeyError as e:
            return OrderResult(
                success=False,
                order_id=None,
                message=f"Invalid option/order type: {e}",
                timestamp=datetime.now()
            )
        
        # Check if we can trade (skip for SELL)
        if ord_type == OrderType.BUY:
            can_trade, reason = self.can_place_order()
            if not can_trade:
                return OrderResult(
                    success=False,
                    order_id=None,
                    message=reason,
                    timestamp=datetime.now()
                )
            # Strike-loss guard
            ok, msg = self.can_enter_strike(option_type, strike)
            if not ok:
                return OrderResult(success=False, order_id=None, message=msg, timestamp=datetime.now())

        if ord_type == OrderType.SELL:
            # Over-sell guard
            qty_check = quantity or self.get_active_quantity()
            ok, msg = self.can_sell_qty(option_type, strike, qty_check)
            if not ok:
                return OrderResult(success=False, order_id=None, message=msg, timestamp=datetime.now())
        
        quantity = quantity or self.get_active_quantity()

        # Capture pre-order LTP so we can measure slippage after fill
        _pre_order_ltp = 0.0
        if ord_type == OrderType.BUY:
            _pre_symbol = f"{self._active_index.name}{strike}{opt_type.value}"
            try:
                _pre_order_ltp = await self.kite.get_instrument_price(_pre_symbol) or 0.0
            except Exception:
                pass

        # Pre-trade risk gate: ensure potential SL loss won't breach the daily cap.
        # can_place_order() only checks current P&L — this forward-projects the worst-case
        # outcome so Trade 3 can't push total losses past max_daily_loss.
        if ord_type == OrderType.BUY and _pre_order_ltp > 0:
            _potential_loss = _pre_order_ltp * quantity * self.config.stop_loss_percentage / 100
            _projected_pnl = self._daily_pnl - _potential_loss
            logger.debug(
                f"GATE[pre_trade_risk]: ltp=₹{_pre_order_ltp:.0f} qty={quantity} "
                f"sl={self.config.stop_loss_percentage}% "
                f"potential_loss=₹{_potential_loss:.0f} "
                f"projected_pnl=₹{_projected_pnl:,.0f} cap=-₹{self.config.max_daily_loss:,.0f}"
            )
            if _projected_pnl < -self.config.max_daily_loss:
                logger.warning(
                    f"Pre-trade risk gate: ₹{_potential_loss:.0f} potential loss "
                    f"(₹{_pre_order_ltp:.0f} × {quantity} × {self.config.stop_loss_percentage}% SL) "
                    f"would push daily P&L to ₹{_projected_pnl:,.0f} — "
                    f"below cap of -₹{self.config.max_daily_loss:,.0f}"
                )
                return OrderResult(
                    success=False,
                    order_id=None,
                    message=(
                        f"Trade blocked: ₹{_potential_loss:.0f} potential loss would breach daily cap "
                        f"(current P&L ₹{self._daily_pnl:,.0f}, cap ₹{self.config.max_daily_loss:,.0f})"
                    ),
                    timestamp=datetime.now(),
                )

        result = await self.kite.place_order(
            option_type=opt_type,
            strike=strike,
            order_type=ord_type,
            quantity=quantity
        )
        
        if result.success and ord_type == OrderType.BUY:
            trade = TradeRecord(
                trade_id=self._generate_trade_id(),
                symbol=f"{self._active_index.name}{strike}{opt_type.value}",
                option_type=opt_type,
                strike=strike,
                order_type=ord_type,
                quantity=quantity,
                price=0,
                timestamp=datetime.now(),
                status="OPEN",
                lot_size=self._active_index.lot_size,
                source=source,
            )
            self._positions[trade.trade_id] = trade
            self._daily_stats.total_trades += 1
            self._last_trade_time = datetime.now()
            # Track held qty for over-sell guard
            _hkey = (opt_type.value, strike)
            self._held_qty[_hkey] = self._held_qty.get(_hkey, 0) + quantity
            # Wait 2s for the order to settle in the broker's book before querying.
            # This ensures the trailing-stop / SL calculations use the real fill price,
            # not a stale LTP snapshot that may differ by 0.5–2 points.
            await asyncio.sleep(2)
            fill_price = 0.0
            try:
                pos_data = await self.kite.get_kite_positions()
                for p in pos_data.get("day", []):
                    sym = p.get("tradingsymbol", "")
                    if (str(strike) in sym and opt_type.value in sym
                            and (p.get("quantity") or 0) != 0):
                        avg = float(p.get("average_price") or 0)
                        if avg > 0:
                            fill_price = avg
                            break
            except Exception as e:
                logger.debug(f"Broker fill fetch failed, falling back to LTP: {e}")

            if fill_price == 0.0:
                # fallback: use LTP if broker fill unavailable
                try:
                    fill_price = await self.kite.get_instrument_price(trade.symbol) or 0.0
                except Exception:
                    pass

            if fill_price > 0:
                trade.price = fill_price
                trade.highest_price = fill_price
                logger.info(f"manual_order: entry fill price = ₹{fill_price:.2f} for {trade.symbol}")
                # Slippage: positive = paid more than pre-order snapshot (bad)
                if _pre_order_ltp > 0:
                    trade.slippage = round(fill_price - _pre_order_ltp, 2)
                    if abs(trade.slippage) > 5:
                        logger.warning(
                            f"High slippage: ₹{trade.slippage:+.2f} on {trade.symbol} "
                            f"(expected ₹{_pre_order_ltp:.2f}, filled ₹{fill_price:.2f})"
                        )

            # Place GTT exchange-level stop-loss (optional, survives bot restarts)
            if self.config.use_gtt and trade.price > 0:
                try:
                    exchange = "BFO" if self._active_index.exchange == "BSE" else "NFO"
                    gtt_id = await self.kite.place_gtt(
                        symbol=trade.symbol,
                        exchange=exchange,
                        entry_price=trade.price,
                        stop_loss_pct=self.config.stop_loss_percentage,
                        quantity=quantity,
                    )
                    trade.gtt_id = gtt_id
                except Exception as e:
                    logger.warning(f"GTT placement after manual order failed: {e}")

        return result
    
    async def close_position(
        self,
        trade_id: str = None,
        symbol: str = None,
        exit_price: float = 0.0,
        partial_qty: int = None
    ) -> OrderResult:
        """
        Close (or partially close) a position by trade ID or symbol.

        Args:
            trade_id:    Trade ID to close
            symbol:      Symbol pattern to match
            exit_price:  Current option price for P&L calculation
            partial_qty: If set, only close this many units (partial exit)
        """
        target = None

        if trade_id and trade_id in self._positions:
            target = self._positions[trade_id]
        elif symbol:
            for tid, trade in self._positions.items():
                if symbol.upper() in trade.symbol.upper() and trade.status == "OPEN":
                    target = trade
                    break

        if not target:
            return OrderResult(
                success=False,
                order_id=None,
                message=f"Position not found",
                timestamp=datetime.now()
            )

        qty_to_close = partial_qty if partial_qty and partial_qty < target.quantity else target.quantity

        # Always try to parse index from Dhan dash-format symbol
        # (e.g. "NIFTY-Mar2026-23250-CE") so we don't depend on the broker's
        # _active_index which may have been switched by the UI since entry.
        effective_strike = target.strike
        index_override = None
        m = re.match(r'^([A-Za-z]+)-\w+-(\d+)-(CE|PE)$', target.symbol, re.IGNORECASE)
        if m:
            if effective_strike == 0:
                effective_strike = int(m.group(2))
            try:
                index_override = get_index(m.group(1).upper())
            except Exception:
                logger.debug(f"close_position: unknown index '{m.group(1)}' — using active index")

        result = await self.kite.place_order(
            option_type=target.option_type,
            strike=effective_strike,
            order_type=OrderType.SELL,
            quantity=qty_to_close,
            index_config=index_override,
        )

        if result.success:
            # Cancel the GTT stop-loss — position is now closed by the bot
            if target.gtt_id:
                try:
                    await self.kite.cancel_gtt(target.gtt_id)
                    target.gtt_id = None
                except Exception as e:
                    logger.warning(f"Could not cancel GTT {target.gtt_id}: {e}")

            if exit_price > 0 and target.price > 0:
                pnl = (exit_price - target.price) * qty_to_close
                realized = pnl
                self._daily_pnl += realized
                self._save_daily_pnl()  # persist so restarts don't lose the daily loss tally
                if target.pnl is None:
                    target.pnl = 0.0
                target.pnl += realized
                self._daily_stats.realized_pnl += realized

                if qty_to_close == target.quantity:
                    # Full close — update win/loss streaks
                    _hkey = (target.option_type.value if hasattr(target.option_type, 'value') else str(target.option_type), effective_strike)
                    if realized < 0:
                        self._daily_stats.losing_trades += 1
                        self._consecutive_losses += 1
                        # Per-strike loss counter
                        self._strike_loss_count[_hkey] = self._strike_loss_count.get(_hkey, 0) + 1
                        if self._strike_loss_count[_hkey] >= self._max_strike_losses:
                            logger.warning(
                                f"Strike {_hkey[0]} {_hkey[1]} has lost "
                                f"{self._strike_loss_count[_hkey]}x today — blocked from re-entry."
                            )
                        if self._consecutive_losses >= self.config.max_consecutive_losses:
                            pause_until = datetime.now() + timedelta(
                                minutes=self.config.pause_after_losses_minutes
                            )
                            self._trading_paused_until = pause_until
                            logger.warning(
                                f"{self._consecutive_losses} consecutive losses — "
                                f"trading paused for {self.config.pause_after_losses_minutes} min."
                            )
                    else:
                        self._daily_stats.winning_trades += 1
                        self._consecutive_losses = 0
                        self._trading_paused_until = None
                    # Decrement held qty on full close
                    self._held_qty[_hkey] = max(0, self._held_qty.get(_hkey, 0) - qty_to_close)
                else:
                    # Partial close — decrement held qty proportionally
                    _hkey = (target.option_type.value if hasattr(target.option_type, 'value') else str(target.option_type), effective_strike)
                    self._held_qty[_hkey] = max(0, self._held_qty.get(_hkey, 0) - qty_to_close)

            if qty_to_close == target.quantity:
                # Full exit
                target.status = "CLOSED"
                self._trade_history.append(target)
                if target.trade_id in self._positions:
                    del self._positions[target.trade_id]
            else:
                # Partial exit — reduce remaining quantity
                target.quantity -= qty_to_close

        return result

    def check_profit_tiers(
        self, current_prices: Dict[str, float]
    ) -> list:
        """
        Check tiered profit-taking thresholds.

        Returns:
            List of (trade_id, qty_to_close, tier_label) for each triggered tier.
        """
        if not self.config.use_profit_tiers:
            return []

        actions = []

        for trade_id, trade in self._positions.items():
            if trade.status != "OPEN" or trade.symbol not in current_prices:
                continue

            current = current_prices[trade.symbol]
            entry = trade.price
            if entry <= 0:
                continue

            pnl_pct = (current - entry) / entry * 100

            # Use the lot size recorded at trade entry — immune to active-index mid-swap.
            lot_size = max(1, trade.lot_size)

            if not trade.tier1_exited and pnl_pct >= self.config.take_profit_tier_1_percent:
                raw = trade.quantity * self.config.take_profit_tier_1_quantity_percent / 100
                lots = int(raw // lot_size)  # how many whole lots to exit
                # If raw rounds down to 0 lots (single-lot position), exit exactly 1 lot
                qty = max(lot_size, lots * lot_size)
                qty = min(qty, trade.quantity)  # never exceed remaining position
                actions.append((trade_id, qty, "TIER1"))

            elif trade.tier1_exited and pnl_pct >= self.config.take_profit_tier_2_percent:
                raw = trade.quantity * self.config.take_profit_tier_2_quantity_percent / 100
                lots = int(raw // lot_size)
                qty = max(lot_size, lots * lot_size)
                qty = min(qty, trade.quantity)  # never exceed remaining position
                actions.append((trade_id, qty, "TIER2"))

        return actions
    
    async def close_all_positions(self) -> List[OrderResult]:
        """Close all open positions"""
        results = []
        
        open_trades = [t for t in self._positions.values() if t.status == "OPEN"]
        
        for trade in open_trades:
            result = await self.close_position(trade_id=trade.trade_id)
            results.append(result)
        
        return results
    
    def check_stop_loss_targets(self, current_prices: Dict[str, float]) -> List[str]:
        """
        Check if any positions hit stop-loss, trailing-stop, or target.

        Args:
            current_prices: Dict mapping symbol to current price

        Returns:
            List of trade IDs that need to be closed.
        """
        actions_needed = []

        for trade_id, trade in self._positions.items():
            if trade.status != "OPEN":
                continue

            if trade.symbol not in current_prices:
                continue

            current = current_prices[trade.symbol]
            entry = trade.price

            if entry <= 0:
                continue

            # Always keep the peak price fresh (needed for trailing stop)
            if current > trade.highest_price:
                trade.highest_price = current

            pnl_pct = ((current - entry) / entry) * 100

            # ── Trailing stop-loss ─────────────────────────────────────────────
            # Arms only after the position reaches `trailing_stop_activation_pct`
            # profit.  Before that the fixed SL is the only guard, so a normal
            # intraday fluctuation never mis-fires the trail on a tiny tick up.
            # Once armed, the trail follows the peak and exits on any pullback
            # beyond `trailing_stop_percentage` — capturing the maximum run.
            _activation_pct = getattr(self.config, 'trailing_stop_activation_pct', 10.0)
            _trail_armed = trade.highest_price >= entry * (1 + _activation_pct / 100)
            if self.config.use_trailing_stop_loss and _trail_armed:
                if self.config.use_trailing_stop_amount:
                    # ₹ amount is total-P&L basis → convert to per-unit
                    # e.g. ₹500 trail on 50 lots = ₹10/unit trail distance
                    qty = max(trade.quantity, 1)
                    trail_sl = trade.highest_price - (self.config.trailing_stop_amount / qty)
                else:
                    # After BOTH profit tiers hit, tighten the trail on the final
                    # runner — default tight trail = 5% vs 12% normal distance.
                    # This locks nearly all remaining gains once the big move is done.
                    _trail_pct = (
                        getattr(self.config, 'tight_trail_after_tier2_pct', 5.0)
                        if getattr(trade, 'tier2_exited', False)
                        else self.config.trailing_stop_percentage
                    )
                    trail_sl = trade.highest_price * (1 - _trail_pct / 100)
                if current <= trail_sl:
                    locked_pct = ((trade.highest_price - entry) / entry) * 100
                    logger.info(
                        f"TRAILING STOP triggered for {trade.symbol}: "
                        f"peak={trade.highest_price:.2f}, trail_sl={trail_sl:.2f}, "
                        f"current={current:.2f}  (had locked {locked_pct:.1f}%)"
                    )
                    actions_needed.append(trade_id)
                    continue  # skip fixed SL/target check

            # ── Time-stop: exit if open > N minutes with no meaningful profit ──
            # Per-strategy adaptive limits reflect each strategy's natural hold time:
            #   GAP=60 min  (gaps need time to develop or fill)
            #   ORB=45 min  (standard breakout window)
            #   VWAP=30 min (mean-reversion is fast — works or doesn't within 30 min)
            #   Auto=90 min (trend-following needs time to play out)
            #   EOD=15 min  (sprint to close — no time to wait)
            #   Others=45 min (conservative default)
            _source_time_stops = {
                "GAP":    60,
                "ORB":    45,
                "VWAP":   30,
                "Auto":   90,
                "EOD":    15,
                "Rule":   45,
                "Manual": 45,
            }
            _trade_source = getattr(trade, 'source', 'Manual')
            time_stop_mins = _source_time_stops.get(
                _trade_source,
                getattr(self.config, 'time_stop_minutes', 45)
            )
            if time_stop_mins > 0:
                age_min = (datetime.now() - trade.timestamp).total_seconds() / 60
                # Trigger only if: old enough AND peak never exceeded entry by 2%
                if age_min >= time_stop_mins and trade.highest_price < entry * 1.02:
                    logger.info(
                        f"TIME-STOP triggered for {trade.symbol}: "
                        f"{age_min:.0f} min open, "
                        f"peak={trade.highest_price:.2f}, entry={entry:.2f} "
                        f"(never moved >2% into profit)"
                    )
                    actions_needed.append(trade_id)
                    continue

            # ── Max loss per trade (absolute ₹ amount) ────────────────────────
            pnl_inr = (current - entry) * trade.quantity
            if (
                self.config.max_loss_per_trade > 0
                and pnl_inr <= -self.config.max_loss_per_trade
            ):
                logger.warning(
                    f"MAX LOSS PER TRADE triggered for {trade.symbol}: "
                    f"₹{pnl_inr:.0f} (limit ₹{self.config.max_loss_per_trade:.0f})"
                )
                actions_needed.append(trade_id)
                continue  # skip % SL check — already closing

            # ── Fixed stop-loss ───────────────────────────────────────────────
            if pnl_pct <= -self.config.stop_loss_percentage:
                logger.warning(f"STOP-LOSS triggered for {trade.symbol}: {pnl_pct:.1f}%")
                actions_needed.append(trade_id)

            # ── Fixed target ──────────────────────────────────────────────────
            elif pnl_pct >= self.config.target_percentage:
                logger.info(f"TARGET reached for {trade.symbol}: {pnl_pct:.1f}%")
                actions_needed.append(trade_id)

        return actions_needed
    
    def get_positions_summary(self) -> str:
        """Get formatted summary of current positions"""
        open_positions = [t for t in self._positions.values() if t.status == "OPEN"]
        
        if not open_positions:
            return "No open positions"
        
        lines = ["═══ OPEN POSITIONS ═══"]
        for trade in open_positions:
            lines.append(
                f"  {trade.trade_id}: {trade.symbol} x {trade.quantity} @ ₹{trade.price:.2f}"
            )
        
        return "\n".join(lines)
    
    def calculate_total_pnl(self, current_prices: Dict[str, float] = None) -> float:
        """
        Calculate total P&L including both realized and unrealized.
        
        Args:
            current_prices: Optional dict mapping symbol to current price
            
        Returns:
            Total P&L (realized + unrealized)
        """
        total = self._daily_pnl  # Start with realized P&L
        
        # Add unrealized P&L from open positions
        if current_prices:
            for trade_id, trade in self._positions.items():
                if trade.status == "OPEN" and trade.symbol in current_prices:
                    current = current_prices[trade.symbol]
                    entry = trade.price
                    if entry > 0:
                        # For short positions (SELL): profit if price went down
                        # For long positions (BUY): profit if price went up
                        if trade.order_type == OrderType.BUY:
                            unrealized = (current - entry) * trade.quantity
                        else:  # SELL
                            unrealized = (entry - current) * trade.quantity
                        total += unrealized
        
        return total
    
    def get_daily_summary(self) -> str:
        """Get formatted daily trading summary"""
        self._reset_daily_stats_if_needed()
        
        stats = self._daily_stats
        
        return f"""
═══ DAILY SUMMARY ({stats.date}) ═══
  Total Trades: {stats.total_trades}
  Winning: {stats.winning_trades}
  Losing: {stats.losing_trades}
  Realized P&L: ₹{stats.realized_pnl:,.2f}
  Current Day P&L: ₹{self._daily_pnl:,.2f}
"""
    

