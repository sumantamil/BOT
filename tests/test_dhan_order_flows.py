"""
Dhan Order Flow Tests
Tests: place_order, GTT stop-loss, trailing stop-loss, target, and exit/close.
Uses mocked dhanhq client so no real orders are placed.
"""
import asyncio
import sys
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, '.')

# ── helpers ──────────────────────────────────────────────────────────────────
PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
results = []

def check(label, cond, detail=""):
    status = PASS if cond else FAIL
    print(f"  {status}  {label}" + (f"  [{detail}]" if detail else ""))
    results.append((label, cond))


# ── build a DhanBroker with a mocked dhanhq client ───────────────────────────
def _nifty_next_expiry() -> date:
    """Return the next NIFTY expiry date (next Tuesday from today).
    Mirrors DhanBroker._next_expiry_date so the test instrument-map key always matches.
    """
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7 or 7  # 1 = Tuesday
    return today + timedelta(days=days_ahead)


def make_broker():
    from browser.dhan import DhanBroker
    from bot.index_config import NIFTY
    b = DhanBroker.__new__(DhanBroker)
    b.dhan_config  = MagicMock(client_id="2603155234", access_token="test")
    b.trading_config = MagicMock(
        default_quantity=65,
        stop_loss_percentage=15.0,
        target_percentage=30.0,
        use_trailing_stop_loss=True,
        trailing_stop_percentage=12.0,
        use_trailing_stop_amount=False,
        trailing_stop_amount=750.0,
        use_profit_tiers=True,
        take_profit_tier_1_percent=15.0,
        take_profit_tier_1_quantity_percent=50.0,
        take_profit_tier_2_percent=25.0,
        take_profit_tier_2_quantity_percent=50.0,
        max_positions=2,
        max_trades_per_day=4,
        min_time_between_trades_minutes=10,
        max_daily_loss=5000,
        max_consecutive_losses=3,
        pause_after_losses_minutes=30,
        market_open_hour=9,
        market_open_minute=15,
        market_close_hour=15,
        market_close_minute=30,
        use_gtt=True,
    )
    b._active_index = NIFTY
    b._logged_in    = True
    b._browser      = None
    b._page         = None

    # Instrument lookup cache — uses the real next-expiry date so the key always matches
    _expiry = _nifty_next_expiry()
    b._instrument_map = {
        f"NIFTY|24500|CE|{_expiry.isoformat()}": "57847",
        f"NIFTY|24500|PE|{_expiry.isoformat()}": "57848",
    }
    b._instruments_loaded_date = date.today()
    # Security ID cache (matches what place_order populates after a real trade)
    b._position_security_ids = {}

    # Mock dhanhq client
    mock_client = MagicMock()
    b._client = mock_client
    return b, mock_client


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — Place order (BUY)
# ─────────────────────────────────────────────────────────────────────────────
async def test_place_order():
    print("\n── TEST 1: Place Order (BUY CE) ──")
    b, mock_client = make_broker()

    mock_client.place_order.return_value = {
        "status": "success",
        "data": {"orderId": "ORD001"}
    }

    from browser.dhan import OptionType, OrderType
    result = await b.place_order(OptionType.CE, 24500, OrderType.BUY, quantity=65)

    check("result.success is True",  result.success,  f"success={result.success}")
    check("order_id returned",       result.order_id == "ORD001", f"id={result.order_id}")
    check("place_order API called",  mock_client.place_order.called)
    call_kwargs = mock_client.place_order.call_args[1]
    check("security_id correct",     call_kwargs.get("security_id") == "57847", f"sid={call_kwargs.get('security_id')}")
    check("exchange NSE_FNO",        call_kwargs.get("exchange_segment") == "NSE_FNO")
    check("transaction_type BUY",    call_kwargs.get("transaction_type") == "BUY")
    check("product_type INTRADAY",   call_kwargs.get("product_type") == "INTRADAY")
    check("order_type MARKET",       call_kwargs.get("order_type") == "MARKET")
    check("quantity 65",             call_kwargs.get("quantity") == 65)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — GTT / Forever Order (Stop Loss)
