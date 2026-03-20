"""
IV Monitor & Greeks Calculator

Provides two capabilities:

1. IV Percentile Filter
   - Fetches ATM IV from NSE option chain (falls back to India VIX via yfinance)
   - Maintains a rolling 30-day history in .iv_history.json
   - Computes IV percentile rank: if current IV is above the 80th percentile of the
     last 30 days it means premiums are expensive → block new entries

2. Black-Scholes Greeks (pure Python, no scipy dependency)
   - Delta, Gamma, Theta (daily ₹), Vega (per 1% IV change)
   - Used to stamp every trade record at entry and for early-exit warning in the
     position monitor (theta drain kill-switch)

Usage:
    from bot.iv_monitor import iv_monitor, GreeksResult

    # Before entry — check IV percentile
    allowed, reason, greeks = iv_monitor.check_entry(
        symbol="NIFTY", strike=23000, option_type="CE",
        spot=23050, quantity=65,
    )

    # In position monitor — check theta drain
    should_exit, reason = iv_monitor.check_theta_drain(trade, current_price, quantity)
"""

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Optional, Tuple
from loguru import logger

from config import settings

# ─────────────────────── constants ────────────────────────────────────────────
_RISK_FREE_RATE = 0.065     # India 10-yr G-sec ≈ 6.5%
_DIVIDEND_YIELD = 0.012     # NIFTY/BANKNIFTY dividend yield ≈ 1.2%
_IV_HISTORY_FILE = ".iv_history.json"
_IV_HISTORY_DAYS = 30       # rolling window for percentile calculation
_TRADING_DAYS_PER_YEAR = 252


# ─────────────────────── dataclasses ──────────────────────────────────────────

@dataclass
class GreeksResult:
    """Greeks at a specific moment. All ₹ values are per single unit (1 option)."""
    delta: float            # 0–1 for CE, -1–0 for PE; % move per 1-pt index move
    gamma: float            # change in delta per 1-pt index move
    theta_daily: float      # ₹ lost per day from time decay (negative)
    vega: float             # ₹ change per 1% change in IV
    iv_used: float          # annualised IV decimal used (e.g. 0.15 = 15%)
    days_to_expiry: float   # T used in calculation
    iv_pct: float           # IV as percentage (e.g. 15.0)


@dataclass
class IVCheckResult:
    """Result of the IV percentile entry check."""
    allowed: bool
    reason: str
    iv_pct: float               # current ATM IV in % (0 = not available)
    iv_percentile: float        # 0–100; high = expensive
    greeks: Optional[GreeksResult]


# ─────────────────────── Black-Scholes helpers ─────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_greeks(
    spot: float,
    strike: float,
    days_to_expiry: float,
    iv_pct: float,
    option_type: str,       # "CE" or "PE"
    risk_free: float = _RISK_FREE_RATE,
    dividend_yield: float = _DIVIDEND_YIELD,
) -> Optional[GreeksResult]:
    """
    Calculate Black-Scholes Greeks for an index option.

    Parameters
    ----------
    spot            : Current index spot price
    strike          : Option strike price
    days_to_expiry  : Calendar days until expiry (0.5 = same day, intraday)
    iv_pct          : Implied volatility as a percentage (e.g. 15.0 = 15%)
    option_type     : "CE" or "PE"
    risk_free       : Annual risk-free rate as decimal (default 6.5%)
    dividend_yield  : Annual continuous dividend yield (default 1.2%)

    Returns None if inputs are invalid (zero days, zero IV, etc.)
    """
    try:
        # Guard against degenerate inputs
        if spot <= 0 or strike <= 0 or iv_pct <= 0 or days_to_expiry < 0.1:
            return None

        sigma = iv_pct / 100.0
        T = days_to_expiry / 365.0  # time in years
        S = spot
        K = strike
        r = risk_free
        q = dividend_yield

        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T

        # Common terms
        exp_qt = math.exp(-q * T)
        exp_rt = math.exp(-r * T)
        npdf_d1 = _norm_pdf(d1)

        if option_type.upper() == "CE":
            delta = exp_qt * _norm_cdf(d1)
            theta_yr = (
                -S * exp_qt * npdf_d1 * sigma / (2 * sqrt_T)
                - r * K * exp_rt * _norm_cdf(d2)
                + q * S * exp_qt * _norm_cdf(d1)
            )
        else:  # PE
            delta = exp_qt * (_norm_cdf(d1) - 1.0)
            theta_yr = (
                -S * exp_qt * npdf_d1 * sigma / (2 * sqrt_T)
                + r * K * exp_rt * _norm_cdf(-d2)
                - q * S * exp_qt * _norm_cdf(-d1)
            )

        gamma = exp_qt * npdf_d1 / (S * sigma * sqrt_T)
        vega = S * exp_qt * npdf_d1 * sqrt_T / 100.0   # per 1% IV change
        theta_daily = theta_yr / 365.0                  # per calendar day

        return GreeksResult(
            delta=round(delta, 4),
            gamma=round(gamma, 6),
            theta_daily=round(theta_daily, 4),
            vega=round(vega, 4),
            iv_used=sigma,
            days_to_expiry=days_to_expiry,
            iv_pct=iv_pct,
        )
    except (ValueError, ZeroDivisionError, OverflowError) as e:
        logger.debug(f"Black-Scholes error (S={spot}, K={strike}, T={days_to_expiry}, σ={iv_pct}): {e}")
        return None


