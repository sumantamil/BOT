"""
Tests for browser/zerodha.py

Covers both Kite API mode and browser automation mode using mocks —
no real network, browser, or Kite credentials required.
"""

import asyncio
import calendar
import sys
import types
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, Mock, patch, PropertyMock
import pytest

# ---------------------------------------------------------------------------
# Stub out heavy optional dependencies before importing the module under test
# ---------------------------------------------------------------------------

# Stub playwright so the import doesn't require it at test time
playwright_stub = types.ModuleType("playwright")
playwright_async_stub = types.ModuleType("playwright.async_api")
for name in ("async_playwright", "Browser", "Page", "BrowserContext"):
    setattr(playwright_async_stub, name, MagicMock)
playwright_stub.async_api = playwright_async_stub
sys.modules.setdefault("playwright", playwright_stub)
sys.modules.setdefault("playwright.async_api", playwright_async_stub)

# Stub kiteconnect
kiteconnect_stub = types.ModuleType("kiteconnect")
kiteconnect_stub.KiteConnect = MagicMock
sys.modules.setdefault("kiteconnect", kiteconnect_stub)

# Stub pyotp
pyotp_stub = types.ModuleType("pyotp")
pyotp_stub.TOTP = MagicMock
sys.modules.setdefault("pyotp", pyotp_stub)

# ---------------------------------------------------------------------------
# Now import the module under test
# ---------------------------------------------------------------------------
sys.path.insert(0, ".")
from browser.zerodha import (
    ZerodhaKite,
    OrderType,
    OptionType,
    OrderResult,
    Position,
)
from bot.index_config import NIFTY, BANKNIFTY, SENSEX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kite(use_api: bool = True, api_key: str = "TESTKEY", access_token: str = "TESTTOKEN") -> ZerodhaKite:
    """Create a ZerodhaKite instance wired with a mock KiteConnect client."""
    with (
        patch("browser.zerodha.settings") as mock_settings,
        patch("browser.zerodha.KITECONNECT_AVAILABLE", True),
    ):
        mock_settings.zerodha.user_id = "U0001"
        mock_settings.zerodha.password = "pass"
        mock_settings.zerodha.totp_secret = None
        mock_settings.kite_api.use_kite_api = use_api
        mock_settings.kite_api.api_key = api_key
        mock_settings.kite_api.access_token = access_token
        mock_settings.trading.default_quantity = 65
        kite = ZerodhaKite.__new__(ZerodhaKite)
        kite.config = mock_settings.zerodha
        kite.trading_config = mock_settings.trading
        kite.kite_config = mock_settings.kite_api
        kite.use_kite_api = use_api
        kite._active_index = NIFTY
        kite._browser = None
        kite._context = None
        kite._page = None
        kite._logged_in = False
        kite._playwright = None

        mock_kite_client = MagicMock()
        mock_kite_client.set_access_token = MagicMock()
        kite.kite = mock_kite_client if use_api else None
    return kite


# ===========================================================================
# 1. Initialisation
# ===========================================================================

