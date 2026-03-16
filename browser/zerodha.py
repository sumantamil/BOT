"""
Zerodha Kite Browser Automation Module

Handles browser automation for:
- Login to Kite
- Navigating to options chain
- Placing buy/sell orders
- Monitoring positions
"""

import asyncio
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from loguru import logger

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

try:
    from kiteconnect import KiteConnect
    KITECONNECT_AVAILABLE = True
except ImportError:
    KITECONNECT_AVAILABLE = False

import sys
import calendar
sys.path.append('..')
from config import settings
from bot.index_config import IndexConfig, NIFTY


class OrderType(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OptionType(Enum):
    CE = "CE"  # Call Option
    PE = "PE"  # Put Option


@dataclass
class Position:
    """Represents an open position"""
    symbol: str
    quantity: int
    buy_price: float
    current_price: float
    pnl: float
    pnl_percentage: float


@dataclass
class OrderResult:
    """Result of an order placement"""
    success: bool
    order_id: Optional[str]
    message: str
    timestamp: datetime


class ZerodhaKite:
    """
    Zerodha Kite trading platform handler.
    
    Supports two modes:
    1. Kite API (official, recommended) - direct REST API calls
    2. Browser automation (fallback) - Playwright automation
    
    Mode selection via settings.trading.use_kite_api
    """
    
    KITE_URL = "https://kite.zerodha.com/"
    
    def __init__(self):
        self.config = settings.zerodha
        self.trading_config = settings.trading
        self.kite_config = settings.kite_api
        
        # Active index config — used for symbol construction (default NIFTY)
        self._active_index: IndexConfig = NIFTY

        # Kite API mode
        self.use_kite_api = settings.kite_api.use_kite_api
        self.kite = None
        if self.use_kite_api and KITECONNECT_AVAILABLE:
            logger.info("Initializing Kite API mode")
            try:
                if not self.kite_config.api_key:
                    raise ValueError("KITE_API_KEY is not set")
                self.kite = KiteConnect(api_key=self.kite_config.api_key)
                if self.kite_config.access_token:
                    self.kite.set_access_token(self.kite_config.access_token)
                logger.info("Kite API initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Kite API: {e}")
                self.kite = None

        # Browser automation mode
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logged_in: bool = False
        self._playwright = None
        
    def set_active_index(self, index_config: IndexConfig):
        """Set the active trading index used when building option symbols."""
        self._active_index = index_config
        logger.info(f"ZerodhaKite: active index set to {index_config.display_name}")

    async def initialize(self, headless: bool = False):
        """
        Initialize the browser instance.

        Args:
            headless: Run browser in headless mode (default: False for login visibility)
        """
        if self.use_kite_api and self.kite:
            logger.info("Kite API mode active — skipping browser initialization")
            return

        logger.info("Initializing browser...")

        self._playwright = await async_playwright().start()
        
        # Launch Chromium browser
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--start-maximized',
                '--window-size=1920,1080'
            ]
        )
        
        # Create browser context with viewport
        self._context = await self._browser.new_context(
            viewport=None,
            screen={'width': 1920, 'height': 1080},
            no_viewport=True,
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        # Create new page
        self._page = await self._context.new_page()

        try:
            await self._page.evaluate(
                """
                () => {
                    window.moveTo(0, 0);
                    window.resizeTo(screen.availWidth, screen.availHeight);
                }
                """
            )
        except Exception as e:
            logger.warning(f"Could not maximize browser window: {e}")
        
        logger.info("Browser initialized successfully")
        
    async def login(self, wait_for_2fa: bool = True) -> bool:
        """
        Login to Zerodha Kite.
        
        Args:
            wait_for_2fa: Wait for manual 2FA completion if TOTP not configured
            
        Returns:
            True if login successful
        """
        if not self._page:
            await self.initialize()
        
        logger.info("Navigating to Kite login page...")
        await self._page.goto(self.KITE_URL)
        
        # Wait for login form
        try:
            await self._page.wait_for_selector('input[type="text"]', timeout=10000)
        except:
            # Check if already logged in
            if await self._check_logged_in():
                return True
            raise Exception("Login page not found")
        
        # Enter user ID
        user_id = self.config.user_id
        if not user_id:
            logger.warning("Zerodha User ID not configured!")
            logger.info("Please enter your credentials manually in the browser")
            if wait_for_2fa:
                await self._wait_for_login()
                return self._logged_in
            return False
        
        logger.info(f"Entering User ID: {user_id[:2]}***")
        await self._page.fill('input[type="text"]', user_id)
        
        # Enter password
        password = self.config.password
        if password:
            await self._page.fill('input[type="password"]', password)
            
            # Click login button
            await self._page.click('button[type="submit"]')
            logger.info("Credentials submitted, waiting for 2FA page...")
            
            # Wait for 2FA page
            await asyncio.sleep(2)
            
            # Handle TOTP if configured
            if self.config.totp_secret and PYOTP_AVAILABLE:
                await self._handle_totp()
            elif wait_for_2fa:
                logger.info("Please complete 2FA manually in the browser...")
                await self._wait_for_login()
        else:
            logger.info("Password not configured. Please complete login manually.")
            if wait_for_2fa:
                await self._wait_for_login()
        
        return self._logged_in
    
    async def _handle_totp(self):
        """Handle automatic TOTP entry"""
        try:
            totp = pyotp.TOTP(self.config.totp_secret)
            otp = totp.now()
            
            logger.info("Entering TOTP...")
            
            # Wait for OTP input field
            await self._page.wait_for_selector('input[type="text"]', timeout=5000)
            await self._page.fill('input[type="text"]', otp)
            await self._page.click('button[type="submit"]')
            
            await self._wait_for_login(timeout=10)
            
        except Exception as e:
            logger.error(f"TOTP handling failed: {e}")
            logger.info("Please complete 2FA manually")
    
    async def _wait_for_login(self, timeout: int = 120):
        """Wait for login completion by checking for dashboard elements"""
        logger.info(f"Waiting up to {timeout}s for login completion...")
        
        start_time = datetime.now()
        while (datetime.now() - start_time).seconds < timeout:
            if await self._check_logged_in():
                self._logged_in = True
                logger.info("Login successful!")
                
                # Dismiss any modals that appear after login
                await self._dismiss_modals()
                
                return
            await asyncio.sleep(2)
        
        logger.warning("Login timeout - may not be fully logged in")
    
    async def _dismiss_modals(self):
        """Dismiss any modals that appear (e.g., risk disclosures)"""
        logger.info("Checking for modals to dismiss...")
        
        # Try multiple selectors for dismiss buttons
        modal_button_selectors = [
            'button:has-text("I understand")',      # Exact text match
            'button:has-text("I Understand")',
            'button:has-text("OK")',
            'button:has-text("Close")',
            'button:has-text("Accept")',
            'button[class*="understand"]',
            'button[class*="modal"] button[type="button"]',
            '.modal-footer button',
            '.modal button[type="button"]',
        ]
        
        for selector in modal_button_selectors:
            try:
                button = await self._page.query_selector(selector)
                if button:
                    button_text = await button.inner_text()
                    logger.info(f"Found modal dismiss button: {button_text}")
                    await button.click()
                    await asyncio.sleep(1)
                    logger.info("Modal dismissed")
                    return
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue
        
        logger.info("No modals found to dismiss")
    
    async def _check_logged_in(self) -> bool:
        """Check if we're logged into the dashboard"""
        try:
            current_url = self._page.url
            logger.debug(f"Current page URL: {current_url}")
            
            # Primary check: URL should not contain 'login' and should contain 'kite'
            if 'login' in current_url:
                return False
            
            if 'kite.zerodha.com' not in current_url:
                return False
            
            # Secondary check: Look for dashboard indicators
            # Try to access page content  
            page_text = await self._page.content()
            
            # Check for actual dashboard content (not error page)
            dashboard_keywords = ['holdings', 'portfolio', 'funds', 'watchlist', 'orders', 'positions']
            has_dashboard = any(keyword in page_text.lower() for keyword in dashboard_keywords)
            
            # Check for navigation/header elements
            nav_present = await self._page.query_selector('[class*="nav"], [class*="header"], [class*="sidebar"]')
            
            if has_dashboard or nav_present:
                logger.info("✅ Dashboard detected - logged in successfully")
                return True
            
            # If URL looks right and no error, assume logged in
            logger.info("✅ URL indicates dashboard - assuming logged in")
            return True
            
        except Exception as e:
            logger.debug(f"Login check error: {e}")
            return False
    
    async def navigate_to_options(self, symbol: str = "NIFTY"):
        """
        Navigate to options chain for given symbol.
        
        Args:
            symbol: Underlying symbol (default: NIFTY)
        """
        if not self._logged_in:
            logger.warning("Not logged in - attempting login first")
            await self.login()
        
        logger.info(f"Navigating to {symbol} options chain...")
        
        # Use Kite's search functionality
        try:
            # Click on search or use keyboard shortcut
            await self._page.keyboard.press('/')
            await asyncio.sleep(0.5)
            
            # Type symbol
            await self._page.keyboard.type(f"{symbol}", delay=50)
            await asyncio.sleep(1)
            
            logger.info("Options chain search initiated")
        except Exception as e:
            logger.error(f"Navigation error: {e}")
    
    async def place_order(
        self,
        option_type: OptionType,
        strike: int,
        order_type: OrderType = OrderType.BUY,
        quantity: int = None,
        expiry: str = None  # Format: "DDMMMYYYY" e.g., "25JAN2024"
    ) -> OrderResult:
        """
        Place an options order using Kite API or browser automation.
        
        Args:
            option_type: CE (Call) or PE (Put)
            strike: Strike price
            order_type: BUY or SELL
            quantity: Number of lots (uses default if not specified)
            expiry: Expiry date (uses weekly/monthly default if not specified)
            
        Returns:
            OrderResult with order details
        """
        quantity = quantity or self.trading_config.default_quantity
        
        # Route to appropriate method
        if self.use_kite_api and self.kite:
            logger.info("Using Kite API for order placement")
            return await self._place_order_kite_api(
                option_type, strike, order_type, quantity, expiry
            )
        else:
            logger.info("Using browser automation for order placement")
            return await self._place_order_browser(
                option_type, strike, order_type, quantity, expiry
            )
    
    async def get_instrument_price(self, symbol: str) -> Optional[float]:
        """
        Get current price of an instrument via Kite API.
        
        Args:
            symbol: Trading symbol (e.g., 'NIFTY2631724000CE')
            
        Returns:
            Current price or None if not available
        """
        if not self.kite:
            return None
        
        try:
            # Fetch quote for the instrument
            quote = await asyncio.to_thread(
                self.kite.quote,
                instruments=[symbol]
            )
            
            if quote and symbol in quote:
                # Return the last traded price
                ltp = quote[symbol].get('last_price')
                if ltp:
                    logger.debug(f"Got price for {symbol}: ₹{ltp}")
                    return ltp
        except Exception as e:
            logger.warning(f"Could not fetch price for {symbol}: {e}")
        
        return None
    
    async def get_holdings(self) -> List[Dict]:
        """
        Get current holdings/positions from Kite API.
        
        Returns:
            List of holding dictionaries
        """
        if not self.kite:
            return []
        
        try:
            holdings = await asyncio.to_thread(self.kite.holdings)
            return holdings if holdings else []
        except Exception as e:
            logger.warning(f"Could not fetch holdings: {e}")
            return []

    async def get_kite_positions(self) -> Dict:
        """
        Fetch today's positions from Kite API (open + closed).
        
        Returns:
            Dict with 'day' (all trades today) and 'net' (open positions) lists,
            plus 'total_pnl' summed from all day positions.
        """
        if not self.kite:
            return {"day": [], "net": [], "total_pnl": 0.0}
        
        try:
            data = await asyncio.to_thread(self.kite.positions)
            day_positions = data.get("day", [])
            net_positions = data.get("net", [])
            
            total_pnl = sum(p.get("pnl", 0) for p in day_positions)
            
            return {
                "day": day_positions,
                "net": net_positions,
                "total_pnl": total_pnl
            }
        except Exception as e:
            logger.warning(f"Could not fetch Kite positions: {e}")
            return {"day": [], "net": [], "total_pnl": 0.0}
    
    # Kite's single-character month codes for weekly NFO symbols (Oct=O, Nov=N, Dec=D)
    _MONTH_CODES = {1:'1',2:'2',3:'3',4:'4',5:'5',6:'6',7:'7',8:'8',9:'9',10:'O',11:'N',12:'D'}

    def _build_option_symbol(
        self,
        option_type: OptionType,
        strike: int,
        expiry: Optional[str] = None,
    ) -> str:
        """
        Build the NFO/BFO trading symbol Kite expects.
          Weekly  (NIFTY, SENSEX) : {NAME}{YY}{month_code}{DD}{strike}{CE|PE}
          Monthly (BANKNIFTY)     : {NAME}{DD}{MON}{YY}{strike}{CE|PE}
        """
        index = self._active_index
        if expiry:
            return f"{index.name}{expiry}{strike}{option_type.value}"

        today = datetime.now()
        if index.weekly_expiry:
            wd = index.expiry_weekday
            days_ahead = (wd - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            exp = today + timedelta(days=days_ahead)
            m_code = self._MONTH_CODES[exp.month]
            expiry_str = f"{exp.strftime('%y')}{m_code}{exp.strftime('%d')}"
        else:
            # Monthly: last occurrence of expiry_weekday in the month
            year, month = today.year, today.month
            exp = None
            for _ in range(2):  # try current month, then next
                last_day = calendar.monthrange(year, month)[1]
                for d in range(last_day, 0, -1):
                    if datetime(year, month, d).weekday() == index.expiry_weekday:
                        exp = datetime(year, month, d)
                        break
                if exp and exp.date() > today.date():
                    break
                month += 1
                if month > 12:
                    month, year = 1, year + 1
                exp = None
            expiry_str = exp.strftime("%d%b%y").upper()  # e.g., "25MAR26"

        return f"{index.name}{expiry_str}{strike}{option_type.value}"

    async def _place_order_kite_api(
        self,
        option_type: OptionType,
        strike: int,
        order_type: OrderType = OrderType.BUY,
        quantity: int = None,
        expiry: str = None
    ) -> OrderResult:
        """
        Place order via official Kite API.
        """
        if not self.kite:
            return OrderResult(
                success=False,
                order_id=None,
                message="Kite API not initialized",
                timestamp=datetime.now()
            )
        
        quantity = quantity or self.trading_config.default_quantity

        try:
            symbol = self._build_option_symbol(option_type, strike, expiry)
            # SENSEX trades on BFO (BSE F&O); all others use NFO
            exchange = "BFO" if self._active_index.exchange == "BSE" else "NFO"

            logger.info(f"Kite API: Placing {order_type.value} order for {symbol} x {quantity} on {exchange}")

            order_id = await asyncio.to_thread(
                self.kite.place_order,
                variety='regular',
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=order_type.value,
                quantity=quantity,
                product='MIS',
                order_type='MARKET',
            )
            
            logger.info(f"Kite API Order placed successfully: {order_id}")
            
            return OrderResult(
                success=True,
                order_id=str(order_id),
                message=f"Order placed via Kite API: {order_type.value} {symbol} x {quantity}",
                timestamp=datetime.now()
            )
            
        except Exception as e:
            error_msg = f"Kite API order placement failed: {str(e)}"
            logger.error(error_msg)
            logger.error(f"Exception type: {type(e).__name__}")
            
            return OrderResult(
                success=False,
                order_id=None,
                message=error_msg,
                timestamp=datetime.now()
            )
    
    async def _place_order_browser(
        self,
        option_type: OptionType,
        strike: int,
        order_type: OrderType = OrderType.BUY,
        quantity: int = None,
        expiry: str = None
    ) -> OrderResult:
        """
        Place order via browser automation (fallback).
        """
        if not self._logged_in:
            return OrderResult(
                success=False,
                order_id=None,
                message="Not logged in",
                timestamp=datetime.now()
            )
        
        quantity = quantity or self.trading_config.default_quantity
        
        # Build symbol name (e.g., "NIFTY24JAN23000CE")
        symbol = f"NIFTY{expiry or 'WEEKLY'}{strike}{option_type.value}"
        
        logger.info(f"Browser: Placing {order_type.value} order: {symbol} x {quantity}")
        
        try:
            # First verify we're logged in
            if not await self._check_logged_in():
                logger.error("Not properly logged in - cannot place order")
                return OrderResult(
                    success=False,
                    order_id=None,
                    message="Not logged into dashboard",
                    timestamp=datetime.now()
                )
            
            # Dismiss any modals that might be blocking the UI
            await self._dismiss_modals()
            await asyncio.sleep(1)
            
            # Search for the option
            logger.info("Opening search (press /)...")
            await self._page.keyboard.press('/')
            await asyncio.sleep(0.5)
            
            logger.info(f"Typing search: NIFTY {strike} {option_type.value}")
            await self._page.keyboard.type(f"NIFTY {strike} {option_type.value}", delay=30)
            await asyncio.sleep(2)  # Wait for autocomplete
            
            # Try multiple selector strategies for search results
            result_selectors = [
                '.search-results li',                # List items in search results
                '.search-results button',            # Buttons in search results
                '[class*="search"] li',              # List items with search class
                '._1pxc button',                     # Zerodha specific class
                'button[type="button"]',             # Any button
                'div[onclick]',                      # Clickable divs
                '.instrument',                       # Instrument class
            ]
            
            element_found = False
            for selector in result_selectors:
                try:
                    logger.info(f"Trying search result selector: {selector}")
                    element = await self._page.query_selector(selector)
                    if element:
                        logger.info(f"✅ Found element with selector: {selector}")
                        await element.click()
                        element_found = True
                        await asyncio.sleep(0.5)
                        break
                except Exception as e:
                    logger.debug(f"Selector {selector} failed: {e}")
                    continue
            
            if not element_found:
                logger.warning("Could not find search result element - trying Enter key")
                await self._page.keyboard.press('Enter')
                await asyncio.sleep(1)
            
            # Wait for order form to appear
            await asyncio.sleep(1)
            
            # Fill quantity (try multiple selectors)
            qty_selectors = [
                'input[type="number"][aria-label*="quantity"]',  # Zerodha specific
                'input[type="number"]',                          # First number input
            ]
            
            qty_filled = False
            for qty_sel in qty_selectors:
                try:
                    qty_inputs = await self._page.query_selector_all(qty_sel)
                    if qty_inputs:
                        # Use the first quantity input
                        await qty_inputs[0].fill(str(quantity))
                        logger.info(f"Set quantity to {quantity}")
                        qty_filled = True
                        break
                except Exception as e:
                    logger.debug(f"Quantity selector {qty_sel} failed: {e}")
                    continue
            
            if not qty_filled:
                logger.warning("Could not set quantity - proceeding with default")
            
            # Select MIS (intraday) product type for immediate exit
            try:
                mis_radio = await self._page.query_selector('input[name="product"][value="MIS"]')
                if mis_radio:
                    await mis_radio.click()
                    logger.info("Selected MIS (intraday) product")
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"Could not select MIS: {e}")
            
            # Select MARKET order type
            try:
                market_radio = await self._page.query_selector('input[name="orderType"][value="MARKET"]')
                if market_radio:
                    await market_radio.click()
                    logger.info("Selected MARKET order type")
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"Could not select MARKET: {e}")
            
            # Handle Buy/Sell toggle if needed
            if order_type == OrderType.SELL:
                try:
                    # If form is in BUY mode, toggle to SELL
                    buy_sell_toggle = await self._page.query_selector('input[type="checkbox"][class="su-switch"]')
                    if buy_sell_toggle:
                        current_state = await buy_sell_toggle.get_attribute('value')
                        if current_state == 'BUY':
                            await buy_sell_toggle.click()
                            logger.info("Toggled to SELL mode")
                            await asyncio.sleep(0.5)
                except Exception as e:
                    logger.debug(f"Could not toggle Buy/Sell: {e}")
            
            # Click submit button (Buy/Sell) - with robust waiting
            button_found = False
            
            # First, wait for the order window to be visible
            try:
                await self._page.wait_for_selector('form[role="dialog"]', timeout=5000)
                logger.info("Order form is visible")
            except Exception as e:
                logger.warning(f"Order form wait timeout: {e}")
                await self._page.screenshot(path=f"zerodha_no_form_{datetime.now().strftime('%H%M%S')}.png")
            
            # Wait for the submit button to be visible
            submit_selectors = [
                'button[type="submit"].submit',     # Zerodha specific
                'button.submit',                     # Class only
                'button[type="submit"]',             # Generic submit
                'footer button[type="submit"]',      # In footer
            ]
            
            for submit_sel in submit_selectors:
                try:
                    logger.info(f"Waiting for button with selector: {submit_sel}")
                    
                    # Wait for element to be visible
                    await self._page.wait_for_selector(submit_sel, timeout=3000)
                    submit_btn = await self._page.query_selector(submit_sel)
                    
                    if submit_btn:
                        # Check if button is actually visible and clickable
                        is_visible = await submit_btn.is_visible()
                        is_enabled = await submit_btn.is_enabled()
                        
                        logger.info(f"Button found - Visible: {is_visible}, Enabled: {is_enabled}")
                        
                        if is_visible and is_enabled:
                            button_text = await submit_btn.inner_text()
                            logger.info(f"✅ Clicking submit button: {button_text}")
                            
                            # Try to click with force if normal click fails
                            try:
                                await submit_btn.click()
                                button_found = True
                                logger.info("Button clicked successfully")
                                await asyncio.sleep(1)
                                break
                            except Exception as click_err:
                                logger.warning(f"Click failed, trying force click: {click_err}")
                                await submit_btn.click(force=True)
                                button_found = True
                                await asyncio.sleep(1)
                                break
                except asyncio.TimeoutError:
                    logger.debug(f"Timeout waiting for: {submit_sel}")
                    continue
                except Exception as e:
                    logger.debug(f"Error with selector {submit_sel}: {e}")
                    continue
            
            if not button_found:
                # Last resort: try using Playwright's locator API
                logger.info("Standard selectors failed - trying locator API")
                try:
                    # Get all buttons in footer
                    footer_buttons = await self._page.query_selector_all('footer button')
                    logger.info(f"Found {len(footer_buttons)} buttons in footer")
                    
                    for btn in footer_buttons:
                        try:
                            btn_type = await btn.get_attribute('type')
                            btn_text = await btn.inner_text()
                            btn_class = await btn.get_attribute('class')
                            logger.info(f"Button: type={btn_type}, text={btn_text}, class={btn_class}")
                            
                            if btn_type == 'submit':
                                logger.info(f"Found submit button via fallback: {btn_text}")
                                await btn.click()
                                button_found = True
                                await asyncio.sleep(1)
                                break
                        except Exception as btn_err:
                            logger.debug(f"Button iteration error: {btn_err}")
                            continue
                except Exception as e:
                    logger.warning(f"Locator API fallback failed: {e}")
            
            # Final screenshot and error if still not found
            if not button_found:
                logger.error("❌ Submit button not found after all attempts")
                await self._page.screenshot(path=f"zerodha_button_notfound_{datetime.now().strftime('%H%M%S')}.png")
                logger.error("Screenshot saved for debugging")
                raise Exception("Submit button not found - check zerodha_button_notfound_*.png")
            
            # Wait for order confirmation/result
            await asyncio.sleep(1)
            
            # Check for confirmation or error
            order_id = f"ORD_{datetime.now().strftime('%H%M%S')}"  # Order ID
            
            logger.info(f"✅ Order placed successfully: {order_id}")
            
            return OrderResult(
                success=True,
                order_id=order_id,
                message=f"Order placed: {order_type.value} {symbol} x {quantity}",
                timestamp=datetime.now()
            )
            
        except Exception as e:
            error_msg = f"Browser order placement failed: {str(e)}"
            logger.error(error_msg)
            
            # Take screenshot for debugging
            try:
                await self._page.screenshot(path=f"zerodha_error_{datetime.now().strftime('%H%M%S')}.png")
            except:
                pass
            
            return OrderResult(
                success=False,
                order_id=None,
                message=error_msg,
                timestamp=datetime.now()
            )
    
    async def get_positions(self) -> List[Position]:
        """
        Get current open positions via Kite API (preferred) or browser automation.

        Returns:
            List of Position objects
        """
        if self.use_kite_api and self.kite:
            data = await self.get_kite_positions()
            positions = []
            for p in data.get("net", []):
                qty = p.get("quantity", 0)
                if qty == 0:
                    continue
                buy_price = p.get("average_price", 0.0)
                current_price = p.get("last_price", 0.0)
                pnl = p.get("pnl", 0.0)
                positions.append(Position(
                    symbol=p.get("tradingsymbol", ""),
                    quantity=qty,
                    buy_price=buy_price,
                    current_price=current_price,
                    pnl=pnl,
                    pnl_percentage=(pnl / (buy_price * abs(qty))) * 100 if buy_price > 0 else 0.0,
                ))
            logger.info(f"Kite API: {len(positions)} open positions")
            return positions

        if not self._logged_in:
            logger.warning("Not logged in")
            return []

        positions = []

        try:
            # Navigate to positions page
            await self._page.click('[href="/positions"]')
            await asyncio.sleep(1)

            # Parse positions table
            # Note: Selectors need to match Kite's actual UI structure
            rows = await self._page.query_selector_all('.positions-table tbody tr')

            for row in rows:
                try:
                    cols = await row.query_selector_all('td')
                    if len(cols) >= 5:
                        symbol = await cols[0].inner_text()
                        qty = int(await cols[1].inner_text())
                        buy_price = float((await cols[2].inner_text()).replace(',', ''))
                        current = float((await cols[3].inner_text()).replace(',', ''))
                        pnl = float((await cols[4].inner_text()).replace(',', ''))

                        positions.append(Position(
                            symbol=symbol,
                            quantity=qty,
                            buy_price=buy_price,
                            current_price=current,
                            pnl=pnl,
                            pnl_percentage=(pnl / (buy_price * qty)) * 100 if buy_price > 0 else 0
                        ))
                except:
                    continue

            logger.info(f"Found {len(positions)} open positions")

        except Exception as e:
            logger.error(f"Error fetching positions: {e}")

        return positions
    
    async def close_position(self, symbol: str) -> OrderResult:
        """
        Close an existing position via Kite API or browser.

        Args:
            symbol: Full or partial trading symbol to close

        Returns:
            OrderResult
        """
        logger.info(f"Closing position: {symbol}")

        if self.use_kite_api and self.kite:
            data = await self.get_kite_positions()
            target = None
            for p in data.get("net", []):
                if symbol.upper() in p.get("tradingsymbol", "").upper() and p.get("quantity", 0) != 0:
                    target = p
                    break

            if not target:
                return OrderResult(
                    success=False,
                    order_id=None,
                    message=f"Position not found: {symbol}",
                    timestamp=datetime.now()
                )

            qty = abs(target["quantity"])
            txn = "SELL" if target["quantity"] > 0 else "BUY"
            exchange = target.get("exchange", "NFO")
            product = target.get("product", "MIS")

            try:
                order_id = await asyncio.to_thread(
                    self.kite.place_order,
                    variety="regular",
                    exchange=exchange,
                    tradingsymbol=target["tradingsymbol"],
                    transaction_type=txn,
                    quantity=qty,
                    product=product,
                    order_type="MARKET",
                )
                logger.info(f"Position closed via Kite API: order_id={order_id}")
                return OrderResult(
                    success=True,
                    order_id=str(order_id),
                    message=f"Closed {target['tradingsymbol']} x {qty}",
                    timestamp=datetime.now()
                )
            except Exception as e:
                error_msg = f"Kite API close_position failed: {e}"
                logger.error(error_msg)
                return OrderResult(
                    success=False,
                    order_id=None,
                    message=error_msg,
                    timestamp=datetime.now()
                )

        # Browser fallback — find position then place contra order
        positions = await self.get_positions()
        target_pos = None
        for pos in positions:
            if symbol.upper() in pos.symbol.upper():
                target_pos = pos
                break

        if not target_pos:
            return OrderResult(
                success=False,
                order_id=None,
                message=f"Position not found: {symbol}",
                timestamp=datetime.now()
            )

        close_order_type = OrderType.SELL if target_pos.quantity > 0 else OrderType.BUY
        opt_type = OptionType.PE if "PE" in symbol.upper() else OptionType.CE
        return await self._place_order_browser(
            option_type=opt_type,
            strike=0,
            order_type=close_order_type,
            quantity=abs(target_pos.quantity),
        )
    
    async def place_gtt(self, symbol: str, exchange: str, entry_price: float,
                         stop_loss_pct: float, quantity: int) -> Optional[int]:
        """
        Place a GTT (Good Till Triggered) single-leg stop-loss order on Kite.
        This order lives on the exchange and fires even if the bot is offline.

        Args:
            symbol:         Full trading symbol e.g. 'NIFTY26317CE24000'
            exchange:       'NFO' or 'BFO'
            entry_price:    Actual fill price of the entry order
            stop_loss_pct:  Stop loss % below entry (e.g. 15.0 for 15%)
            quantity:       Number of units to sell on trigger

        Returns:
            GTT order ID (int) on success, None on failure
        """
        if not self.kite:
            logger.warning("GTT: Kite API not available — skipping GTT placement")
            return None

        trigger_price = round(entry_price * (1 - stop_loss_pct / 100), 1)
        # Kite requires limit price slightly below trigger to guarantee fill
        limit_price = round(trigger_price * 0.98, 1)

        gtt_params = {
            "trigger_type": self.kite.GTT_TYPE_SINGLE,
            "tradingsymbol": symbol,
            "exchange": exchange,
            "trigger_values": [trigger_price],
            "last_price": entry_price,
            "orders": [{
                "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
                "quantity": quantity,
                "product": self.kite.PRODUCT_MIS,
                "order_type": self.kite.ORDER_TYPE_LIMIT,
                "price": limit_price,
            }]
        }

        try:
            result = await asyncio.to_thread(self.kite.place_gtt, **gtt_params)
            gtt_id = result.get("trigger_id")
            logger.info(
                f"GTT placed: id={gtt_id} | {symbol} | trigger=₹{trigger_price} "
                f"(SL {stop_loss_pct}% below ₹{entry_price})"
            )
            return gtt_id
        except Exception as e:
            logger.error(f"GTT placement failed for {symbol}: {e}")
            return None

    async def cancel_gtt(self, gtt_id: int) -> bool:
        """
        Cancel an existing GTT order by ID.

        Args:
            gtt_id: The trigger_id returned by place_gtt()

        Returns:
            True if cancelled successfully
        """
        if not self.kite or not gtt_id:
            return False
        try:
            await asyncio.to_thread(self.kite.delete_gtt, gtt_id)
            logger.info(f"GTT cancelled: id={gtt_id}")
            return True
        except Exception as e:
            logger.warning(f"GTT cancel failed (id={gtt_id}): {e}")
            return False

    async def get_screenshot(self, path: str = "kite_screenshot.png"):
        """Take a screenshot of current browser state"""
        if self._page:
            await self._page.screenshot(path=path)
            logger.info(f"Screenshot saved: {path}")
    
    async def close(self):
        """Close the browser and cleanup"""
        logger.info("Closing browser...")
        
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        
        self._logged_in = False
        logger.info("Browser closed")
    
    def is_logged_in(self) -> bool:
        """Check if currently logged in (browser or Kite API mode)"""
        if self.use_kite_api and self.kite:
            return True
        return self._logged_in

    async def get_fund_limits(self) -> dict:
        """
        Get available funds and margin from Zerodha/Kite account.
        Returns dict with: available_balance, used_margin, available_margin
        """
        if not self.use_kite_api or not self.kite:
            return {"available_balance": 0.0, "used_margin": 0.0, "available_margin": 0.0}
        try:
            margins = await asyncio.to_thread(self.kite.margins)
            equity = margins.get("equity", {})
            available_cash = float(equity.get("available", {}).get("cash", 0) or 0)
            used_margin = float(equity.get("utilised", {}).get("debits", 0) or 0)
            available_margin = float(equity.get("available", {}).get("live_balance", 0) or 0)
            
            return {
                "available_balance": round(available_cash, 2),
                "used_margin": round(used_margin, 2),
                "available_margin": round(available_margin, 2)
            }
        except Exception as e:
            logger.warning(f"Zerodha get_fund_limits failed: {e}")
            return {"available_balance": 0.0, "used_margin": 0.0, "available_margin": 0.0}


# Standalone testing
async def test_browser():
    """Test browser automation"""
    kite = ZerodhaKite()
    
    try:
        await kite.initialize(headless=False)
        print("Browser initialized. Please login manually...")
        
        # Wait for manual login
        success = await kite.login(wait_for_2fa=True)
        
        if success:
            print("Login successful!")
            
            # Test navigation
            await kite.navigate_to_options("NIFTY")
            
            # Take screenshot
            await kite.get_screenshot()
            
            # Get positions
            positions = await kite.get_positions()
            print(f"Open positions: {len(positions)}")
            
            # Keep browser open for inspection
            input("Press Enter to close browser...")
        else:
            print("Login failed or timed out")
            
    finally:
        await kite.close()


if __name__ == "__main__":
    asyncio.run(test_browser())