# ─────────────────────── IV Monitor class ──────────────────────────────────────

class IVMonitor:
    """
    Tracks ATM IV history and provides pre-entry IV percentile gate + Greeks.

    Thread safety: single async bot process, no locking needed.
    """

    def __init__(self):
        self._iv_history: Dict[str, Dict[str, float]] = {}   # {symbol: {date_str: iv}}
        self._today_iv_loaded: Dict[str, bool] = {}
        self._load_history()

    # ── History persistence ────────────────────────────────────────────────────

    def _load_history(self):
        if os.path.exists(_IV_HISTORY_FILE):
            try:
                with open(_IV_HISTORY_FILE, "r") as f:
                    self._iv_history = json.load(f)
                logger.debug(f"IV history loaded: {sum(len(v) for v in self._iv_history.values())} entries")
            except Exception as e:
                logger.warning(f"IV history load failed: {e} — starting fresh")
                self._iv_history = {}

    def _save_history(self):
        try:
            with open(_IV_HISTORY_FILE, "w") as f:
                json.dump(self._iv_history, f)
        except Exception as e:
            logger.warning(f"IV history save failed: {e}")

    def _prune_old_entries(self, symbol: str):
        """Keep only the last _IV_HISTORY_DAYS calendar days."""
        cutoff = (date.today() - timedelta(days=_IV_HISTORY_DAYS)).isoformat()
        hist = self._iv_history.get(symbol, {})
        pruned = {k: v for k, v in hist.items() if k >= cutoff}
        self._iv_history[symbol] = pruned

    def record_iv(self, symbol: str, iv_pct: float):
        """
        Record today's ATM IV for a symbol.
        Only stores one value per day (first call wins — captures opening IV).
        """
        if iv_pct <= 0:
            return
        today_str = date.today().isoformat()
        sym_hist = self._iv_history.setdefault(symbol, {})
        if today_str not in sym_hist:
            sym_hist[today_str] = round(iv_pct, 2)
            self._prune_old_entries(symbol)
            self._save_history()
            logger.debug(f"IV history: recorded {symbol} ATM IV = {iv_pct:.1f}% (today={today_str})")

    def get_iv_percentile(self, symbol: str, current_iv: float) -> float:
        """
        Return the percentile rank of current_iv vs the last 30-day history.
        Returns -1.0 if there is insufficient history (<5 days).
        """
        hist = self._iv_history.get(symbol, {})
        values = list(hist.values())
        if len(values) < 5:
            return -1.0     # insufficient history — can't compute meaningful percentile
        below = sum(1 for v in values if v < current_iv)
        return round(below / len(values) * 100.0, 1)

    # ── IV fetching ────────────────────────────────────────────────────────────

    def _get_iv_from_option_chain(self, symbol: str, option_type: str = "CE") -> float:
        """
        Try to get ATM IV from NSE (or BSE for SENSEX) option chain.
        Returns 0.0 if scraping fails.
        """
        try:
            if symbol == "SENSEX":
                from bot.bse_scraper import bse_scraper
                chain = bse_scraper.fetch_option_chain("SENSEX")
            else:
                from bot.nse_scraper import nse_scraper
                chain = nse_scraper.fetch_option_chain(symbol)

            if not chain:
                return 0.0

            iv = chain.atm_call_iv if option_type.upper() == "CE" else chain.atm_put_iv
            if iv <= 0:
                # Try the other side if primary is zero
                iv = chain.atm_put_iv if option_type.upper() == "CE" else chain.atm_call_iv
            return float(iv)
        except Exception as e:
            logger.debug(f"Option chain IV fetch failed for {symbol}: {e}")
            return 0.0

    def _get_iv_from_vix(self) -> float:
        """
        Use India VIX as a proxy for NIFTY ATM IV.
        VIX ≈ annualised 30-day ATM IV for NIFTY.
        Returns 0.0 on failure.
        """
        try:
            import yfinance as yf
            info = yf.Ticker("^INDIAVIX").fast_info
            vix = float(info.get("lastPrice") or 0)
            if vix <= 0:
                hist = yf.Ticker("^INDIAVIX").history(period="1d", interval="1d")
                if not hist.empty:
                    vix = float(hist["Close"].iloc[-1])
            return vix
        except Exception as e:
            logger.debug(f"VIX fallback for IV fetch failed: {e}")
            return 0.0

    def get_atm_iv(self, symbol: str, option_type: str = "CE") -> float:
        """
        Get current ATM IV in % for a symbol.
        Priority: NSE/BSE option chain → India VIX proxy → 0.0
        """
        iv = self._get_iv_from_option_chain(symbol, option_type)
        if iv <= 0 and symbol in ("NIFTY", "BANKNIFTY"):
            iv = self._get_iv_from_vix()
        return iv

    # ── Days to expiry ─────────────────────────────────────────────────────────

    @staticmethod
    def days_to_expiry(symbol: str) -> float:
        """
        Calculate calendar days to the next weekly expiry for the given index.
        Returns at least 0.5 (same-day trades, to avoid division-by-zero).
        """
        try:
            from bot.index_config import get_index
            idx = get_index(symbol)
            if not idx:
                return 3.0  # safe default: 3 days

            expiry_wd = idx.expiry_weekday   # 0=Mon, 1=Tue, ..., 6=Sun
            today = date.today()
            today_wd = today.weekday()

            delta_days = (expiry_wd - today_wd) % 7
            if delta_days == 0:
                # Today is expiry day — use same-day (0.5) to reflect intraday only
                delta_days_f = 0.5
            else:
                delta_days_f = float(delta_days)
            return max(0.5, delta_days_f)
        except Exception:
            return 3.0

    # ── Main entry check ───────────────────────────────────────────────────────

    def check_entry(
        self,
        symbol: str,
        strike: int,
        option_type: str,
        spot: float,
        quantity: int = 1,
    ) -> IVCheckResult:
        """
        Pre-entry IV percentile gate.

        Decision:
        - IV percentile < iv_percentile_max (default 80): ALLOW entry
        - IV percentile ≥ iv_percentile_max               : BLOCK entry — premiums overpriced
        - Insufficient history (<5 data points)           : ALLOW entry with a warning

        Also returns Greeks computed at current IV.
        """
        if not getattr(settings.trading, 'iv_filter_enabled', False):
            return IVCheckResult(
                allowed=True, reason="IV filter disabled",
                iv_pct=0.0, iv_percentile=-1.0, greeks=None,
            )

        iv_pct = self.get_atm_iv(symbol, option_type)
        greeks: Optional[GreeksResult] = None

        if iv_pct > 0:
            # Record in history BEFORE computing percentile so today counts
            self.record_iv(symbol, iv_pct)
            dte = self.days_to_expiry(symbol)
            greeks = black_scholes_greeks(spot, strike, dte, iv_pct, option_type)

        iv_percentile = self.get_iv_percentile(symbol, iv_pct) if iv_pct > 0 else -1.0
        iv_pct_max = getattr(settings.trading, 'iv_percentile_max', 80.0)

        if iv_pct <= 0:
            return IVCheckResult(
                allowed=True,
                reason="IV data unavailable — skipping IV filter",
                iv_pct=0.0, iv_percentile=-1.0, greeks=greeks,
            )

        if iv_percentile < 0:
            return IVCheckResult(
                allowed=True,
                reason=f"IV={iv_pct:.1f}% (insufficient history for percentile — allowing entry)",
                iv_pct=iv_pct, iv_percentile=-1.0, greeks=greeks,
            )

        if iv_percentile >= iv_pct_max:
            return IVCheckResult(
                allowed=False,
                reason=(
                    f"IV too expensive: {iv_pct:.1f}% is at {iv_percentile:.0f}th percentile "
                    f"(threshold {iv_pct_max:.0f}th). "
                    f"Premiums inflated — wait for IV crush."
                ),
                iv_pct=iv_pct, iv_percentile=iv_percentile, greeks=greeks,
            )

        return IVCheckResult(
            allowed=True,
            reason=f"IV={iv_pct:.1f}% at {iv_percentile:.0f}th percentile — OK",
            iv_pct=iv_pct, iv_percentile=iv_percentile, greeks=greeks,
        )

    # ── Position: theta drain kill ─────────────────────────────────────────────

    def check_theta_drain(
        self,
        symbol: str,
        strike: int,
        option_type: str,
        spot: float,
        entry_price: float,
        current_price: float,
        quantity: int,
    ) -> Tuple[bool, str]:
        """
        Warn if theta is eating more than ₹50/day AND the position has not moved into
        meaningful profit.  Used in the position monitor loop.

        Returns (True, reason) to suggest early exit; (False, "") otherwise.
        """
        if not getattr(settings.trading, 'theta_exit_enabled', True):
            return False, ""

        iv_pct = self.get_atm_iv(symbol, option_type)
        if iv_pct <= 0:
            return False, ""

        dte = self.days_to_expiry(symbol)
        greeks = black_scholes_greeks(spot, strike, dte, iv_pct, option_type)
        if not greeks:
            return False, ""

        theta_rupees_day = greeks.theta_daily * quantity  # ₹ lost per day (negative)
        theta_threshold = getattr(settings.trading, 'theta_exit_threshold', -50.0)

        if entry_price <= 0:
            return False, ""

        unrealised_pct = (current_price - entry_price) / entry_price * 100.0

        if theta_rupees_day < theta_threshold and unrealised_pct < 5.0:
            reason = (
                f"⚠️ Theta drain: ₹{theta_rupees_day:.0f}/day "
                f"(IV={iv_pct:.1f}%, DTE={dte:.1f}) | "
                f"Position P&L only {unrealised_pct:+.1f}% — "
                f"consider early exit before decay accelerates"
            )
            return True, reason

        return False, ""

    # ── IV history backfill ────────────────────────────────────────────────────

    def backfill_from_vix(self, days: int = 30, symbols: list = None) -> int:
        """
        Seed `.iv_history.json` using India VIX historical closes as an ATM IV proxy.

        India VIX ≈ NIFTY 30-day ATM implied volatility, so it's a reasonable
        first-week stand-in until live option-chain data accumulates.

        Parameters
        ----------
        days    : How many calendar days to look back (default 30).
        symbols : Symbols to seed (default: NIFTY, BANKNIFTY, SENSEX).
                  SENSEX uses its own BSE chain so VIX proxying is less accurate —
                  it's still included because having *some* history is better than none.

        Returns the number of new dates written (0 if history already complete).
        """
        if symbols is None:
            symbols = ["NIFTY", "BANKNIFTY", "SENSEX"]

        try:
            import yfinance as yf
            hist = yf.Ticker("^INDIAVIX").history(period=f"{days + 10}d", interval="1d")
            if hist.empty:
                logger.warning("backfill_from_vix: no VIX history returned from yfinance")
                return 0
        except Exception as e:
            logger.warning(f"backfill_from_vix: yfinance fetch failed: {e}")
            return 0

        cutoff = (date.today() - timedelta(days=days)).isoformat()
        written = 0

        for sym in symbols:
            sym_hist = self._iv_history.setdefault(sym, {})
            for idx_ts, row in hist.iterrows():
                try:
                    day_str = idx_ts.date().isoformat()
                except Exception:
                    day_str = str(idx_ts)[:10]

                if day_str < cutoff or day_str >= date.today().isoformat():
                    continue  # skip future / too-old dates

                if day_str in sym_hist:
                    continue  # don't overwrite real option-chain data

                vix_close = float(row["Close"])
                if vix_close > 0:
                    sym_hist[day_str] = round(vix_close, 2)
                    written += 1

        if written > 0:
            self._save_history()
            logger.info(
                f"backfill_from_vix: seeded {written} date-entries across {symbols} "
                f"using India VIX history. Activate IV filter once real data overlaps."
            )
        else:
            logger.info("backfill_from_vix: no new entries needed (history already populated).")

        return written


# ─────────────────────── singleton ────────────────────────────────────────────

iv_monitor = IVMonitor()