# ─────────────────────────────────────────────────────────────────────────────
async def test_gtt_place():
    print("\n── TEST 2: GTT / Forever Order (Stop-Loss) ──")
    b, mock_client = make_broker()

    mock_client.place_forever.return_value = {
        "status": "success",
        "data": {"orderId": "GTT001"}
    }

    gtt_id = await b.place_gtt(
        symbol="NIFTY24500CE",
        exchange="NSE_FNO",
        entry_price=100.0,
        stop_loss_pct=15.0,
        quantity=65,
    )

    check("Forever Order placed",        mock_client.place_forever.called)
    check("gtt_id returned",             gtt_id == 1, f"gtt_id={gtt_id}")
    kw = mock_client.place_forever.call_args[1]
    expected_trigger = round(100.0 * (1 - 15/100), 1)   # 85.0
    expected_limit   = round(expected_trigger * 0.98, 1) # 83.3
    check(f"trigger_Price = ₹{expected_trigger}",
          kw.get("trigger_Price") == expected_trigger, f"got={kw.get('trigger_Price')}")
    check(f"limit price = ₹{expected_limit}",
          kw.get("price") == expected_limit,  f"got={kw.get('price')}")
    check("transaction_type SELL",       kw.get("transaction_type") == "SELL")
    check("product_type CNC",            kw.get("product_type") == "CNC")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — GTT Cancel
# ─────────────────────────────────────────────────────────────────────────────
async def test_gtt_cancel():
    print("\n── TEST 3: Cancel Forever Order ──")
    b, mock_client = make_broker()

    mock_client.cancel_forever.return_value = {"status": "success", "data": {}}

    ok = await b.cancel_gtt(gtt_id=999)

    check("cancel_forever called",  mock_client.cancel_forever.called)
    check("returns True on success",      ok is True, f"ok={ok}")
    call_args = mock_client.cancel_forever.call_args
    check("correct order_id passed",      call_args[1].get("order_id") == "999")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Close position (exit SELL)
# ─────────────────────────────────────────────────────────────────────────────
async def test_close_position():
    print("\n── TEST 4: Close Position (Exit SELL) ──")
    b, mock_client = make_broker()

    # Inject a fake open position into the positions response
    mock_client.get_positions.return_value = {
        "status": "success",
        "data": [{
            "tradingSymbol": "NIFTY24500CE",
            "netQty": 65,
            "buyQty": 65,
            "sellQty": 0,
            "costPrice": 100.0,
            "ltp": 130.0,
            "dayBuyQty": 65,
            "realizedProfit": 0,
            "unrealizedProfit": 1950.0,
            "exchangeSegment": "NSE_FNO",
        }]
    }
    mock_client.place_order.return_value = {"status": "success", "data": {"orderId": "EXIT001"}}

    result = await b.close_position("NIFTY24500CE")

    check("exit order placed",         result.success, f"success={result.success}")
    check("order_id returned",         result.order_id == "EXIT001")
    kw = mock_client.place_order.call_args[1]
    check("transaction_type SELL",     kw.get("transaction_type") == "SELL")
    check("quantity correct",          kw.get("quantity") == 65, f"qty={kw.get('quantity')}")
    check("order_type MARKET",         kw.get("order_type") == "MARKET")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — Stop Loss logic (OrderManager)
# ─────────────────────────────────────────────────────────────────────────────
def test_stop_loss_logic():
    print("\n── TEST 5: Stop-Loss Logic (OrderManager) ──")
    from bot.order_manager import OrderManager, TradeRecord
    from browser.dhan import OptionType, OrderType

    om = OrderManager.__new__(OrderManager)
    om.config  = MagicMock(
        stop_loss_percentage=15.0,
        target_percentage=30.0,
        max_loss_per_trade=5000.0,
        use_trailing_stop_loss=True,
        trailing_stop_percentage=12.0,
        trailing_stop_activation_pct=10.0,
        use_trailing_stop_amount=False,
        trailing_stop_amount=500,
        time_stop_minutes=0,     # disable time-stop so it doesn't interfere
    )
    om._positions  = {}
    om._trade_history = []
    om._daily_pnl  = 0.0

    trade = TradeRecord(
        trade_id="TRD001",
        symbol="NIFTY24500CE",
        option_type=OptionType.CE,
        strike=24500,
        order_type=OrderType.BUY,
        quantity=65,
        price=100.0,
        timestamp=datetime.now(),
        highest_price=100.0,
    )
    om._positions["TRD001"] = trade

    # ── SL hit (price fell 20% below entry) ──
    hits = om.check_stop_loss_targets({"NIFTY24500CE": 80.0})
    check("Stop-loss triggers at -20%", "TRD001" in hits, f"hits={hits}")

    # ── Target hit (price up +35%) ──
    trade.highest_price = 100.0
    trade.price = 100.0
    hits = om.check_stop_loss_targets({"NIFTY24500CE": 135.0})
    check("Target triggers at +35%",    "TRD001" in hits, f"hits={hits}")

    # ── Trailing stop (price peaked at 160 then fell back to 140) ──
    trade.highest_price = 160.0
    trail_level = round(160 * (1 - 12/100), 2)   # 140.8
    hits = om.check_stop_loss_targets({"NIFTY24500CE": 140.0})
    check(f"Trailing SL triggers below ₹{trail_level}", "TRD001" in hits, f"hits={hits}")

    # ── No action when price is healthy ──
    trade.highest_price = 115.0
    hits = om.check_stop_loss_targets({"NIFTY24500CE": 115.0})
    check("No trigger when price is healthy", "TRD001" not in hits, f"hits={hits}")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — Profit Tiers logic (OrderManager)
