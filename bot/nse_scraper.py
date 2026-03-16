"""
NSE Option Chain Scraper

Fetches real-time option chain data from NSE India website.
Uses Playwright (headless browser) to bypass Akamai bot protection,
with a requests-based fallback.

Provides:
- Real Open Interest for each strike
- Actual PCR (Put-Call Ratio)
- Real LTP (Last Traded Price) for options
- IV (Implied Volatility)
- Change in OI
- Max Pain calculation
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from loguru import logger
import time
import json
import random

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


@dataclass
class OptionData:
    """Data for a single option contract"""
    strike: int
    option_type: str  # CE or PE
    ltp: float  # Last Traded Price
    open_interest: int
    change_in_oi: int
    volume: int
    iv: float  # Implied Volatility
    bid_price: float
    ask_price: float
    bid_qty: int
    ask_qty: int


@dataclass
class OptionChainData:
    """Complete option chain data"""
    symbol: str
    spot_price: float
    timestamp: datetime
    expiry_date: str
    
    # All options data
    calls: List[OptionData]
    puts: List[OptionData]
    
    # Derived metrics
    total_call_oi: int
    total_put_oi: int
    pcr_ratio: float  # Put-Call Ratio based on OI
    max_call_oi_strike: int  # Resistance
    max_put_oi_strike: int   # Support
    max_pain: int
    
    # ATM info
    atm_strike: int
    atm_call_iv: float
    atm_put_iv: float


class NSEOptionChainScraper:
    """
    Scraper for NSE India option chain data.
    
    Features:
    - Fetches real option chain data from NSE
    - Handles session management and headers
    - Implements caching to avoid rate limiting
    - Calculates derived metrics (PCR, Max Pain, etc.)
    """
    
    BASE_URL = "https://www.nseindia.com"
    OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices"
    LIVE_DERIV_URL = "https://www.nseindia.com/api/liveEquity-derivatives"
    
    _USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    ]
    
    HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }
    
    def __init__(self, cache_duration: int = 300):
        """
        Initialize NSE scraper.
        
        Args:
            cache_duration: Cache duration in seconds (default 5 minutes)
        """
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.session.headers["User-Agent"] = random.choice(self._USER_AGENTS)
        self._cookies_set = False
        self._cookie_time: Optional[datetime] = None
        self._last_fetch_time: Dict[str, datetime] = {}
        self._cache: Dict[str, OptionChainData] = {}
        self._cache_duration = cache_duration
        self._max_retries = 3
    
    def _set_cookies(self) -> bool:
        """
        Set cookies by visiting the main NSE page first.
        NSE requires cookies from the main page before API access.
        Rotates User-Agent on each attempt and adds a small delay.
        """
        for attempt in range(self._max_retries):
            try:
                self.session.cookies.clear()
                self.session.headers["User-Agent"] = random.choice(self._USER_AGENTS)
                
                time.sleep(random.uniform(0.5, 1.5))
                
                response = self.session.get(
                    self.BASE_URL,
                    timeout=15,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    self._cookies_set = True
                    self._cookie_time = datetime.now()
                    logger.info(f"NSE cookies set successfully (attempt {attempt + 1})")
                    return True
                else:
                    logger.warning(f"NSE cookie attempt {attempt + 1}: HTTP {response.status_code}")
                    time.sleep(2 ** attempt)
            except Exception as e:
                logger.warning(f"NSE cookie attempt {attempt + 1} error: {e}")
                time.sleep(2 ** attempt)
        
        logger.error("Failed to get NSE cookies after all retries")
        return False
    
    def _cookies_expired(self) -> bool:
        """NSE cookies typically expire after ~5 minutes."""
        if not self._cookie_time:
            return True
        return (datetime.now() - self._cookie_time).total_seconds() > 270
    
    def _fetch_live_derivatives(self, symbol: str = "NIFTY") -> Optional[Dict]:
        """
        Fetch from the liveEquity-derivatives endpoint which is less protected
        than the option-chain-indices endpoint. Uses curl_cffi for TLS impersonation.
        Falls back to standard requests if curl_cffi is unavailable.
        """
        from bot.index_config import get_index
        idx = get_index(symbol)
        if idx and idx.nse_deriv_param:
            index_param = idx.nse_deriv_param
        else:
            param_map = {"NIFTY": "nse50_opt", "BANKNIFTY": "nifty_bank_opt"}
            index_param = param_map.get(symbol, "nse50_opt")
        api_url = f"{self.LIVE_DERIV_URL}?index={index_param}"
        
        if CURL_CFFI_AVAILABLE:
            try:
                sess = cffi_requests.Session(impersonate="chrome131")
                sess.get(self.BASE_URL, timeout=15)
                time.sleep(random.uniform(0.8, 1.5))
                
                r = sess.get(
                    api_url,
                    headers={"Referer": "https://www.nseindia.com/"},
                    timeout=15,
                )
                if r.status_code == 200 and len(r.text) > 100:
                    data = r.json()
                    if data.get("data"):
                        logger.info(f"Fetched {len(data['data'])} derivatives rows via curl_cffi")
                        return data
            except Exception as e:
                logger.warning(f"curl_cffi live-derivatives error: {e}")
        
        # Fallback to standard requests
        try:
            if not self._cookies_set or self._cookies_expired():
                self._set_cookies()
            
            r = self.session.get(
                api_url,
                headers={"Referer": "https://www.nseindia.com/"},
                timeout=15,
            )
            if r.status_code == 200 and len(r.text) > 100:
                data = r.json()
                if data.get("data"):
                    logger.info(f"Fetched {len(data['data'])} derivatives rows via requests")
                    return data
        except Exception as e:
            logger.warning(f"requests live-derivatives error: {e}")
        
        return None
    
    def _parse_live_derivatives(
        self,
        raw_data: Dict,
        symbol: str,
        target_expiry: str = None,
    ) -> Optional[OptionChainData]:
        """Parse the liveEquity-derivatives response into OptionChainData."""
        try:
            rows = raw_data.get("data", [])
            if not rows:
                return None
            
            nifty_rows = [r for r in rows if r.get("underlying") == symbol]
            if not nifty_rows:
                nifty_rows = rows
            
            spot_price = nifty_rows[0].get("underlyingValue", 0) if nifty_rows else 0
            
            expiry_dates = sorted(set(r.get("expiryDate", "") for r in nifty_rows if r.get("expiryDate")))
            
            if target_expiry and target_expiry in expiry_dates:
                selected_expiry = target_expiry
            else:
                selected_expiry = expiry_dates[0] if expiry_dates else None
            
            if not selected_expiry:
                logger.error("No expiry dates in live derivatives data")
                return None
            
            calls: List[OptionData] = []
            puts: List[OptionData] = []
            
            for row in nifty_rows:
                if row.get("expiryDate") != selected_expiry:
                    continue
                
                strike = row.get("strikePrice", 0)
                opt_type_raw = row.get("optionType", "").strip()
                if opt_type_raw == "Call":
                    opt_type = "CE"
                elif opt_type_raw == "Put":
                    opt_type = "PE"
                else:
                    continue
                
                od = OptionData(
                    strike=int(strike),
                    option_type=opt_type,
                    ltp=row.get("lastPrice", 0),
                    open_interest=row.get("openInterest", 0),
                    change_in_oi=0,
                    volume=row.get("volume", 0),
                    iv=0,
                    bid_price=0,
                    ask_price=0,
                    bid_qty=0,
                    ask_qty=0,
                )
                
                if opt_type == "CE":
                    calls.append(od)
                else:
                    puts.append(od)
            
            total_call_oi = sum(c.open_interest for c in calls)
            total_put_oi = sum(p.open_interest for p in puts)
            pcr_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0
            
            max_call_oi_strike = max(calls, key=lambda x: x.open_interest).strike if calls else 0
            max_put_oi_strike = max(puts, key=lambda x: x.open_interest).strike if puts else 0
            
            from bot.index_config import get_index
            idx_cfg = get_index(symbol)
            strike_interval = idx_cfg.strike_interval if idx_cfg else 50
            atm_strike = round(spot_price / strike_interval) * strike_interval
            
            atm_call = next((c for c in calls if c.strike == atm_strike), None)
            atm_put = next((p for p in puts if p.strike == atm_strike), None)
            
            max_pain = self._calculate_max_pain(calls, puts, spot_price)
            
            return OptionChainData(
                symbol=symbol,
                spot_price=spot_price,
                timestamp=datetime.now(),
                expiry_date=selected_expiry,
                calls=calls,
                puts=puts,
                total_call_oi=total_call_oi,
                total_put_oi=total_put_oi,
                pcr_ratio=pcr_ratio,
                max_call_oi_strike=max_call_oi_strike,
                max_put_oi_strike=max_put_oi_strike,
                max_pain=max_pain,
                atm_strike=atm_strike,
                atm_call_iv=atm_call.iv if atm_call else 0,
                atm_put_iv=atm_put.iv if atm_put else 0,
            )
        except Exception as e:
            logger.error(f"Error parsing live derivatives: {e}")
            return None
    
    def _fetch_via_curl_cffi(self, symbol: str = "NIFTY") -> Optional[Dict]:
        """
        Use curl_cffi to fetch the option-chain-indices endpoint.
        Kept as fallback in case NSE unblocks it.
        """
        if not CURL_CFFI_AVAILABLE:
            return None
        
        try:
            sess = cffi_requests.Session(impersonate="chrome131")
            sess.get(self.BASE_URL, timeout=15)
            time.sleep(random.uniform(1.0, 2.0))
            
            api_url = f"{self.OPTION_CHAIN_URL}?symbol={symbol}"
            r = sess.get(
                api_url,
                headers={
                    "Referer": "https://www.nseindia.com/option-chain",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("records", {}).get("expiryDates"):
                    logger.info("Fetched option chain via curl_cffi")
                    return data
            return None
        except Exception:
            return None
    
    def _fetch_via_playwright(self, symbol: str = "NIFTY") -> Optional[Dict]:
        """
        Use a real headless browser to fetch NSE option chain data.
        Intercepts the XHR response from the actual option chain page.
        """
        if not PLAYWRIGHT_AVAILABLE:
            logger.debug("Playwright not available for NSE fetch")
            return None
        
        pw = None
        browser = None
        captured_data = {}
        
        def handle_response(response):
            if "option-chain-indices" in response.url and response.status == 200:
                try:
                    captured_data["json"] = response.json()
                except Exception:
                    pass
        
        try:
            pw = sync_playwright().start()
            # Firefox has a different TLS fingerprint that Akamai may not block
            browser = pw.firefox.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="en-IN",
                java_script_enabled=True,
            )
            
            page = context.new_page()
            page.on("response", handle_response)
            
            # Navigate to the option chain page; the page itself fires the API call
            page.goto(
                "https://www.nseindia.com/option-chain",
                wait_until="networkidle",
                timeout=30000,
            )
            page.wait_for_timeout(3000)
            
            if "json" in captured_data:
                data = captured_data["json"]
                records = data.get("records", {})
                if records.get("expiryDates"):
                    logger.info("Captured NSE option chain data from browser XHR")
                    return data
                else:
                    logger.warning("Playwright captured response but records are empty")
            
            # Fallback: try a direct fetch from the page context
            api_url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            resp = page.evaluate(f"""
                async () => {{
                    const r = await fetch('{api_url}');
                    return await r.json();
                }}
            """)
            if resp and resp.get("records", {}).get("expiryDates"):
                logger.info("Captured NSE data via page.evaluate fetch")
                return resp
            
            logger.warning("Playwright browser could not get valid NSE data")
            return None
                
        except Exception as e:
            logger.warning(f"Playwright NSE fetch error: {e}")
            return None
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass
    
    def _is_cache_valid(self, symbol: str, expiry: str) -> bool:
        """Check if cached data is still valid"""
        cache_key = f"{symbol}_{expiry}"
        if cache_key not in self._last_fetch_time:
            return False
        
        elapsed = (datetime.now() - self._last_fetch_time[cache_key]).total_seconds()
        return elapsed < self._cache_duration
    
    def fetch_option_chain(
        self, 
        symbol: str = "NIFTY",
        expiry_date: str = None
    ) -> Optional[OptionChainData]:
        """
        Fetch option chain data from NSE.
        
        Args:
            symbol: Index symbol (NIFTY, BANKNIFTY, etc.)
            expiry_date: Specific expiry date (format: DD-Mon-YYYY) or None for nearest
            
        Returns:
            OptionChainData or None if fetch fails
        """
        cache_key = f"{symbol}_{expiry_date or 'nearest'}"
        
        # Check cache first
        if self._is_cache_valid(symbol, expiry_date or 'nearest'):
            logger.debug(f"Returning cached data for {cache_key}")
            return self._cache.get(cache_key)
        
        # PRIMARY: liveEquity-derivatives endpoint (bypasses Akamai block)
        logger.info(f"Fetching NSE option chain for {symbol}...")
        live_data = self._fetch_live_derivatives(symbol)
        if live_data:
            option_chain = self._parse_live_derivatives(live_data, symbol, expiry_date)
            if option_chain:
                self._cache[cache_key] = option_chain
                self._last_fetch_time[cache_key] = datetime.now()
                logger.info(
                    f"NSE LIVE: Spot={option_chain.spot_price}, "
                    f"PCR={option_chain.pcr_ratio:.2f}, MaxPain={option_chain.max_pain}, "
                    f"CE={len(option_chain.calls)}, PE={len(option_chain.puts)}"
                )
                return option_chain
        
        # FALLBACK: option-chain-indices via curl_cffi (may work if NSE loosens protection)
        logger.info("Live derivatives failed, trying option-chain-indices...")
        oc_data = self._fetch_via_curl_cffi(symbol)
        if oc_data:
            option_chain = self._parse_option_chain(oc_data, symbol, expiry_date)
            if option_chain:
                self._cache[cache_key] = option_chain
                self._last_fetch_time[cache_key] = datetime.now()
                logger.info(f"NSE OC: Spot={option_chain.spot_price}, PCR={option_chain.pcr_ratio:.2f}")
                return option_chain
        
        logger.error("All NSE fetch methods failed")
        return None
    
    def _parse_option_chain(
        self, 
        data: Dict, 
        symbol: str,
        target_expiry: str = None
    ) -> Optional[OptionChainData]:
        """Parse NSE API response into OptionChainData"""
        try:
            records = data.get("records", {})
            filtered_data = data.get("filtered", {})
            
            # Get spot price
            spot_price = records.get("underlyingValue", 0)
            if spot_price == 0:
                spot_price = filtered_data.get("CE", [{}])[0].get("underlyingValue", 0) if filtered_data.get("CE") else 0
            
            # Get available expiry dates
            expiry_dates = records.get("expiryDates", [])
            
            # Select expiry date
            if target_expiry and target_expiry in expiry_dates:
                selected_expiry = target_expiry
            else:
                # Use nearest expiry
                selected_expiry = expiry_dates[0] if expiry_dates else None
            
            if not selected_expiry:
                logger.error("No expiry dates available")
                return None
            
            # Parse option data
            all_data = records.get("data", [])
            
            calls: List[OptionData] = []
            puts: List[OptionData] = []
            
            for item in all_data:
                # Filter by expiry date
                if item.get("expiryDate") != selected_expiry:
                    continue
                
                strike = item.get("strikePrice", 0)
                
                # Parse CE (Call) data
                ce_data = item.get("CE", {})
                if ce_data:
                    calls.append(OptionData(
                        strike=strike,
                        option_type="CE",
                        ltp=ce_data.get("lastPrice", 0),
                        open_interest=ce_data.get("openInterest", 0),
                        change_in_oi=ce_data.get("changeinOpenInterest", 0),
                        volume=ce_data.get("totalTradedVolume", 0),
                        iv=ce_data.get("impliedVolatility", 0),
                        bid_price=ce_data.get("bidprice", 0),
                        ask_price=ce_data.get("askPrice", 0),
                        bid_qty=ce_data.get("bidQty", 0),
                        ask_qty=ce_data.get("askQty", 0)
                    ))
                
                # Parse PE (Put) data
                pe_data = item.get("PE", {})
                if pe_data:
                    puts.append(OptionData(
                        strike=strike,
                        option_type="PE",
                        ltp=pe_data.get("lastPrice", 0),
                        open_interest=pe_data.get("openInterest", 0),
                        change_in_oi=pe_data.get("changeinOpenInterest", 0),
                        volume=pe_data.get("totalTradedVolume", 0),
                        iv=pe_data.get("impliedVolatility", 0),
                        bid_price=pe_data.get("bidprice", 0),
                        ask_price=pe_data.get("askPrice", 0),
                        bid_qty=pe_data.get("bidQty", 0),
                        ask_qty=pe_data.get("askQty", 0)
                    ))
            
            # Calculate derived metrics
            total_call_oi = sum(c.open_interest for c in calls)
            total_put_oi = sum(p.open_interest for p in puts)
            
            pcr_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0
            
            # Max OI strikes (support/resistance)
            max_call_oi_strike = max(calls, key=lambda x: x.open_interest).strike if calls else 0
            max_put_oi_strike = max(puts, key=lambda x: x.open_interest).strike if puts else 0
            
            from bot.index_config import get_index
            idx_cfg = get_index(symbol)
            strike_interval = idx_cfg.strike_interval if idx_cfg else 50
            atm_strike = round(spot_price / strike_interval) * strike_interval
            
            atm_call = next((c for c in calls if c.strike == atm_strike), None)
            atm_put = next((p for p in puts if p.strike == atm_strike), None)
            atm_call_iv = atm_call.iv if atm_call else 0
            atm_put_iv = atm_put.iv if atm_put else 0
            
            # Calculate Max Pain
            max_pain = self._calculate_max_pain(calls, puts, spot_price)
            
            return OptionChainData(
                symbol=symbol,
                spot_price=spot_price,
                timestamp=datetime.now(),
                expiry_date=selected_expiry,
                calls=calls,
                puts=puts,
                total_call_oi=total_call_oi,
                total_put_oi=total_put_oi,
                pcr_ratio=pcr_ratio,
                max_call_oi_strike=max_call_oi_strike,
                max_put_oi_strike=max_put_oi_strike,
                max_pain=max_pain,
                atm_strike=atm_strike,
                atm_call_iv=atm_call_iv,
                atm_put_iv=atm_put_iv
            )
            
        except Exception as e:
            logger.error(f"Error parsing option chain: {e}")
            return None
    
    def _calculate_max_pain(
        self, 
        calls: List[OptionData], 
        puts: List[OptionData],
        spot_price: float
    ) -> int:
        """
        Calculate Max Pain - the strike where option writers have minimum loss.
        This is often where the index tends to expire.
        """
        if not calls or not puts:
            return round(spot_price / 50) * 50
        
        strikes = sorted(set(c.strike for c in calls) | set(p.strike for p in puts))
        
        min_pain = float('inf')
        max_pain_strike = strikes[len(strikes) // 2]
        
        for strike in strikes:
            # Calculate total pain at this strike
            call_pain = sum(
                c.open_interest * max(0, strike - c.strike) 
                for c in calls
            )
            put_pain = sum(
                p.open_interest * max(0, p.strike - strike)
                for p in puts
            )
            total_pain = call_pain + put_pain
            
            if total_pain < min_pain:
                min_pain = total_pain
                max_pain_strike = strike
        
        return max_pain_strike
    
    def get_option_data(
        self, 
        strike: int, 
        option_type: str,
        symbol: str = "NIFTY",
        expiry_date: str = None
    ) -> Optional[OptionData]:
        """
        Get data for a specific option contract.
        
        Args:
            strike: Strike price
            option_type: 'CE' or 'PE'
            symbol: Index symbol
            expiry_date: Expiry date or None for nearest
            
        Returns:
            OptionData for the specific contract
        """
        chain = self.fetch_option_chain(symbol, expiry_date)
        if not chain:
            return None
        
        options = chain.calls if option_type.upper() == "CE" else chain.puts
        
        for opt in options:
            if opt.strike == strike:
                return opt
        
        return None
    
    def get_support_resistance(
        self, 
        symbol: str = "NIFTY",
        expiry_date: str = None,
        num_levels: int = 3
    ) -> Dict[str, List[int]]:
        """
        Get support and resistance levels based on OI.
        
        Returns:
            Dict with 'support' and 'resistance' lists
        """
        chain = self.fetch_option_chain(symbol, expiry_date)
        if not chain:
            return {"support": [], "resistance": []}
        
        # Sort calls by OI to find resistance levels
        sorted_calls = sorted(chain.calls, key=lambda x: x.open_interest, reverse=True)
        resistance = [c.strike for c in sorted_calls[:num_levels] if c.strike > chain.spot_price]
        
        # Sort puts by OI to find support levels
        sorted_puts = sorted(chain.puts, key=lambda x: x.open_interest, reverse=True)
        support = [p.strike for p in sorted_puts[:num_levels] if p.strike < chain.spot_price]
        
        # Sort levels
        resistance.sort()
        support.sort(reverse=True)
        
        return {
            "support": support[:num_levels],
            "resistance": resistance[:num_levels]
        }


# Global instance
nse_scraper = NSEOptionChainScraper()


# Standalone testing
if __name__ == "__main__":
    import sys
    from loguru import logger
    
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    
    scraper = NSEOptionChainScraper()
    
    print("Fetching NIFTY option chain...")
    chain = scraper.fetch_option_chain("NIFTY")
    
    if chain:
        print(f"\n=== NIFTY Option Chain ===")
        print(f"Spot Price: {chain.spot_price}")
        print(f"Expiry: {chain.expiry_date}")
        print(f"ATM Strike: {chain.atm_strike}")
        print(f"PCR Ratio: {chain.pcr_ratio:.2f}")
        print(f"Max Call OI (Resistance): {chain.max_call_oi_strike}")
        print(f"Max Put OI (Support): {chain.max_put_oi_strike}")
        print(f"Max Pain: {chain.max_pain}")
        print(f"ATM Call IV: {chain.atm_call_iv}%")
        print(f"ATM Put IV: {chain.atm_put_iv}%")
        
        # Get support/resistance
        levels = scraper.get_support_resistance()
        print(f"\nSupport Levels: {levels['support']}")
        print(f"Resistance Levels: {levels['resistance']}")
    else:
        print("Failed to fetch option chain")
