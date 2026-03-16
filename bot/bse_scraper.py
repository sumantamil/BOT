"""
BSE Option Chain Scraper for SENSEX

Attempts to fetch SENSEX option chain data from BSE India.
BSE does not expose a clean public JSON API like NSE, so this uses
best-effort scraping with a generous fallback to estimated data.

Returns the same OptionChainData dataclass used by the NSE scraper
so the rest of the system is agnostic to the data source.
"""

import time
import random
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from loguru import logger

from bot.nse_scraper import OptionData, OptionChainData

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

import requests


class BSEOptionChainScraper:
    """
    Scraper for BSE India SENSEX option chain data.

    Strategy:
    1. Try the BSE derivatives API endpoints with curl_cffi
    2. Fall back to estimated data built from Yahoo Finance spot price
    """

    BSE_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
    BSE_HOME = "https://www.bseindia.com"

    def __init__(self, cache_duration: int = 300):
        self._cache: Dict[str, OptionChainData] = {}
        self._cache_time: Dict[str, datetime] = {}
        self._cache_duration = cache_duration

    def _is_cache_valid(self, key: str) -> bool:
        if key not in self._cache_time:
            return False
        return (datetime.now() - self._cache_time[key]).total_seconds() < self._cache_duration

    def fetch_option_chain(
        self,
        symbol: str = "SENSEX",
        expiry_date: str = None,
    ) -> Optional[OptionChainData]:
        cache_key = f"{symbol}_{expiry_date or 'nearest'}"
        if self._is_cache_valid(cache_key):
            return self._cache.get(cache_key)

        chain = self._try_bse_api(symbol, expiry_date)

        if chain:
            self._cache[cache_key] = chain
            self._cache_time[cache_key] = datetime.now()
            logger.info(
                f"BSE LIVE: Spot={chain.spot_price}, PCR={chain.pcr_ratio:.2f}, "
                f"CE={len(chain.calls)}, PE={len(chain.puts)}"
            )
        else:
            logger.warning("BSE scraper could not fetch option chain; falling back to estimates")

        return chain

    def _try_bse_api(self, symbol: str, expiry_date: str = None) -> Optional[OptionChainData]:
        """Attempt to fetch from BSE API endpoints."""
        if not CURL_CFFI_AVAILABLE:
            return None

        try:
            sess = cffi_requests.Session(impersonate="chrome131")
            sess.get(self.BSE_HOME, timeout=15)
            time.sleep(random.uniform(0.5, 1.0))

            # BSE derivatives live data endpoint
            url = f"{self.BSE_BASE}/MktRptsLiveDerivatives/w"
            params = {"Ession_Type": symbol, "ExpDate": expiry_date or "", "Strike_price": ""}
            r = sess.get(url, params=params, headers={"Referer": self.BSE_HOME}, timeout=15)

            if r.status_code == 200 and len(r.text) > 100:
                try:
                    data = r.json()
                    if isinstance(data, list) and data:
                        return self._parse_bse_data(data, symbol)
                except Exception:
                    pass

            # Fallback: option chain endpoint
            url2 = f"{self.BSE_BASE}/OptionChainDeri/w"
            params2 = {"Ession_Type": symbol, "ExpDate": "", "Strike_price": ""}
            r2 = sess.get(url2, params=params2, headers={"Referer": self.BSE_HOME}, timeout=15)

            if r2.status_code == 200 and len(r2.text) > 100:
                try:
                    data2 = r2.json()
                    if isinstance(data2, list) and data2:
                        return self._parse_bse_data(data2, symbol)
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"BSE API error: {e}")

        return None

    def _parse_bse_data(self, rows: list, symbol: str) -> Optional[OptionChainData]:
        """Parse BSE API response rows into OptionChainData."""
        try:
            calls: List[OptionData] = []
            puts: List[OptionData] = []
            spot_price = 0
            selected_expiry = ""

            for row in rows:
                if not isinstance(row, dict):
                    continue

                strike = row.get("Strike_Price") or row.get("StrikePrice") or row.get("strikePrice")
                if strike is None:
                    continue
                strike = int(float(strike))

                opt_type = row.get("CE_PE") or row.get("OptionType") or ""
                ltp = float(row.get("Last_Trd_Price") or row.get("LTP") or row.get("lastPrice") or 0)
                oi = int(float(row.get("Open_Interest") or row.get("OI") or row.get("openInterest") or 0))
                volume = int(float(row.get("Vol_Traded") or row.get("volume") or 0))
                iv = float(row.get("IV") or row.get("impliedVolatility") or 0)

                if not selected_expiry:
                    selected_expiry = str(row.get("Expiry_Date") or row.get("expiryDate") or "")

                if not spot_price:
                    spot_price = float(row.get("underlyingValue") or row.get("Spot") or 0)

                od = OptionData(
                    strike=strike,
                    option_type="CE" if "C" in opt_type.upper() else "PE",
                    ltp=ltp,
                    open_interest=oi,
                    change_in_oi=0,
                    volume=volume,
                    iv=iv,
                    bid_price=0, ask_price=0, bid_qty=0, ask_qty=0,
                )

                if od.option_type == "CE":
                    calls.append(od)
                else:
                    puts.append(od)

            if not calls and not puts:
                return None

            total_call_oi = sum(c.open_interest for c in calls)
            total_put_oi = sum(p.open_interest for p in puts)
            pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

            max_call_oi_strike = max(calls, key=lambda x: x.open_interest).strike if calls else 0
            max_put_oi_strike = max(puts, key=lambda x: x.open_interest).strike if puts else 0

            from bot.index_config import get_index
            idx = get_index(symbol)
            si = idx.strike_interval if idx else 100
            atm = round(spot_price / si) * si if spot_price else 0

            atm_call = next((c for c in calls if c.strike == atm), None)
            atm_put = next((p for p in puts if p.strike == atm), None)

            from bot.nse_scraper import nse_scraper
            max_pain = nse_scraper._calculate_max_pain(calls, puts, spot_price) if spot_price else atm

            return OptionChainData(
                symbol=symbol,
                spot_price=spot_price,
                timestamp=datetime.now(),
                expiry_date=selected_expiry,
                calls=calls,
                puts=puts,
                total_call_oi=total_call_oi,
                total_put_oi=total_put_oi,
                pcr_ratio=pcr,
                max_call_oi_strike=max_call_oi_strike,
                max_put_oi_strike=max_put_oi_strike,
                max_pain=max_pain,
                atm_strike=atm,
                atm_call_iv=atm_call.iv if atm_call else 0,
                atm_put_iv=atm_put.iv if atm_put else 0,
            )
        except Exception as e:
            logger.error(f"Error parsing BSE data: {e}")
            return None

    def get_option_data(self, strike: int, option_type: str, symbol: str = "SENSEX", expiry_date: str = None) -> Optional[OptionData]:
        chain = self.fetch_option_chain(symbol, expiry_date)
        if not chain:
            return None
        options = chain.calls if option_type.upper() == "CE" else chain.puts
        return next((o for o in options if o.strike == strike), None)

    def get_support_resistance(self, symbol: str = "SENSEX", expiry_date: str = None, num_levels: int = 3) -> Dict[str, list]:
        chain = self.fetch_option_chain(symbol, expiry_date)
        if not chain:
            return {"support": [], "resistance": []}

        sorted_calls = sorted(chain.calls, key=lambda x: x.open_interest, reverse=True)
        resistance = sorted([c.strike for c in sorted_calls[:num_levels] if c.strike > chain.spot_price])

        sorted_puts = sorted(chain.puts, key=lambda x: x.open_interest, reverse=True)
        support = sorted([p.strike for p in sorted_puts[:num_levels] if p.strike < chain.spot_price], reverse=True)

        return {"support": support[:num_levels], "resistance": resistance[:num_levels]}


bse_scraper = BSEOptionChainScraper()