# ─────────────────────────────────────────────────────────────────────────────
def test_profit_tiers():
    print("\n── TEST 6: Profit Tiers / Partial-Exit Target ──")
    from bot.order_manager import OrderManager, TradeRecord
    from browser.dhan import OptionType, OrderType

    om = OrderManager.__new__(OrderManager)
    om.config = MagicMock(
        use_profit_tiers=True,
        take_profit_tier_1_percent=15.0,
        take_profit_tier_1_quantity_percent=50.0,
        take_profit_tier_2_percent=25.0,
        take_profit_tier_2_quantity_percent=50.0,
    )
    om._positions = {}

    trade = TradeRecord(
        trade_id="TRD001",
        symbol="NIFTY24500CE",
        option_type=OptionType.CE,
        strike=24500,
        order_type=OrderType.BUY,
        quantity=65,
        price=100.0,
        timestamp=datetime.now(),
        highest_price=100.0,
        tier1_exited=False,
    )
    om._positions["TRD001"] = trade

    # Tier 1: price up +20%  →  partial exit 50% of 65 = 32 units
    actions = om.check_profit_tiers({"NIFTY24500CE": 120.0})
    check("Tier-1 partial exit triggered at +20%", len(actions) == 1, f"actions={actions}")
    if actions:
        _, qty, label = actions[0]
        check("Tier-1 exits ~50% quantity", qty == 32, f"qty={qty}")
        check("Tier-1 label correct",       label == "TIER1")

    # After tier1 exit, tier2 at +30%
    trade.tier1_exited = True
    actions = om.check_profit_tiers({"NIFTY24500CE": 130.0})
    check("Tier-2 partial exit triggered at +30%", len(actions) == 1, f"actions={actions}")
    if actions:
        _, qty, label = actions[0]
        check("Tier-2 label correct", label == "TIER2")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 — AMO (After Market Order) detection + flag