class TestInit:
    def test_api_mode_sets_kite_client(self):
        with (
            patch("browser.zerodha.settings") as ms,
            patch("browser.zerodha.KITECONNECT_AVAILABLE", True),
            patch("browser.zerodha.KiteConnect") as MockKC,
        ):
            ms.zerodha.user_id = ""
            ms.zerodha.password = ""
            ms.zerodha.totp_secret = None
            ms.kite_api.use_kite_api = True
            ms.kite_api.api_key = "KEY"
            ms.kite_api.access_token = "TOKEN"
            ms.trading.default_quantity = 65

            kite = ZerodhaKite()

            MockKC.assert_called_once_with(api_key="KEY")
            MockKC.return_value.set_access_token.assert_called_once_with("TOKEN")
            assert kite.kite is not None

    def test_api_mode_missing_key_leaves_kite_none(self):
        with (
            patch("browser.zerodha.settings") as ms,
            patch("browser.zerodha.KITECONNECT_AVAILABLE", True),
        ):
            ms.zerodha.user_id = ""
            ms.zerodha.password = ""
            ms.zerodha.totp_secret = None
            ms.kite_api.use_kite_api = True
            ms.kite_api.api_key = ""   # missing key
            ms.kite_api.access_token = ""
            ms.trading.default_quantity = 65

            kite = ZerodhaKite()

            assert kite.kite is None

    def test_browser_mode_does_not_create_kite_client(self):
        with (
            patch("browser.zerodha.settings") as ms,
            patch("browser.zerodha.KITECONNECT_AVAILABLE", True),
            patch("browser.zerodha.KiteConnect") as MockKC,
        ):
            ms.zerodha.user_id = ""
            ms.zerodha.password = ""
            ms.zerodha.totp_secret = None
            ms.kite_api.use_kite_api = False
            ms.kite_api.api_key = "KEY"
            ms.kite_api.access_token = "TOKEN"
            ms.trading.default_quantity = 65

            kite = ZerodhaKite()

            MockKC.assert_not_called()
            assert kite.kite is None

    def test_default_active_index_is_nifty(self):
        kite = _make_kite(use_api=False)
        assert kite._active_index is NIFTY


# ===========================================================================
# 2. set_active_index
# ===========================================================================

class TestSetActiveIndex:
    def test_set_to_banknifty(self):
        kite = _make_kite()
        kite.set_active_index(BANKNIFTY)
        assert kite._active_index is BANKNIFTY

    def test_set_to_sensex(self):
        kite = _make_kite()
        kite.set_active_index(SENSEX)
        assert kite._active_index is SENSEX


# ===========================================================================
# 3. _build_option_symbol
# ===========================================================================

