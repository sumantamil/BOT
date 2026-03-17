"""
Dhan Broker Module

Implements the same interface as browser/zerodha.py but uses the free
dhanhq SDK instead of the paid Kite Connect API.

Key differences from Zerodha:
- Token never expires (set once in .env, no daily refresh needed)
- Orders use numeric security_id (looked up from Dhan instrument master)
- Exchange names: NSE_FNO (not NFO), BSE_FO (not BFO)
- Forever Orders instead of GTT orders
- No Playwright browser automation fallback (API-only)

Setup:
1. pip install dhanhq
2. Get client_id + access_token from https://dhanhq.co/
3. Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN to .env
4. Set BROKER=dhan in .env
"""

import asyncio
import csv
import io
import os
import calendar
from typing import Optional, Dict, List
from datetime import datetime, timedelta, date
from dataclasses import dataclass
from enum import Enum
from loguru import logger

try:
    from dhanhq import dhanhq
    DHANHQ_AVAILABLE = True
except ImportError:
    DHANHQ_AVAILABLE = False
    logger.warning("dhanhq not installed. Run: pip install dhanhq")

import sys
sys.path.append('..')
from config import settings
from bot.index_config import IndexConfig, NIFTY


# ── Re-export the same enums so the rest of the bot code is broker-agnostic ──
class OrderType(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OptionType(Enum):
    CE = "CE"
    PE = "PE"


@dataclass
class Position:
    symbol: str
    quantity: int
    buy_price: float
    current_price: float
    pnl: float
    pnl_percentage: float


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    message: str
    timestamp: datetime


# ── Dhan exchange / product constants ────────────────────────────────────────
_EXCHANGE_NSE_FNO = "NSE_FNO"
_EXCHANGE_BSE_FO  = "BSE_FO"
_PRODUCT_INTRADAY = "INTRADAY"
_PRODUCT_CNC      = "CNC"        # used for AMO (queue at exchange overnight)
_PRODUCT_MARGIN   = "MARGIN"     # required for BSE_FO (SENSEX/BANKEX options)
_ORDER_MARKET     = "MARKET"
_ORDER_LIMIT      = "LIMIT"

# Dhan AMO window: weekdays 17:00–23:59 and 00:00–09:08
_AMO_START_EVE  = 17 * 60          # 17:00 in minutes-of-day
_AMO_END_MORN   =  9 * 60 + 8     #  9:08 in minutes-of-day


class DhanBroker:
    """
    Dhan trading broker.  Implements the same public interface as ZerodhaKite
    so the rest of the codebase needs zero changes when BROKER=dhan is set.

    Public methods (identical signatures to ZerodhaKite):
        set_active_index(index_config)
        initialize() / close()
        is_logged_in()
        place_order(option_type, strike, order_type, quantity, expiry)
        get_instrument_price(symbol) → float | None
        get_holdings() → list
        get_kite_positions() → dict   (same shape returned)
        get_positions() → List[Position]
        close_position(symbol) → OrderResult
        place_gtt(symbol, exchange, entry_price, stop_loss_pct, quantity) → int|None
        cancel_gtt(gtt_id) → bool
    """

    # Dhan instrument master CSV URL (daily download, cached in memory)
    _INSTRUMENT_CSV_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
    # Local cache path
    _INSTRUMENT_CACHE   = os.path.join(os.path.dirname(__file__), ".dhan_instruments.csv")

    def __init__(self):
        self.dhan_config  = settings.dhan
        self.trading_config = settings.trading
        self._active_index: IndexConfig = NIFTY

        # dhanhq client
        self._client = None
        if DHANHQ_AVAILABLE:
            cid   = self.dhan_config.client_id
            token = self.dhan_config.access_token
            if cid and token:
                try:
                    self._client = dhanhq(cid, token)
                    logger.info(f"Dhan API initialised for client_id={cid}")
                except Exception as e:
                    logger.error(f"Dhan API init failed: {e}")
            else:
                logger.warning("DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN not set in .env")
        else:
            logger.error("dhanhq package not installed — run: pip install dhanhq")

        # Instrument lookup cache: {(index_name, strike, option_type, expiry_date)} → security_id
        self._instrument_map: Dict[str, str] = {}
        self._instruments_loaded_date: Optional[date] = None

        # Security ID cache for active/recent positions.
        # Keyed by canonical symbol e.g. "NIFTY24500CE" → "57847"
        # Populated at order placement so subsequent GTT/LTP/exit calls
        # always use the EXACT same security_id (correct expiry guaranteed).
        self._position_security_ids: Dict[str, str] = {}

        # LTP cache: {symbol: (price, fetch_timestamp)}
        # Dhan quote_data is rate-limited; cache for 30 s to avoid 429 errors.
        self._ltp_cache: Dict[str, tuple] = {}
        _LTP_CACHE_TTL_SECONDS = 30  # class-level but stored as instance for clarity
        self._ltp_cache_ttl: int = _LTP_CACHE_TTL_SECONDS

    # ── Index ────────────────────────────────────────────────────────────────

    def set_active_index(self, index_config: IndexConfig):
        self._active_index = index_config
        logger.info(f"DhanBroker: active index → {index_config.display_name}")

    # ── Lifecycle (no browser; just to match interface) ──────────────────────

    async def initialize(self, headless: bool = False):
        """No browser to start; load instrument master cache."""
        await self._ensure_instruments_loaded()
        logger.info("DhanBroker ready")

    async def close(self):
        logger.info("DhanBroker: nothing to close")

    def is_logged_in(self) -> bool:
        return self._client is not None

    async def get_fund_limits(self) -> dict:
        """
        Get available funds and margin from Dhan account.
        Returns dict with: available_balance, used_margin, available_margin
        """
        if not self._client:
            return {"available_balance": 0.0, "used_margin": 0.0, "available_margin": 0.0}
        try:
            resp = await asyncio.to_thread(self._client.get_fund_limits)
            if not resp or resp.get("status") != "success":
                err_code = resp.get("data", {}).get("errorCode", "") if resp else ""
                if err_code == "DH-901":
                    logger.error("Dhan access token is INVALID or EXPIRED. "
                                 "Generate a new token at https://web.dhan.co and update DHAN_ACCESS_TOKEN in .env")
                else:
                    logger.warning(f"Dhan get_fund_limits failed: {resp}")
                return {"available_balance": 0.0, "used_margin": 0.0, "available_margin": 0.0}
            
            data = resp.get("data", {})
            # Dhan API fields (note: API has typo "availabelBalance")
            available_balance = float(data.get("availabelBalance", 0) or 0)
            used_margin = float(data.get("utilizedAmount", 0) or 0)
            # Calculate available margin as balance minus utilized
            available_margin = available_balance - used_margin
            
            return {
                "available_balance": round(available_balance, 2),
                "used_margin": round(used_margin, 2),
                "available_margin": round(max(0, available_margin), 2)
            }
        except Exception as e:
            logger.warning(f"Dhan get_fund_limits failed: {e}")
            return {"available_balance": 0.0, "used_margin": 0.0, "available_margin": 0.0}

    # ── AMO window detection ─────────────────────────────────────────────────

    @staticmethod
    def is_amo_window() -> bool:
        """
        Returns True when the current time falls inside Dhan's AMO window.
        Dhan accepts AMO orders on weekdays between 17:00–23:59 and 00:00–09:08.
        """
        now = datetime.now()
        if now.weekday() > 4:            # Saturday / Sunday — no AMO
            return False
        minutes = now.hour * 60 + now.minute
        return minutes >= _AMO_START_EVE or minutes < _AMO_END_MORN

    # ── Instrument master ────────────────────────────────────────────────────

    async def _ensure_instruments_loaded(self):
        """Download/refresh Dhan instrument master once per calendar day."""
        today = date.today()
        if self._instruments_loaded_date == today and self._instrument_map:
            return

        # Try local cache first (same day)
        if os.path.exists(self._INSTRUMENT_CACHE):
            cache_date = date.fromtimestamp(os.path.getmtime(self._INSTRUMENT_CACHE))
            if cache_date == today:
                await self._parse_instrument_file(self._INSTRUMENT_CACHE)
                self._instruments_loaded_date = today
                logger.info(f"Dhan instruments loaded from cache ({len(self._instrument_map)} FNO entries)")
                return

        # Download fresh copy
        try:
            import urllib.request
            logger.info("Downloading Dhan instrument master…")
            urllib.request.urlretrieve(self._INSTRUMENT_CSV_URL, self._INSTRUMENT_CACHE)
            await self._parse_instrument_file(self._INSTRUMENT_CACHE)
            self._instruments_loaded_date = today
            logger.info(f"Dhan instruments updated ({len(self._instrument_map)} FNO entries)")
        except Exception as e:
            logger.warning(f"Could not download Dhan instrument master: {e}")

    async def _parse_instrument_file(self, path: str):
        """
        Parse the Dhan scrip master CSV and build lookup map.

        Actual Dhan CSV columns (verified against live file):
            SEM_EXM_EXCH_ID        → 'NSE' or 'BSE'
            SEM_SMST_SECURITY_ID   → numeric security_id
            SEM_INSTRUMENT_NAME    → 'OPTIDX' for index options
            SEM_TRADING_SYMBOL     → e.g. 'NIFTY-Mar2026-23000-CE'
            SEM_STRIKE_PRICE       → strike as float string
            SEM_OPTION_TYPE        → 'CE' / 'PE'
            SEM_EXPIRY_DATE        → 'YYYY-MM-DD HH:MM:SS'
        """
        self._instrument_map = {}
        try:
            with open(path, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    instr = row.get("SEM_INSTRUMENT_NAME", "").strip()
                    if instr != "OPTIDX":        # index options only
                        continue
                    exch = row.get("SEM_EXM_EXCH_ID", "").strip()
                    if exch not in ("NSE", "BSE"):
                        continue
                    opttype = row.get("SEM_OPTION_TYPE", "").strip().upper()
                    if opttype not in ("CE", "PE"):
                        continue
                    sec_id    = row.get("SEM_SMST_SECURITY_ID", "").strip()
                    strike    = row.get("SEM_STRIKE_PRICE", "").strip()
                    expiry_dt = row.get("SEM_EXPIRY_DATE", "").strip()   # 'YYYY-MM-DD HH:MM:SS'
                    trad_sym  = row.get("SEM_TRADING_SYMBOL", "").strip()
                    if not (sec_id and strike and expiry_dt and trad_sym):
                        continue
                    # Index name is the prefix before the first '-'
                    # e.g. 'NIFTY-Mar2026-23000-CE' → 'NIFTY'
                    index_name = trad_sym.split("-")[0].upper()
                    if index_name not in ("NIFTY", "BANKNIFTY", "SENSEX", "BANKEX",
                                         "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
                        continue
                    try:
                        strike_int = int(float(strike))
                    except ValueError:
                        continue
                    expiry_date = expiry_dt[:10]   # strip time portion → 'YYYY-MM-DD'
                    key = f"{index_name}|{strike_int}|{opttype}|{expiry_date}"
                    self._instrument_map[key] = sec_id
        except Exception as e:
            logger.error(f"Failed to parse Dhan instrument file: {e}")

    def _get_security_id(self, index_name: str, strike: int, option_type: str, expiry_date: date) -> Optional[str]:
        """Look up Dhan security_id from cached instrument master."""
        key = f"{index_name.upper()}|{strike}|{option_type.upper()}|{expiry_date.isoformat()}"
        sec_id = self._instrument_map.get(key)
        if not sec_id:
            logger.warning(f"Dhan: security_id not found for key={key}")
        return sec_id

    # ── Expiry calculation (same logic as Zerodha) ───────────────────────────

    def _next_expiry_date(self, index: Optional["IndexConfig"] = None) -> date:
        """Return the next expiry date for an index (defaults to the active index).

        For monthly indices, exchange holidays can shift the published expiry by
        1-2 days from the algorithmically-computed weekday.  After computing the
        canonical date we scan the instrument cache for the nearest real expiry
        within a ±3-day window so holiday-shifted dates are handled automatically.
        """
        index = index or self._active_index
        today = datetime.now()
        if index.weekly_expiry:
            wd = index.expiry_weekday
            days_ahead = (wd - today.weekday()) % 7 or 7
            computed = (today + timedelta(days=days_ahead)).date()
        else:
            # Monthly: last expiry_weekday of current/next month
            computed = today.date()  # fallback
            year, month = today.year, today.month
            for _ in range(2):
                last_day = calendar.monthrange(year, month)[1]
                for d in range(last_day, 0, -1):
                    if datetime(year, month, d).weekday() == index.expiry_weekday:
                        exp = date(year, month, d)
                        if exp > today.date():
                            computed = exp
                            break
                if computed > today.date():
                    break
                month += 1
                if month > 12:
                    month, year = 1, year + 1

        # Verify the computed date actually exists in the instrument cache.
        # If not, search within a ±3-day window for the nearest real expiry
        # (handles exchange holiday shifts like Eid, etc.).
        if self._instrument_map:
            index_name = index.name.upper()
            if not any(k.startswith(f"{index_name}|") and k.endswith(f"|{computed.isoformat()}")
                       for k in self._instrument_map):
                for delta in range(1, 4):
                    for candidate in [computed - timedelta(days=delta),
                                      computed + timedelta(days=delta)]:
                        if candidate > today.date() and any(
                            k.startswith(f"{index_name}|") and k.endswith(f"|{candidate.isoformat()}")
                            for k in self._instrument_map
                        ):
                            logger.info(
                                f"Dhan expiry shift: {index_name} computed={computed} "
                                f"→ actual={candidate} (holiday/exchange adjustment)"
                            )
                            return candidate

        return computed

    # ── Core order placement ─────────────────────────────────────────────────

    async def place_order(
        self,
        option_type: OptionType,
        strike: int,
        order_type: OrderType = OrderType.BUY,
        quantity: int = None,
        expiry: str = None,          # ignored (calculated automatically)
        index_config=None,           # optional IndexConfig override (for closing recovered positions)
    ) -> OrderResult:
        """Place an options order via Dhan API."""
        if not self._client:
            return OrderResult(False, None, "Dhan API not initialised", datetime.now())

        quantity = quantity or self.trading_config.default_quantity
        await self._ensure_instruments_loaded()

        index = index_config or self._active_index
        expiry_date = self._next_expiry_date(index)
        exchange = _EXCHANGE_BSE_FO if index.exchange == "BSE" else _EXCHANGE_NSE_FNO

        sec_id = self._get_security_id(index.name, strike, option_type.value, expiry_date)
        if not sec_id:
            msg = (f"Dhan: Could not find security_id for {index.name} {strike} "
                   f"{option_type.value} expiry {expiry_date} — "
                   f"instrument master may need refresh")
            logger.error(msg)
            return OrderResult(False, None, msg, datetime.now())

        try:
            amo = self.is_amo_window()
            # BSE_FO (SENSEX/BANKEX) only accepts MARGIN product type.
            # NSE_FNO uses INTRADAY during market hours, CNC for AMO.
            if exchange == _EXCHANGE_BSE_FO:
                product = _PRODUCT_MARGIN
            elif amo:
                product = _PRODUCT_CNC
            else:
                product = _PRODUCT_INTRADAY
            tag = " [AMO]" if amo else ""
            logger.info(
                f"Dhan{tag}: {order_type.value} {index.name} {strike} {option_type.value} "
                f"x{quantity} | sec_id={sec_id} | expiry={expiry_date} | product={product}"
            )
            order_kwargs = dict(
                security_id=sec_id,
                exchange_segment=exchange,
                transaction_type=order_type.value,
                quantity=quantity,
                order_type=_ORDER_MARKET,
                product_type=product,
                price=0,
            )
            if amo:
                order_kwargs["after_market_order"] = True
            response = await asyncio.to_thread(
                self._client.place_order,
                **order_kwargs,
            )
            if response and response.get("status") == "success":
                oid = str(response.get("data", {}).get("orderId", ""))
                logger.info(f"Dhan order placed: {oid}")
                # Cache security_id for this symbol so GTT / LTP / exit lookups
                # always use the EXACT same contract (right expiry guaranteed).
                sym_key = f"{index.name}{strike}{option_type.value}".upper()
                self._position_security_ids[sym_key] = sec_id
                return OrderResult(True, oid,
                    f"Dhan {order_type.value} {index.name}{strike}{option_type.value} x{quantity}",
                    datetime.now())
            else:
                msg = f"Dhan order failed: {response}"
                logger.error(msg)
                return OrderResult(False, None, msg, datetime.now())
        except Exception as e:
            msg = f"Dhan place_order exception: {e}"
            logger.error(msg)
            return OrderResult(False, None, msg, datetime.now())

    # ── Price fetch ──────────────────────────────────────────────────────────

    def _symbol_matches(self, search: str, candidate: str) -> bool:
        """
        Flexible symbol matching between Zerodha-style (NIFTY24500CE) and
        Dhan position formats (NIFTY2431724500CE, NIFTY-17MAR26-24500-CE, etc.).
        Parses underlying, strike, and option type from 'search' and checks
        all three appear in 'candidate'.
        """
        s = search.upper()
        c = candidate.upper()
        if s in c or c in s:
            return True
        for underlying in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
                           "NIFTY", "SENSEX", "BANKEX"):
            if not s.startswith(underlying):
                continue
            rest = s[len(underlying):]
            opt = "CE" if rest.endswith("CE") else ("PE" if rest.endswith("PE") else None)
            if not opt:
                continue
            strike_str = rest[:-2]
            try:
                strike = str(int(strike_str))
            except ValueError:
                continue
            return underlying in c and strike in c and opt in c
        return False

    def _sec_id_for_symbol(self, symbol: str) -> Optional[str]:
        """
        Best-effort security_id lookup for a symbol.
        Priority: (1) per-order cache, (2) expiry-aware instrument map.
        Handles both compact format (NIFTY23250CE) and Dhan dash format
        (NIFTY-Mar2026-23250-CE).
        """
        sym_upper = symbol.upper()
        # 1. Check order placement cache (exact expiry guaranteed)
        sec_id = self._position_security_ids.get(sym_upper)
        if sec_id:
            return sec_id
        # 2. Expiry-aware lookup via next_expiry_date
        for underlying in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
                           "NIFTY", "SENSEX", "BANKEX"):
            if not sym_upper.startswith(underlying):
                continue
            rest = sym_upper[len(underlying):]
            # Dhan dash format: -MMM2026-23250-CE
            if rest.startswith("-"):
                parts = rest.split("-")
                # parts = ['', 'MMMYYYY', 'STRIKE', 'CE']
                if len(parts) >= 4:
                    opt = parts[-1] if parts[-1] in ("CE", "PE") else None
                    if opt:
                        try:
                            strike = int(parts[-2])
                        except ValueError:
                            continue
                        # Parse month/year from the symbol (e.g. 'MAR2026') and scan
                        # the instrument map by prefix to get the exact expiry date,
                        # rather than using _next_expiry_date() which may return the
                        # wrong Thursday/Tuesday for near- or past-expiry contracts.
                        mon_yr = parts[-3]  # e.g. 'MAR2026'
                        try:
                            dt = datetime.strptime(mon_yr, "%b%Y")
                            prefix = f"{underlying}|{strike}|{opt}|{dt.year}-{dt.month:02d}-"
                            for key, sid in self._instrument_map.items():
                                if key.startswith(prefix):
                                    return sid
                        except (ValueError, Exception):
                            pass
                        # Fallback: next expiry date
                        exp = self._next_expiry_date()
                        return self._get_security_id(underlying, strike, opt, exp)
                continue
            # Compact format: 23250CE
            opt = "CE" if rest.endswith("CE") else ("PE" if rest.endswith("PE") else None)
            if not opt:
                continue
            try:
                strike = int(rest[:-2])
            except ValueError:
                continue
            exp = self._next_expiry_date()
            return self._get_security_id(underlying, strike, opt, exp)
        return None

    async def get_instrument_price(self, symbol: str) -> Optional[float]:
        """Fetch current LTP for an option symbol using positions P&L."""
        if not self._client:
            return None
        await self._ensure_instruments_loaded()

        # ── LTP cache (avoid calling get_positions more than once per cycle) ──
        now = datetime.now()
        cached = self._ltp_cache.get(symbol)
        if cached:
            cached_price, cached_ts = cached
            if (now - cached_ts).total_seconds() < self._ltp_cache_ttl:
                return cached_price if cached_price > 0 else None

        # ── Primary path: compute LTP from positions P&L ─────────────────────
        # Dhan's positions endpoint has no live LTP field but does have
        # unrealizedProfit which allows a reliable mark-to-market computation:
        #   ltp ≈ costPrice + unrealizedProfit / netQty
        try:
            pos_data = await self.get_kite_positions()
            for p in pos_data["day"] + pos_data["net"]:
                if self._symbol_matches(symbol, p["tradingsymbol"]):
                    ltp_val = p.get("last_price") or 0
                    if ltp_val > 0:
                        self._ltp_cache[symbol] = (float(ltp_val), now)
                        # Seed security-id placement cache
                        raw_sec = p.get("security_id", "")
                        if raw_sec and symbol.upper() not in self._position_security_ids:
                            self._position_security_ids[symbol.upper()] = str(raw_sec)
                        return float(ltp_val)
        except Exception as e:
            logger.debug(f"Dhan LTP positions fetch failed for {symbol}: {e}")
        return None

    # ── Holdings ─────────────────────────────────────────────────────────────

    async def get_holdings(self) -> List[Dict]:
        if not self._client:
            return []
        try:
            resp = await asyncio.to_thread(self._client.get_holdings)
            return resp.get("data", []) if resp else []
        except Exception as e:
            logger.warning(f"Dhan get_holdings failed: {e}")
            return []

    # ── Positions (Kite-compatible shape) ────────────────────────────────────

    async def get_kite_positions(self) -> Dict:
        """
        Return positions in the same dict shape as ZerodhaKite.get_kite_positions()
        so the rest of the codebase (web/app.py, order_manager.py) needs no changes.
        """
        if not self._client:
            return {"day": [], "net": [], "total_pnl": 0.0}
        try:
            resp = await asyncio.to_thread(self._client.get_positions)
            if not resp or resp.get("status") != "success":
                err_code = (resp.get("data", {}) or {}).get("errorCode", "") if resp else ""
                if err_code == "DH-901":
                    logger.error("Dhan access token is INVALID or EXPIRED. "
                                 "Generate a new token at https://web.dhan.co and update DHAN_ACCESS_TOKEN in .env")
                return {"day": [], "net": [], "total_pnl": 0.0}
            raw = resp.get("data", [])
            positions = raw if isinstance(raw, list) else []

            day_list = []
            net_list = []
            total_pnl = 0.0

            for p in positions:
                sym = p.get("tradingSymbol", "")
                qty = p.get("netQty", 0) or 0
                buy_qty = p.get("buyQty", 0) or 0
                sell_qty = p.get("sellQty", 0) or 0
                avg_price = p.get("costPrice", 0) or 0
                # Dhan positions endpoint has NO dedicated ltp/LTP field.
                # Compute current mark-to-market price from unrealised P&L:
                #   ltp ≈ avg_price + unrealised_pnl / qty
                unreal = float(p.get("unrealizedProfit", 0) or 0)
                raw_qty = abs(float(qty)) if qty else 0
                if raw_qty > 0 and avg_price:
                    ltp = max(0.0, float(avg_price) + unreal / raw_qty)
                else:
                    ltp = 0.0
                pnl = p.get("unrealizedProfit", 0) or 0
                realised = p.get("realizedProfit", 0) or 0
                total_pnl += pnl + realised

                row = {
                    "tradingsymbol": sym,
                    "product": "MIS",
                    "buy_quantity": buy_qty,
                    "sell_quantity": sell_qty,
                    "quantity": qty,
                    "average_price": avg_price,
                    "last_price": ltp,
                    "pnl": pnl + realised,
                    "realised": realised,
                    "unrealised": pnl,
                    "exchange": _EXCHANGE_NSE_FNO,
                    # Include raw security_id so recovery can cache it directly
                    "security_id": str(p.get("securityId", "") or ""),
                }
                # Use buyQty (always populated) instead of dayBuyQty (can be null/0 for
                # GTT-triggered orders), so all today's positions are counted correctly.
                if buy_qty > 0 or sell_qty > 0:
                    day_list.append(row)
                if qty != 0:
                    net_list.append(row)

            return {"day": day_list, "net": net_list, "total_pnl": round(total_pnl, 2)}
        except Exception as e:
            logger.warning(f"Dhan get_positions failed: {e}")
            return {"day": [], "net": [], "total_pnl": 0.0}

    async def get_positions(self) -> List[Position]:
        data = await self.get_kite_positions()
        result = []
        for p in data["net"]:
            entry = p["average_price"] or 0
            ltp   = p["last_price"] or 0
            qty   = p["quantity"]
            pnl_pct = ((ltp - entry) / entry * 100) if entry else 0
            result.append(Position(
                symbol=p["tradingsymbol"],
                quantity=qty,
                buy_price=entry,
                current_price=ltp,
                pnl=p["pnl"],
                pnl_percentage=round(pnl_pct, 2),
            ))
        return result

    # ── Close position ───────────────────────────────────────────────────────

    async def close_position(self, symbol: str) -> OrderResult:
        """Close an open Dhan position by matching symbol."""
        # Resolve security_id first (use per-order cache for exact expiry)
        sec_id = self._sec_id_for_symbol(symbol)

        # Fetch positions to get qty and exchange
        data = await self.get_kite_positions()
        target = None
        for p in data["net"]:
            if self._symbol_matches(symbol, p["tradingsymbol"]) and p["quantity"] != 0:
                target = p
                break

        if not target:
            return OrderResult(False, None, f"Position not found: {symbol}", datetime.now())

        qty  = abs(target["quantity"])
        txn  = "SELL" if target["quantity"] > 0 else "BUY"
        exch = target.get("exchange", "NSE_FNO")
        dhan_exchange = _EXCHANGE_BSE_FO if "BSE" in exch else _EXCHANGE_NSE_FNO

        # If cache miss, try: (1) raw security_id from live position data,
        # (2) instrument map via symbol parsing (handles Dhan dash format)
        if not sec_id:
            sec_id = target.get("security_id") or ""
        if not sec_id:
            await self._ensure_instruments_loaded()
            sec_id = self._sec_id_for_symbol(target["tradingsymbol"])
        if not sec_id:
            return OrderResult(False, None,
                f"Dhan: security_id not found for {symbol}", datetime.now())

        try:
            resp = await asyncio.to_thread(
                self._client.place_order,
                security_id=sec_id,
                exchange_segment=dhan_exchange,
                transaction_type=txn,
                quantity=qty,
                order_type=_ORDER_MARKET,
                product_type=_PRODUCT_INTRADAY,
                price=0,
            )
            if resp and resp.get("status") == "success":
                oid = str(resp.get("data", {}).get("orderId", ""))
                logger.info(f"Dhan close_position: {symbol} x{qty} order={oid}")
                # Remove from security_id cache — position is closed
                self._position_security_ids.pop(symbol.upper(), None)
                return OrderResult(True, oid, f"Closed {symbol} x{qty}", datetime.now())
            else:
                msg = f"Dhan close_position failed: {resp}"
                logger.error(msg)
                return OrderResult(False, None, msg, datetime.now())
        except Exception as e:
            msg = f"Dhan close_position exception: {e}"
            logger.error(msg)
            return OrderResult(False, None, msg, datetime.now())

    # ── Forever Orders (Dhan equivalent of Kite GTT) ─────────────────────────

    async def place_gtt(
        self,
        symbol: str,
        exchange: str,
        entry_price: float,
        stop_loss_pct: float,
        quantity: int,
    ) -> Optional[int]:
        """
        Place a Dhan Forever Order (stop-loss that survives bot restart).
        Equivalent to Kite GTT.  Returns order_id (int) or None on failure.
        """
        if not self._client:
            logger.warning("Dhan Forever Order: client not initialised")
            return None

        # Resolve security_id — use per-order cache first (guaranteed correct expiry)
        await self._ensure_instruments_loaded()
        sec_id = self._sec_id_for_symbol(symbol)
        if not sec_id:
            logger.warning(f"Dhan Forever Order: security_id not found for {symbol}")
            return None

        trigger_price = round(entry_price * (1 - stop_loss_pct / 100), 1)
        limit_price   = round(trigger_price * 0.98, 1)
        dhan_exchange = _EXCHANGE_BSE_FO if "BSE" in exchange.upper() else _EXCHANGE_NSE_FNO

        try:
            resp = await asyncio.to_thread(
                self._client.place_forever,
                security_id=sec_id,
                exchange_segment=dhan_exchange,
                transaction_type="SELL",
                quantity=quantity,
                order_type=_ORDER_LIMIT,
                product_type=_PRODUCT_CNC,   # Forever Orders only accept CNC, not INTRADAY
                price=limit_price,
                trigger_Price=trigger_price,
            )
            if resp and resp.get("status") == "success":
                oid = resp.get("data", {}).get("orderId")
                logger.info(
                    f"Dhan Forever Order placed: id={oid} | {symbol} | "
                    f"trigger=₹{trigger_price} (SL {stop_loss_pct}% below ₹{entry_price})"
                )
                return int(oid) if oid else None
            else:
                logger.warning(f"Dhan Forever Order failed: {resp}")
                return None
        except Exception as e:
            logger.error(f"Dhan Forever Order exception for {symbol}: {e}")
            return None

    async def cancel_gtt(self, gtt_id: int) -> bool:
        """Cancel a Dhan Forever Order by ID."""
        if not self._client or not gtt_id:
            return False
        try:
            resp = await asyncio.to_thread(
                self._client.cancel_forever,
                order_id=str(gtt_id),
            )
            if resp and resp.get("status") == "success":
                logger.info(f"Dhan Forever Order cancelled: id={gtt_id}")
                return True
            logger.warning(f"Dhan cancel_forever_order response: {resp}")
            return False
        except Exception as e:
            logger.warning(f"Dhan cancel_forever_order exception (id={gtt_id}): {e}")
            return False