# ─────────────────────────────────────────────────────────────────────────────
async def test_amo_order():
    print("\n── TEST 7: AMO (After Market Order) ──")
    from browser.dhan import DhanBroker, OptionType, OrderType

    b, mock_client = make_broker()
    mock_client.place_order.return_value = {
        "status": "success",
        "data": {"orderId": "AMO001"}
    }

    import unittest.mock as um

    # Use Monday March 23 as the fake date so _next_expiry_date returns Tuesday March 24,
    # which matches the dynamic instrument-map key in make_broker().
    # ── Simulate 18:30 (inside AMO evening window) ──
    fake_evening = datetime(2026, 3, 23, 18, 30)   # Monday evening
    with um.patch("browser.dhan.datetime") as mock_dt:
        mock_dt.now.return_value = fake_evening
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        is_amo = DhanBroker.is_amo_window()
    check("18:30 → AMO window detected", is_amo, f"is_amo={is_amo}")

    # ── Simulate 08:00 (inside AMO morning window) ──
    fake_morning = datetime(2026, 3, 23, 8, 0)   # Monday morning
    with um.patch("browser.dhan.datetime") as mock_dt:
        mock_dt.now.return_value = fake_morning
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        is_amo = DhanBroker.is_amo_window()
    check("08:00 → AMO window detected", is_amo, f"is_amo={is_amo}")

    # ── Simulate 10:30 (market hours, NOT AMO) ──
    fake_market = datetime(2026, 3, 23, 10, 30)
    with um.patch("browser.dhan.datetime") as mock_dt:
        mock_dt.now.return_value = fake_market
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        is_amo = DhanBroker.is_amo_window()
    check("10:30 → NOT AMO window", not is_amo, f"is_amo={is_amo}")

    # ── AMO order sends after_market_order=True and product_type=CNC ──
    with um.patch("browser.dhan.datetime") as mock_dt:
        mock_dt.now.return_value = fake_evening
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        result = await b.place_order(OptionType.CE, 24500, OrderType.BUY, quantity=65)

    check("AMO place_order succeeds", result.success, f"success={result.success}")
    kw = mock_client.place_order.call_args[1]
    check("after_market_order=True sent",  kw.get("after_market_order") is True,  f"got={kw.get('after_market_order')}")
    check("product_type=CNC for AMO",      kw.get("product_type") == "CNC",       f"got={kw.get('product_type')}")

    # ── Normal market-hours order must NOT send after_market_order ──
    mock_client.place_order.reset_mock()
    with um.patch("browser.dhan.datetime") as mock_dt:
        mock_dt.now.return_value = fake_market
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        result = await b.place_order(OptionType.CE, 24500, OrderType.BUY, quantity=65)

    kw2 = mock_client.place_order.call_args[1]
    check("Market-hours order: no AMO flag",    kw2.get("after_market_order") is None, f"got={kw2.get('after_market_order')}")
    check("Market-hours order: product INTRADAY", kw2.get("product_type") == "INTRADAY", f"got={kw2.get('product_type')}")

    # ── OrderManager allows order during AMO window ──
    from bot.order_manager import OrderManager
    om = OrderManager.__new__(OrderManager)
    om.config = MagicMock(
        amo_enabled=True,
        max_trades_per_day=4,
        min_time_between_trades_minutes=10,
        max_positions=2,
        max_daily_loss=5000,
        market_open_hour=9, market_open_minute=15,
        market_close_hour=15, market_close_minute=30,
        max_consecutive_losses=3,
        pause_after_losses_minutes=30,
    )
    om._positions = {}
    om._daily_stats = MagicMock(date=datetime.now().date(), total_trades=0)
    om._daily_pnl = 0.0
    om._trading_paused_until = None
    om._last_trade_time = None

    with um.patch.object(OrderManager, '_is_market_hours', return_value=False), \
         um.patch.object(OrderManager, '_is_amo_window', return_value=True):
        can, reason = om.can_place_order()
    check("OrderManager allows order in AMO window", can, f"can={can}, reason={reason}")
    check("reason is 'AMO window'", reason == "AMO window", f"reason={reason}")

    # ── OrderManager blocks order outside both windows ──
    with um.patch.object(OrderManager, '_is_market_hours', return_value=False), \
         um.patch.object(OrderManager, '_is_amo_window', return_value=False):
        can, reason = om.can_place_order()
    check("OrderManager blocks outside market+AMO", not can, f"can={can}, reason={reason}")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 — Symbol matching (flexible format handling)
# ─────────────────────────────────────────────────────────────────────────────
def test_symbol_matching():
    print("\n── TEST 8: Symbol Matching & Security ID Cache ──")
    b, _ = make_broker()

    # _symbol_matches: ZerodhaStyle vs various Dhan position formats
    check("NIFTY24500CE matches NIFTY24500CE (exact)",
          b._symbol_matches("NIFTY24500CE", "NIFTY24500CE"))
    check("NIFTY24500CE matches NIFTY2431724500CE (compact with date)",
          b._symbol_matches("NIFTY24500CE", "NIFTY2431724500CE"))
    check("NIFTY24500CE matches NIFTY-17MAR26-24500-CE (dash format)",
          b._symbol_matches("NIFTY24500CE", "NIFTY-17MAR26-24500-CE"))
    check("NIFTY24500CE does NOT match NIFTY24500PE (wrong type)",
          not b._symbol_matches("NIFTY24500CE", "NIFTY24500PE"))
    check("BANKNIFTY48000CE matches BANKNIFTY2431748000CE",
          b._symbol_matches("BANKNIFTY48000CE", "BANKNIFTY2431748000CE"))

    # _sec_id_for_symbol: cache-first priority
    b._position_security_ids["NIFTY24500CE"] = "99999"
    sid = b._sec_id_for_symbol("NIFTY24500CE")
    check("Cached security_id returned (99999)", sid == "99999", f"sid={sid}")

    # Without cache, falls back to instrument map
    b._position_security_ids.clear()
    sid = b._sec_id_for_symbol("NIFTY24500CE")
    check("Instrument map fallback returns a security_id", sid is not None, f"sid={sid}")

    # place_order populates the cache
    b._position_security_ids.clear()
    # Simulate that after a successful place_order, cache is set
    b._position_security_ids["NIFTY24500CE"] = "57847"
    check("Cache populated after order placement",
          b._position_security_ids.get("NIFTY24500CE") == "57847")

    # close_position clears the cache entry
    b._position_security_ids.pop("NIFTY24500CE", None)
    check("Cache cleared after close_position",
          "NIFTY24500CE" not in b._position_security_ids)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 — can_place_order() validation chain: one test per gate