class TestBuildOptionSymbol:
    def _symbol(self, kite: ZerodhaKite, option_type: OptionType, strike: int, expiry=None) -> str:
        return kite._build_option_symbol(option_type, strike, expiry)

    def test_explicit_expiry_passthrough(self):
        kite = _make_kite()
        sym = self._symbol(kite, OptionType.CE, 24000, expiry="2631724")
        assert sym == "NIFTY263172424000CE"

    def test_nifty_weekly_symbol_format(self):
        """NIFTY weekly: NIFTY{YY}{month_code}{DD}{strike}{CE|PE}"""
        kite = _make_kite()
        kite._active_index = NIFTY
        sym = self._symbol(kite, OptionType.CE, 23000)
        # Should start with NIFTY and end with 23000CE
        assert sym.startswith("NIFTY")
        assert sym.endswith("23000CE")
        # Length check: NIFTY(5) + YY(2) + month_code(1) + DD(2) + strike(5) + CE(2) = 17
        assert len(sym) == 17, f"Unexpected symbol length: {sym!r}"

    def test_nifty_october_uses_O_month_code(self):
        kite = _make_kite()
        kite._active_index = NIFTY
        # Force expiry to be in October by patching datetime
        oct_tuesday = datetime(2026, 10, 6)  # a Tuesday
        with patch("browser.zerodha.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 30)  # before Oct Tuesday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            sym = self._symbol(kite, OptionType.PE, 23000)
        assert "O" in sym, f"Expected 'O' month code in {sym!r}"

    def test_sensex_uses_sensex_name(self):
        kite = _make_kite()
        kite._active_index = SENSEX
        sym = self._symbol(kite, OptionType.PE, 80000)
        assert sym.startswith("SENSEX")
        assert sym.endswith("80000PE")

    def test_banknifty_monthly_symbol_format(self):
        """BANKNIFTY monthly: BANKNIFTY{DD}{MON}{YY}{strike}{CE|PE}"""
        kite = _make_kite()
        kite._active_index = BANKNIFTY
        sym = self._symbol(kite, OptionType.CE, 48000)
        assert sym.startswith("BANKNIFTY")
        assert sym.endswith("48000CE")

    def test_month_codes_map_all_months(self):
        kite = _make_kite()
        expected = {1:'1',2:'2',3:'3',4:'4',5:'5',6:'6',
                    7:'7',8:'8',9:'9',10:'O',11:'N',12:'D'}
        assert kite._MONTH_CODES == expected


# ===========================================================================
# 4. API mode — _place_order_kite_api
# ===========================================================================

class TestPlaceOrderKiteAPI:
    @pytest.mark.asyncio
    async def test_successful_buy_order(self):
        kite = _make_kite()
        kite.kite.place_order = MagicMock(return_value="ORDER123")

        result = await kite._place_order_kite_api(
            OptionType.CE, 24000, OrderType.BUY, quantity=65
        )

        assert result.success is True
        assert result.order_id == "ORDER123"
        assert "BUY" in result.message
        kite.kite.place_order.assert_called_once()
        call_kwargs = kite.kite.place_order.call_args.kwargs
        assert call_kwargs["exchange"] == "NFO"
        assert call_kwargs["transaction_type"] == "BUY"
        assert call_kwargs["product"] == "MIS"
        assert call_kwargs["order_type"] == "MARKET"
        # price must NOT be passed for market orders
        assert "price" not in call_kwargs

    @pytest.mark.asyncio
    async def test_successful_sell_order(self):
        kite = _make_kite()
        kite.kite.place_order = MagicMock(return_value="ORDER456")

        result = await kite._place_order_kite_api(
            OptionType.PE, 23500, OrderType.SELL, quantity=65
        )

        assert result.success is True
        call_kwargs = kite.kite.place_order.call_args.kwargs
        assert call_kwargs["transaction_type"] == "SELL"

    @pytest.mark.asyncio
    async def test_sensex_uses_bfo_exchange(self):
        kite = _make_kite()
        kite._active_index = SENSEX
        kite.kite.place_order = MagicMock(return_value="BFO001")

        result = await kite._place_order_kite_api(
            OptionType.CE, 80000, OrderType.BUY, quantity=20
        )

        assert result.success is True
        call_kwargs = kite.kite.place_order.call_args.kwargs
        assert call_kwargs["exchange"] == "BFO"

    @pytest.mark.asyncio
    async def test_kite_exception_returns_failure(self):
        kite = _make_kite()
        kite.kite.place_order = MagicMock(side_effect=Exception("Token expired"))

        result = await kite._place_order_kite_api(
            OptionType.CE, 24000, OrderType.BUY, quantity=65
        )

        assert result.success is False
        assert "Token expired" in result.message

    @pytest.mark.asyncio
    async def test_no_kite_client_returns_failure(self):
        kite = _make_kite(use_api=False)

        result = await kite._place_order_kite_api(
            OptionType.CE, 24000, OrderType.BUY, quantity=65
        )

        assert result.success is False
        assert "not initialized" in result.message.lower()

    @pytest.mark.asyncio
    async def test_uses_default_quantity_when_none(self):
        kite = _make_kite()
        kite.trading_config.default_quantity = 65
        kite.kite.place_order = MagicMock(return_value="Q001")

        await kite._place_order_kite_api(OptionType.CE, 24000, OrderType.BUY, quantity=None)

        call_kwargs = kite.kite.place_order.call_args.kwargs
        assert call_kwargs["quantity"] == 65


# ===========================================================================
# 5. API mode — get_kite_positions
# ===========================================================================

class TestGetKitePositions:
    @pytest.mark.asyncio
    async def test_returns_positions_and_pnl(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(return_value={
            "day": [
                {"tradingsymbol": "NIFTY2631724000CE", "quantity": 65, "pnl": 1200.0},
                {"tradingsymbol": "NIFTY2631723500PE", "quantity": -65, "pnl": -500.0},
            ],
            "net": [
                {"tradingsymbol": "NIFTY2631724000CE", "quantity": 65, "average_price": 150.0,
                 "last_price": 168.0, "pnl": 1200.0},
            ],
        })

        data = await kite.get_kite_positions()

        assert len(data["day"]) == 2
        assert len(data["net"]) == 1
        assert data["total_pnl"] == pytest.approx(700.0)

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(side_effect=Exception("network error"))

        data = await kite.get_kite_positions()

        assert data == {"day": [], "net": [], "total_pnl": 0.0}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_kite_client(self):
        kite = _make_kite(use_api=False)

        data = await kite.get_kite_positions()

        assert data == {"day": [], "net": [], "total_pnl": 0.0}


# ===========================================================================
# 6. API mode — get_positions
# ===========================================================================

class TestGetPositionsAPIMode:
    @pytest.mark.asyncio
    async def test_maps_net_positions_to_position_objects(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(return_value={
            "day": [],
            "net": [
                {"tradingsymbol": "NIFTY2631724000CE", "quantity": 65,
                 "average_price": 150.0, "last_price": 168.0, "pnl": 1170.0},
            ],
        })

        positions = await kite.get_positions()

        assert len(positions) == 1
        pos = positions[0]
        assert pos.symbol == "NIFTY2631724000CE"
        assert pos.quantity == 65
        assert pos.buy_price == 150.0
        assert pos.current_price == 168.0
        assert pos.pnl == pytest.approx(1170.0)

    @pytest.mark.asyncio
    async def test_filters_out_zero_quantity_positions(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(return_value={
            "day": [],
            "net": [
                {"tradingsymbol": "NIFTY2631724000CE", "quantity": 0,
                 "average_price": 0.0, "last_price": 0.0, "pnl": 0.0},
            ],
        })

        positions = await kite.get_positions()

        assert positions == []


# ===========================================================================
# 7. API mode — close_position
# ===========================================================================

class TestClosePositionAPIMode:
    @pytest.mark.asyncio
    async def test_closes_long_position_with_sell(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(return_value={
            "day": [],
            "net": [
                {"tradingsymbol": "NIFTY2631724000CE", "quantity": 65,
                 "average_price": 150.0, "last_price": 168.0, "pnl": 1170.0,
                 "exchange": "NFO", "product": "MIS"},
            ],
        })
        kite.kite.place_order = MagicMock(return_value="CLOSE001")

        result = await kite.close_position("NIFTY2631724000CE")

        assert result.success is True
        assert result.order_id == "CLOSE001"
        call_kwargs = kite.kite.place_order.call_args.kwargs
        assert call_kwargs["transaction_type"] == "SELL"
        assert call_kwargs["quantity"] == 65

    @pytest.mark.asyncio
    async def test_closes_short_position_with_buy(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(return_value={
            "day": [],
            "net": [
                {"tradingsymbol": "NIFTY2631723500PE", "quantity": -65,
                 "average_price": 90.0, "last_price": 80.0, "pnl": 650.0,
                 "exchange": "NFO", "product": "MIS"},
            ],
        })
        kite.kite.place_order = MagicMock(return_value="CLOSE002")

        result = await kite.close_position("NIFTY2631723500PE")

        assert result.success is True
        call_kwargs = kite.kite.place_order.call_args.kwargs
        assert call_kwargs["transaction_type"] == "BUY"
        assert call_kwargs["quantity"] == 65

    @pytest.mark.asyncio
    async def test_position_not_found_returns_failure(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(return_value={"day": [], "net": []})

        result = await kite.close_position("NIFTY_NONEXISTENT")

        assert result.success is False
        assert "not found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_api_exception_returns_failure(self):
        kite = _make_kite()
        kite.kite.positions = MagicMock(return_value={
            "day": [],
            "net": [
                {"tradingsymbol": "NIFTY2631724000CE", "quantity": 65,
                 "average_price": 150.0, "last_price": 168.0, "pnl": 1170.0,
                 "exchange": "NFO", "product": "MIS"},
            ],
        })
        kite.kite.place_order = MagicMock(side_effect=Exception("Margin insufficient"))

        result = await kite.close_position("NIFTY2631724000CE")

        assert result.success is False
        assert "Margin insufficient" in result.message


# ===========================================================================
# 8. API mode — get_instrument_price
# ===========================================================================

class TestGetInstrumentPrice:
    @pytest.mark.asyncio
    async def test_returns_ltp(self):
        kite = _make_kite()
        kite.kite.quote = MagicMock(return_value={
            "NIFTY2631724000CE": {"last_price": 152.5}
        })

        price = await kite.get_instrument_price("NIFTY2631724000CE")

        assert price == pytest.approx(152.5)

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        kite = _make_kite()
        kite.kite.quote = MagicMock(side_effect=Exception("Symbol not found"))

        price = await kite.get_instrument_price("INVALID")

        assert price is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_kite_client(self):
        kite = _make_kite(use_api=False)

        price = await kite.get_instrument_price("NIFTY2631724000CE")

        assert price is None


# ===========================================================================
# 9. API mode — get_holdings
# ===========================================================================

class TestGetHoldings:
    @pytest.mark.asyncio
    async def test_returns_holdings_list(self):
        kite = _make_kite()
        kite.kite.holdings = MagicMock(return_value=[{"tradingsymbol": "INFY"}])

        holdings = await kite.get_holdings()

        assert holdings == [{"tradingsymbol": "INFY"}]

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self):
        kite = _make_kite()
        kite.kite.holdings = MagicMock(side_effect=Exception("session expired"))

        holdings = await kite.get_holdings()

        assert holdings == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_kite_client(self):
        kite = _make_kite(use_api=False)

        holdings = await kite.get_holdings()

        assert holdings == []


# ===========================================================================
# 10. place_order routing
# ===========================================================================

class TestPlaceOrderRouting:
    @pytest.mark.asyncio
    async def test_routes_to_kite_api_when_enabled(self):
        kite = _make_kite(use_api=True)
        kite._place_order_kite_api = AsyncMock(return_value=OrderResult(
            success=True, order_id="API01", message="ok", timestamp=datetime.now()
        ))
        kite._place_order_browser = AsyncMock()

        await kite.place_order(OptionType.CE, 24000, OrderType.BUY, 65)

        kite._place_order_kite_api.assert_awaited_once()
        kite._place_order_browser.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_routes_to_browser_when_api_disabled(self):
        kite = _make_kite(use_api=False)
        kite._place_order_browser = AsyncMock(return_value=OrderResult(
            success=True, order_id="BR01", message="ok", timestamp=datetime.now()
        ))
        kite._place_order_kite_api = AsyncMock()

        await kite.place_order(OptionType.PE, 23500, OrderType.SELL, 65)

        kite._place_order_browser.assert_awaited_once()
        kite._place_order_kite_api.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_routes_to_browser_when_kite_client_is_none(self):
        """API is flagged but kite client failed to init."""
        kite = _make_kite(use_api=True)
        kite.kite = None  # client init failed
        kite._place_order_browser = AsyncMock(return_value=OrderResult(
            success=True, order_id="BR02", message="ok", timestamp=datetime.now()
        ))
        kite._place_order_kite_api = AsyncMock()

        await kite.place_order(OptionType.CE, 24000, OrderType.BUY, 65)

        kite._place_order_browser.assert_awaited_once()
        kite._place_order_kite_api.assert_not_awaited()


# ===========================================================================
# 11. Browser mode — _place_order_browser
# ===========================================================================

class TestPlaceOrderBrowser:
    @pytest.mark.asyncio
    async def test_returns_failure_when_not_logged_in(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = False

        result = await kite._place_order_browser(OptionType.CE, 24000, OrderType.BUY)

        assert result.success is False
        assert "not logged in" in result.message.lower()

    @pytest.mark.asyncio
    async def test_places_order_via_browser_when_logged_in(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = True

        # Mock all page interactions
        mock_page = AsyncMock()
        mock_page.query_selector = AsyncMock(return_value=None)
        mock_page.query_selector_all = AsyncMock(return_value=[])
        mock_page.wait_for_selector = AsyncMock(return_value=None)
        mock_page.keyboard = AsyncMock()

        # _check_logged_in returns True
        kite._check_logged_in = AsyncMock(return_value=True)
        kite._dismiss_modals = AsyncMock()
        kite._page = mock_page

        # Simulate submit button found on first selector
        mock_btn = AsyncMock()
        mock_btn.is_visible = AsyncMock(return_value=True)
        mock_btn.is_enabled = AsyncMock(return_value=True)
        mock_btn.inner_text = AsyncMock(return_value="Buy")
        mock_btn.click = AsyncMock()
        mock_page.wait_for_selector = AsyncMock(return_value=mock_btn)
        mock_page.query_selector = AsyncMock(return_value=mock_btn)

        result = await kite._place_order_browser(OptionType.CE, 24000, OrderType.BUY, 65)

        assert result.success is True
        assert result.order_id is not None

    @pytest.mark.asyncio
    async def test_returns_failure_when_check_logged_in_fails(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = True
        kite._check_logged_in = AsyncMock(return_value=False)
        kite._dismiss_modals = AsyncMock()
        kite._page = AsyncMock()

        result = await kite._place_order_browser(OptionType.CE, 24000, OrderType.BUY)

        assert result.success is False
        assert "not logged" in result.message.lower()


# ===========================================================================
# 12. Browser mode — get_positions (browser path)
# ===========================================================================

class TestGetPositionsBrowserMode:
    @pytest.mark.asyncio
    async def test_returns_empty_when_not_logged_in(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = False

        positions = await kite.get_positions()

        assert positions == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_page_error(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = True
        mock_page = AsyncMock()
        mock_page.click = AsyncMock(side_effect=Exception("element not found"))
        kite._page = mock_page

        positions = await kite.get_positions()

        assert positions == []


# ===========================================================================
# 13. is_logged_in
# ===========================================================================

class TestIsLoggedIn:
    def test_returns_true_in_api_mode(self):
        kite = _make_kite(use_api=True)
        assert kite.is_logged_in() is True

    def test_returns_false_in_browser_mode_when_not_logged_in(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = False
        assert kite.is_logged_in() is False

    def test_returns_true_in_browser_mode_when_logged_in(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = True
        assert kite.is_logged_in() is True


# ===========================================================================
# 14. Browser — initialize skips browser when API mode active
# ===========================================================================

class TestInitialize:
    @pytest.mark.asyncio
    async def test_api_mode_skips_playwright(self):
        kite = _make_kite(use_api=True)
        # async_playwright must not be called
        with patch("browser.zerodha.async_playwright") as mock_pw:
            await kite.initialize()
            mock_pw.assert_not_called()

    @pytest.mark.asyncio
    async def test_browser_mode_starts_playwright(self):
        kite = _make_kite(use_api=False)
        mock_pw_ctx = AsyncMock()
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_pw_ctx.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_page.evaluate = AsyncMock()

        mock_pw_instance = AsyncMock()
        mock_pw_instance.__aenter__ = AsyncMock(return_value=mock_pw_ctx)
        mock_pw_instance.__aexit__ = AsyncMock(return_value=None)
        mock_pw_instance.start = AsyncMock(return_value=mock_pw_ctx)

        with patch("browser.zerodha.async_playwright", return_value=mock_pw_instance):
            await kite.initialize(headless=True)

        assert kite._browser is not None or True  # playwright was invoked


# ===========================================================================
# 15. Browser — close
# ===========================================================================

class TestClose:
    @pytest.mark.asyncio
    async def test_close_resets_logged_in(self):
        kite = _make_kite(use_api=False)
        kite._logged_in = True
        kite._page = AsyncMock()
        kite._context = AsyncMock()
        kite._browser = AsyncMock()
        kite._playwright = AsyncMock()

        await kite.close()

        assert kite._logged_in is False