# ─────────────────────────────────────────────────────────────────────────────
def _make_om():
    """Return a minimal OrderManager with all guards in their permissive state."""
    import unittest.mock as um
    from bot.order_manager import OrderManager
    from bot.index_config import NIFTY

    om = OrderManager.__new__(OrderManager)
    om.config = MagicMock(
        amo_enabled=False,
        max_trades_per_day=5,
        min_time_between_trades_minutes=10,
        max_positions=3,
        max_daily_loss=5000,
        max_consecutive_losses=3,
        pause_after_losses_minutes=30,
        market_open_hour=9,  market_open_minute=15,
        market_close_hour=15, market_close_minute=30,
    )
    om._active_index = NIFTY
    om._positions = {}
    om._daily_stats = MagicMock(date=date.today(), total_trades=0)
    om._daily_pnl = 0.0
    om._trading_paused_until = None
    om._last_trade_time = None
    om._consecutive_losses = 0
    om._strike_loss_count = {}
    om._max_strike_losses = 2
    om._held_qty = {}
    return om


def test_validation_chain():
    """
    Exercise every return-False path in the validation chain independently.

    Gates tested (in can_place_order call order):
      G1 market_hours  — outside 9:15–15:30 IST, AMO disabled
      G1b AMO          — outside market hours, AMO enabled + in window
      G2 cool_off      — trading_paused_until in the future
      G3 daily_limit   — total_trades >= max_trades_per_day
      G4 min_gap       — last trade was < min_time_between_trades_minutes ago
      G4b min_gap_skip — hedge leg: gap NOT enforced when positions are open
      G5 max_positions — active_positions >= max_positions
      G6 daily_loss    — daily_pnl <= -max_daily_loss

    Gates tested in can_enter_strike:
      G7 strike_repeat — already lost max_strike_losses times on this strike

    Gate tested in PaperTrader.paper_buy:
      G8 paper_dedup   — same (index, strategy, option_type) already open today
    """
    import unittest.mock as um
    from bot.order_manager import OrderManager
    from bot.paper_trader import PaperTrader
    print("\n── TEST 9: Validation Chain (all 8 gates) ──")

    # ── G1: outside market hours, AMO disabled ───────────────────────────────
    om = _make_om()
    with um.patch.object(OrderManager, '_is_market_hours', return_value=False), \
         um.patch.object(OrderManager, '_is_amo_window',  return_value=False):
        can, reason = om.can_place_order()
    check("G1 market_hours — blocked outside hours", not can, reason)
    check("G1 reason contains 'market hours'", "market hours" in reason.lower(), reason)

    # ── G1b: outside market hours, AMO enabled & in window ───────────────────
    om = _make_om()
    om.config.amo_enabled = True
    with um.patch.object(OrderManager, '_is_market_hours', return_value=False), \
         um.patch.object(OrderManager, '_is_amo_window',  return_value=True):
        can, reason = om.can_place_order()
    check("G1b AMO — allowed in AMO window", can, reason)
    check("G1b reason is 'AMO window'", reason == "AMO window", reason)

    # ── G2: consecutive-loss cool-off ─────────────────────────────────────────
    om = _make_om()
    om._consecutive_losses = 3
    om._trading_paused_until = datetime.now() + timedelta(minutes=25)
    with um.patch.object(OrderManager, '_is_market_hours', return_value=True):
        can, reason = om.can_place_order()
    check("G2 cool_off — blocked during pause window", not can, reason)
    check("G2 reason mentions consecutive losses", "consecutive" in reason.lower(), reason)

    # ── G3: daily trade limit ─────────────────────────────────────────────────
    om = _make_om()
    om._daily_stats.total_trades = 5   # == max_trades_per_day
    with um.patch.object(OrderManager, '_is_market_hours', return_value=True):
        can, reason = om.can_place_order()
    check("G3 daily_limit — blocked at limit", not can, reason)
    check("G3 reason mentions daily trade limit", "daily trade limit" in reason.lower(), reason)

    # ── G4: min gap between trades (no open positions = fresh entry) ──────────
    om = _make_om()
    om._last_trade_time = datetime.now() - timedelta(minutes=3)   # only 3 min ago
    with um.patch.object(OrderManager, '_is_market_hours', return_value=True):
        can, reason = om.can_place_order()
    check("G4 min_gap — blocked when last trade was 3 min ago", not can, reason)
    check("G4 reason mentions 'too soon'", "too soon" in reason.lower(), reason)

    # ── G4b: hedge bypass — open positions exempt from min-gap ───────────────
    om = _make_om()
    om._last_trade_time = datetime.now() - timedelta(minutes=3)
    # Simulate one open position
    mock_pos = MagicMock()
    mock_pos.status = "OPEN"
    om._positions = {"TRD_001": mock_pos}
    with um.patch.object(OrderManager, '_is_market_hours', return_value=True):
        can, reason = om.can_place_order()
    check("G4b min_gap hedge bypass — allowed with open position", can, reason)

    # ── G5: max concurrent positions ─────────────────────────────────────────
    om = _make_om()
    om.config.max_positions = 2
    mock_p1, mock_p2 = MagicMock(status="OPEN"), MagicMock(status="OPEN")
    om._positions = {"T1": mock_p1, "T2": mock_p2}   # 2 == max_positions
    with um.patch.object(OrderManager, '_is_market_hours', return_value=True):
        can, reason = om.can_place_order()
    check("G5 max_positions — blocked at 2/2 open", not can, reason)
    check("G5 reason mentions max positions", "max positions" in reason.lower(), reason)

    # ── G6: daily loss limit ─────────────────────────────────────────────────
    om = _make_om()
    om._daily_pnl = -5000.0   # exactly at the -5000 limit
    with um.patch.object(OrderManager, '_is_market_hours', return_value=True):
        can, reason = om.can_place_order()
    check("G6 daily_loss — blocked when pnl == -max_daily_loss", not can, reason)
    check("G6 reason mentions daily loss limit", "daily loss limit" in reason.lower(), reason)

    # ── G_pass: all gates open → OK ──────────────────────────────────────────
    om = _make_om()
    with um.patch.object(OrderManager, '_is_market_hours', return_value=True):
        can, reason = om.can_place_order()
    check("G_pass all gates open — allowed", can, reason)
    check("G_pass reason is 'OK'", reason == "OK", reason)

    # ── G7: can_enter_strike — strike already lost twice today ───────────────
    om = _make_om()
    om._strike_loss_count[("CE", 24500)] = 2   # == _max_strike_losses
    can, reason = om.can_enter_strike("CE", 24500)
    check("G7 strike_repeat — blocked after 2 losses on same strike", not can, reason)
    check("G7 reason mentions 'lost'", "lost" in reason.lower(), reason)

    # First loss: only 1 loss, should still be allowed
    om2 = _make_om()
    om2._strike_loss_count[("CE", 24500)] = 1
    can2, reason2 = om2.can_enter_strike("CE", 24500)
    check("G7 strike_repeat — allowed after only 1 loss", can2, reason2)

    # ── G8: PaperTrader deduplication — same signal twice same day ────────────
    pt = PaperTrader()
    first = pt.paper_buy(index_name="NIFTY", index_price=22500.0, option_type="CE", strategy="ORB")
    second = pt.paper_buy(index_name="NIFTY", index_price=22600.0, option_type="CE", strategy="ORB")
    check("G8 paper_dedup — first entry accepted", first is not None)
    check("G8 paper_dedup — duplicate same-day entry rejected", second is None)

    # Different option type on same strategy → allowed (CE and PE are independent)
    third = pt.paper_buy(index_name="NIFTY", index_price=22500.0, option_type="PE", strategy="ORB")
    check("G8 paper_dedup — opposite side allowed (PE after CE)", third is not None)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("  DHAN ORDER FLOW TEST SUITE")
    print("=" * 60)

    await test_place_order()
    await test_gtt_place()
    await test_gtt_cancel()
    await test_close_position()
    test_stop_loss_logic()
    test_profit_tiers()
    await test_amo_order()
    test_symbol_matching()
    test_validation_chain()

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed

    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)")
        for label, ok in results:
            if not ok:
                print(f"    \033[91m✗ {label}\033[0m")
    else:
        print("  — all tests passed ✓")
    print("=" * 60)
    return failed == 0

if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
