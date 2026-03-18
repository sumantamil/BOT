"""
Trend Analyzer Module

Fetches market data for any supported index and calculates technical
indicators to determine market trend (BULLISH / BEARISH / NEUTRAL).
Supports NIFTY, BANKNIFTY, and SENSEX.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from enum import Enum
from dataclasses import dataclass
from loguru import logger

import sys
sys.path.append('..')
from config import settings
from bot.index_config import IndexConfig, NIFTY, get_index


class Trend(Enum):
    """Market trend enumeration"""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class TrendSignal:
    """Data class for trend analysis results"""
    trend: Trend
    strength: float  # 0-100, how strong is the signal
    current_price: float
    sma_short: float
    sma_long: float
    rsi: float
    macd: float
    macd_signal: float
    timestamp: datetime
    recommendation: str  # "BUY CE", "BUY PE", "HOLD"
    details: Dict
    # Additional indicators
    ema_9: float = 0.0
    ema_21: float = 0.0
    vwap: float = 0.0
    bollinger_upper: float = 0.0
    bollinger_lower: float = 0.0
    bollinger_mid: float = 0.0
    atr: float = 0.0
    supertrend: float = 0.0
    supertrend_direction: str = "NEUTRAL"  # UP or DOWN
    stoch_rsi: float = 0.0


class TrendAnalyzer:
    """
    Analyzes market trends using technical indicators.
    Supports NIFTY, BANKNIFTY, SENSEX (configurable via set_index).
    """
    
    def __init__(self, index_config: IndexConfig = None):
        self.config = settings.trend
        self._index: IndexConfig = index_config or NIFTY
        self._last_data: Optional[pd.DataFrame] = None
        self._last_signal: Optional[TrendSignal] = None

    def set_index(self, index_config: IndexConfig):
        """Switch to a different index."""
        self._index = index_config
        self._last_data = None
        self._last_signal = None
        
    def fetch_data(self, period: str = None, interval: str = None) -> pd.DataFrame:
        """
        Fetch historical data from Yahoo Finance for the active index.
        """
        period = period or self.config.data_period
        interval = interval or self.config.data_interval
        
        logger.info(f"Fetching {self._index.display_name} data: period={period}, interval={interval}")
        
        try:
            ticker = yf.Ticker(self._index.yahoo_symbol)
            data = ticker.history(period=period, interval=interval)
            
            if data.empty:
                logger.warning("No data received from Yahoo Finance")
                return pd.DataFrame()
            
            self._last_data = data
            logger.info(f"Fetched {len(data)} data points")
            return data
            
        except Exception as e:
            logger.error(f"Error fetching data: {e}")
            return pd.DataFrame()
    
    def calculate_sma(self, data: pd.DataFrame, period: int) -> pd.Series:
        """Calculate Simple Moving Average"""
        return data['Close'].rolling(window=period).mean()
    
    def calculate_rsi(self, data: pd.DataFrame, period: int = None) -> pd.Series:
        """
        Calculate Relative Strength Index (RSI)
        
        RSI = 100 - (100 / (1 + RS))
        RS = Average Gain / Average Loss
        """
        period = period or self.config.rsi_period
        
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def calculate_macd(self, data: pd.DataFrame) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate MACD (Moving Average Convergence Divergence)
        
        Returns:
            Tuple of (MACD line, Signal line, Histogram)
        """
        fast = self.config.macd_fast
        slow = self.config.macd_slow
        signal = self.config.macd_signal
        
        exp_fast = data['Close'].ewm(span=fast, adjust=False).mean()
        exp_slow = data['Close'].ewm(span=slow, adjust=False).mean()
        
        macd_line = exp_fast - exp_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    def calculate_ema(self, data: pd.DataFrame, period: int) -> pd.Series:
        """Calculate Exponential Moving Average"""
        return data['Close'].ewm(span=period, adjust=False).mean()
    
    def calculate_vwap(self, data: pd.DataFrame) -> pd.Series:
        """Calculate Volume Weighted Average Price"""
        typical_price = (data['High'] + data['Low'] + data['Close']) / 3
        if 'Volume' in data.columns and data['Volume'].sum() > 0:
            vwap = (typical_price * data['Volume']).cumsum() / data['Volume'].cumsum()
        else:
            # Fallback to typical price if volume data unavailable
            vwap = typical_price.rolling(window=20).mean()
        return vwap
    
    def calculate_bollinger_bands(self, data: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate Bollinger Bands
        
        Returns:
            Tuple of (Upper Band, Middle Band, Lower Band)
        """
        middle = data['Close'].rolling(window=period).mean()
        std = data['Close'].rolling(window=period).std()
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        return upper, middle, lower
    
    def calculate_atr(self, data: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range (Volatility indicator)"""
        high_low = data['High'] - data['Low']
        high_close = abs(data['High'] - data['Close'].shift())
        low_close = abs(data['Low'] - data['Close'].shift())
        
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean()
        return atr
    
    def get_dynamic_confidence_threshold(self, atr_value: float, spot_price: float = None) -> int:
        """
        Calculate dynamic confidence threshold based on market volatility (ATR).
        
        In volatile markets (high ATR), we lower the threshold to catch more opportunities.
        In calm markets (low ATR), we maintain high threshold for quality signals.
        
        Args:
            atr_value: Average True Range value
            spot_price: Current spot price (optional, for percentage calculation)
            
        Returns:
            int: Confidence threshold (50-70)
        """
        if pd.isna(atr_value) or atr_value == 0:
            return 70  # Default to high threshold if no ATR
        
        # Calculate ATR as percentage of price
        if spot_price and spot_price > 0:
            atr_pct = (atr_value / spot_price) * 100
        else:
            atr_pct = atr_value
        
        # Adaptive thresholds based on volatility
        if atr_pct > 2.0:  # Very high volatility (>2%)
            return 50  # Aggressive - many opportunities
        elif atr_pct > 1.5:  # High volatility (1.5-2%)
            return 55  # More aggressive
        elif atr_pct > 1.0:  # Moderate volatility (1-1.5%)
            return 60  # Balanced
        elif atr_pct > 0.5:  # Low volatility (0.5-1%)
            return 65  # Conservative+
        else:  # Very low volatility (<0.5%) OR spot_price unavailable
            # Fall back to absolute ATR value.
            # Indian F&O index typical 5m ATR ranges:
            #   BANKNIFTY: 50-100 pts | SENSEX: 60-100 pts | NIFTY: 15-30 pts
            # The old thresholds (>100→65, else→70) caused BANKNIFTY/SENSEX
            # (ATR 60-80) to always return 70 — too restrictive for strong-trend days.
            if atr_value > 300:
                return 50
            elif atr_value > 220:
                return 55
            elif atr_value > 150:
                return 60
            elif atr_value > 80:
                return 63
            elif atr_value > 50:
                return 65
            else:
                return 70
    
    def calculate_supertrend(self, data: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> Tuple[pd.Series, pd.Series]:
        """
        Calculate Supertrend indicator
        
        Returns:
            Tuple of (Supertrend value, Direction: 1=UP, -1=DOWN)
        """
        atr = self.calculate_atr(data, period)
        hl2 = (data['High'] + data['Low']) / 2
        
        upper_band = hl2 + (multiplier * atr)
        lower_band = hl2 - (multiplier * atr)
        
        supertrend = pd.Series(index=data.index, dtype=float)
        direction = pd.Series(index=data.index, dtype=int)
        
        for i in range(period, len(data)):
            if data['Close'].iloc[i] > upper_band.iloc[i-1]:
                direction.iloc[i] = 1  # Uptrend
                supertrend.iloc[i] = lower_band.iloc[i]
            elif data['Close'].iloc[i] < lower_band.iloc[i-1]:
                direction.iloc[i] = -1  # Downtrend
                supertrend.iloc[i] = upper_band.iloc[i]
            else:
                direction.iloc[i] = direction.iloc[i-1] if i > period else 1
                if direction.iloc[i] == 1:
                    supertrend.iloc[i] = max(lower_band.iloc[i], supertrend.iloc[i-1]) if i > period else lower_band.iloc[i]
                else:
                    supertrend.iloc[i] = min(upper_band.iloc[i], supertrend.iloc[i-1]) if i > period else upper_band.iloc[i]
        
        return supertrend, direction
    
    def calculate_stoch_rsi(self, data: pd.DataFrame, period: int = 14, smooth_k: int = 3, smooth_d: int = 3) -> pd.Series:
        """Calculate Stochastic RSI"""
        rsi = self.calculate_rsi(data, period)
        
        rsi_min = rsi.rolling(window=period).min()
        rsi_max = rsi.rolling(window=period).max()
        
        stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min) * 100
        stoch_rsi_k = stoch_rsi.rolling(window=smooth_k).mean()
        
        return stoch_rsi_k
    
    def analyze(self, regime: str = None) -> Optional[TrendSignal]:
        """
        Perform full trend analysis on the active index with IMPROVED ACCURACY.

        Args:
            regime: Optional regime hint from MarketRegimeDetector
                    ("TRENDING UP", "TRENDING DOWN", "RANGING", "VOLATILE").
                    When TRENDING, EMA crossover weight is boosted (×3 instead of ×1.5)
                    to favour faster momentum signals.
        """
        # Fetch fresh data
        data = self.fetch_data()
        
        if data.empty or len(data) < self.config.sma_long_period:
            logger.warning("Insufficient data for analysis")
            return None
        
        # Calculate all indicators
        sma_short = self.calculate_sma(data, self.config.sma_short_period)
        sma_long = self.calculate_sma(data, self.config.sma_long_period)
        rsi = self.calculate_rsi(data)
        macd_line, signal_line, histogram = self.calculate_macd(data)
        
        ema_9 = self.calculate_ema(data, 9)
        ema_21 = self.calculate_ema(data, 21)
        vwap = self.calculate_vwap(data)
        bb_upper, bb_mid, bb_lower = self.calculate_bollinger_bands(data)
        atr = self.calculate_atr(data)
        supertrend, st_direction = self.calculate_supertrend(data)
        stoch_rsi = self.calculate_stoch_rsi(data)
        
        # Get latest values
        current_price = data['Close'].iloc[-1]
        latest_sma_short = sma_short.iloc[-1]
        latest_sma_long = sma_long.iloc[-1]
        latest_rsi = rsi.iloc[-1]
        latest_macd = macd_line.iloc[-1]
        latest_signal = signal_line.iloc[-1]
        latest_histogram = histogram.iloc[-1]
        
        # Previous candle values (for momentum confirmation)
        prev_histogram = histogram.iloc[-2] if len(histogram) > 1 else latest_histogram
        prev_rsi = rsi.iloc[-2] if len(rsi) > 1 else latest_rsi
        prev_price = data['Close'].iloc[-2] if len(data) > 1 else current_price
        
        latest_ema_9 = ema_9.iloc[-1]
        latest_ema_21 = ema_21.iloc[-1]
        latest_vwap = vwap.iloc[-1]
        latest_bb_upper = bb_upper.iloc[-1]
        latest_bb_mid = bb_mid.iloc[-1]
        latest_bb_lower = bb_lower.iloc[-1]
        latest_atr = atr.iloc[-1]
        latest_supertrend = supertrend.iloc[-1] if not pd.isna(supertrend.iloc[-1]) else current_price
        latest_st_direction = st_direction.iloc[-1] if not pd.isna(st_direction.iloc[-1]) else 0
        latest_stoch_rsi = stoch_rsi.iloc[-1] if not pd.isna(stoch_rsi.iloc[-1]) else 50
        
        avg_volume = data['Volume'].iloc[-20:].mean() if 'Volume' in data.columns and len(data) >= 20 else 0
        latest_volume = data['Volume'].iloc[-1] if 'Volume' in data.columns else 0
        volume_increase = latest_volume > (avg_volume * 1.2) if avg_volume > 0 else False
        
        # --- RSI: tiered scoring (replaces the old binary 40-70 gate) ---
        # Bullish RSI tiers:
        #   55-70  → +1 (healthy momentum)
        #   70-80  → +1.5 (strong trending RSI, treat as extra bullish)
        #   >80    → +1 (overbought but still counts, just less reliable)
        #   <40    → 0  (do NOT penalise — price bouncing up from low RSI is fine)
        # The old 'rsi_healthy: 40<RSI<70' penalised RSI>70, causing NEUTRAL
        # output during 87+ RSI rallies.  Fixed by splitting into tiers.
        if latest_rsi > 80:
            rsi_bullish_score = 1.0
        elif latest_rsi > 70:
            rsi_bullish_score = 1.5
        elif latest_rsi > 55:
            rsi_bullish_score = 1.0
        else:
            rsi_bullish_score = 0.0

        # Bearish RSI tiers (mirrored)
        if latest_rsi < 20:
            rsi_bearish_score = 1.0
        elif latest_rsi < 30:
            rsi_bearish_score = 1.5
        elif latest_rsi < 45:
            rsi_bearish_score = 1.0
        else:
            rsi_bearish_score = 0.0

        # IMPROVED SIGNAL DETECTION WITH MOMENTUM CONFIRMATION
        signals = {
            'sma_bullish': latest_sma_short > latest_sma_long,
            'sma_crossover': (sma_short.iloc[-2] <= sma_long.iloc[-2]) and (latest_sma_short > latest_sma_long),  # Recent crossover
            'ema_bullish': latest_ema_9 > latest_ema_21,
            'macd_bullish': latest_macd > latest_signal,
            'macd_histogram_positive': latest_histogram > 0,
            'macd_histogram_increasing': latest_histogram > prev_histogram,  # Momentum confirmation!
            'rsi_recovery': (prev_rsi < 45) and (latest_rsi > prev_rsi),  # Reversal from low
            'price_above_sma': current_price > latest_sma_short,
            'price_above_vwap': current_price > latest_vwap,
            'price_above_bb_mid': current_price > latest_bb_mid,
            'supertrend_bullish': latest_st_direction == 1,
            'stoch_rsi_healthy': 20 < latest_stoch_rsi < 80,  # Not extreme
            'volume_spike': volume_increase,  # Confirmation with volume
            'near_support': (current_price > latest_bb_lower) and (current_price < latest_bb_lower * 1.05),  # Near support
            'above_support': current_price > latest_bb_lower  # Not at bottom
        }
        
        # BEARISH EQUIVALENTS
        bearish_signals_list = {
            'sma_bearish': latest_sma_short < latest_sma_long,
            'sma_crossover': (sma_short.iloc[-2] >= sma_long.iloc[-2]) and (latest_sma_short < latest_sma_long),
            'ema_bearish': latest_ema_9 < latest_ema_21,
            'macd_bearish': latest_macd < latest_signal,
            'macd_histogram_negative': latest_histogram < 0,
            'macd_histogram_decreasing': latest_histogram < prev_histogram,
            'rsi_recovery': (prev_rsi > 55) and (latest_rsi < prev_rsi),  # Reversal from high
            'price_below_sma': current_price < latest_sma_short,
            'price_below_vwap': current_price < latest_vwap,
            'price_below_bb_mid': current_price < latest_bb_mid,
            'supertrend_bearish': latest_st_direction == -1,
            'stoch_rsi_healthy': 20 < latest_stoch_rsi < 80,
            'volume_spike': volume_increase,
            'near_resistance': (current_price < latest_bb_upper) and (current_price > latest_bb_upper * 0.95),
            'below_resistance': current_price < latest_bb_upper
        }
        
        # EMA weight: boosted in trending regime so fast EMA crossover is decisive
        is_trending = regime in ("TRENDING UP", "TRENDING DOWN") if regime else False
        ema_weight = 3.0 if is_trending else 1.5

        # CALCULATE BULLISH SCORE (with confidence weighting)
        bullish_score = (
            signals['sma_bullish'] * 2 +  # Double weight for SMA
            signals['sma_crossover'] * 3 +  # Triple weight for crossover (strong signal)
            signals['ema_bullish'] * ema_weight +  # Boosted in trending regime
            signals['macd_bullish'] * 2 +
            signals['macd_histogram_positive'] * 1 +
            signals['macd_histogram_increasing'] * 2 +  # Momentum matters!
            signals['rsi_recovery'] * 2 +  # Reversal matters
            rsi_bullish_score * 1.5 +  # Tiered RSI: rewards high RSI in uptrends
            signals['price_above_sma'] * 1 +
            signals['price_above_vwap'] * 1.5 +
            signals['price_above_bb_mid'] * 1 +
            signals['supertrend_bullish'] * 2 +
            signals['stoch_rsi_healthy'] * 0.5 +
            signals['volume_spike'] * 2 +  # Volume confirmation
            signals['above_support'] * 1
        )
        
        # CALCULATE BEARISH SCORE
        bearish_score = (
            bearish_signals_list['sma_bearish'] * 2 +
            bearish_signals_list['sma_crossover'] * 3 +
            bearish_signals_list['ema_bearish'] * ema_weight +  # Boosted in trending regime
            bearish_signals_list['macd_bearish'] * 2 +
            bearish_signals_list['macd_histogram_negative'] * 1 +
            bearish_signals_list['macd_histogram_decreasing'] * 2 +
            bearish_signals_list['rsi_recovery'] * 2 +
            rsi_bearish_score * 1.5 +  # Tiered RSI: rewards low RSI in downtrends
            bearish_signals_list['price_below_sma'] * 1 +
            bearish_signals_list['price_below_vwap'] * 1.5 +
            bearish_signals_list['price_below_bb_mid'] * 1 +
            bearish_signals_list['supertrend_bearish'] * 2 +
            bearish_signals_list['stoch_rsi_healthy'] * 0.5 +
            bearish_signals_list['volume_spike'] * 2 +
            bearish_signals_list['below_resistance'] * 1
        )
        
        # MAXIMUM POSSIBLE SCORE = 30 (for normalization)
        max_score = 30
        
        # DETERMINE TREND AND STRENGTH (NOW WITH ADAPTIVE CONFIDENCE THRESHOLD)
        bullish_strength = min(int((bullish_score / max_score) * 100), 100)
        bearish_strength = min(int((bearish_score / max_score) * 100), 100)
        
        # ADAPTIVE THRESHOLD: Lower in volatile markets (war/crisis), higher in calm markets
        HIGH_CONFIDENCE_THRESHOLD = self.get_dynamic_confidence_threshold(latest_atr, current_price)
        
        logger.info(f"📊 Signal Analysis: Bullish {bullish_strength}% | Bearish {bearish_strength}% | ATR ₹{latest_atr:.2f} | Threshold {HIGH_CONFIDENCE_THRESHOLD}%")
        
        if bullish_strength >= HIGH_CONFIDENCE_THRESHOLD and bullish_strength > bearish_strength:
            trend = Trend.BULLISH
            recommendation = "BUY CE"
            strength = bullish_strength
        elif bearish_strength >= HIGH_CONFIDENCE_THRESHOLD and bearish_strength > bullish_strength:
            trend = Trend.BEARISH
            recommendation = "BUY PE"
            strength = bearish_strength
        else:
            # WAIT FOR CONFIRMATION - Don't trade weak signals!
            trend = Trend.NEUTRAL
            recommendation = "HOLD - Wait for confirmation"
            strength = max(bullish_strength, bearish_strength)
        
        # Build detailed analysis
        details = {
            'bullish_score': bullish_score,
            'bearish_score': bearish_score,
            'bullish_strength': bullish_strength,
            'bearish_strength': bearish_strength,
            'threshold': HIGH_CONFIDENCE_THRESHOLD,
            'price_change_pct': ((current_price - prev_price) / prev_price) * 100,
            'volume': latest_volume,
            'volume_avg': avg_volume,
            'volume_spike': volume_increase
        }
        
        signal = TrendSignal(
            trend=trend,
            strength=strength,
            current_price=current_price,
            sma_short=latest_sma_short,
            sma_long=latest_sma_long,
            rsi=latest_rsi,
            macd=latest_macd,
            macd_signal=latest_signal,
            timestamp=datetime.now(),
            recommendation=recommendation,
            details=details,
            ema_9=latest_ema_9,
            ema_21=latest_ema_21,
            vwap=latest_vwap,
            bollinger_upper=latest_bb_upper,
            bollinger_lower=latest_bb_lower,
            bollinger_mid=latest_bb_mid,
            atr=latest_atr,
            supertrend=latest_supertrend,
            supertrend_direction="UP" if latest_st_direction == 1 else "DOWN",
            stoch_rsi=latest_stoch_rsi
        )
        
        self._last_signal = signal
        logger.info(f"Analysis: {trend.value} (strength: {strength}%, bull: {bullish_strength}%, bear: {bearish_strength}%) - {recommendation}")
        
        return signal
    
    @staticmethod
    def _human_expiry_text(days: int) -> str:
        """Return a human-readable string for days-to-expiry."""
        if days <= 0:
            return "TODAY (Expiry Day!)"
        if days == 1:
            return "Tomorrow"
        return f"{days} days away"

    def _get_next_expiry(self) -> dict:
        """Calculate the next expiry based on the active index config."""
        today = datetime.now().date()
        exp_weekday = self._index.expiry_weekday

        if self._index.weekly_expiry:
            # Weekly expiry: find the next occurrence of exp_weekday
            days_until = (exp_weekday - today.weekday()) % 7
            if days_until == 0 and datetime.now().hour < 15:
                next_exp = today
            elif days_until == 0:
                next_exp = today + timedelta(days=7)
            else:
                next_exp = today + timedelta(days=days_until)
        else:
            # Monthly-only (e.g. BANKNIFTY): find last <exp_weekday> of this/next month
            next_exp = self._last_weekday_of_month(today, exp_weekday)
            if next_exp < today or (next_exp == today and datetime.now().hour >= 15):
                if today.month == 12:
                    first_next = today.replace(year=today.year + 1, month=1, day=1)
                else:
                    first_next = today.replace(month=today.month + 1, day=1)
                next_exp = self._last_weekday_of_month(first_next, exp_weekday)

        # Determine if this is a monthly expiry (last <weekday> of month)
        last_of_month = self._last_weekday_of_month(next_exp, exp_weekday)
        is_monthly = next_exp == last_of_month

        days_to_expiry = (next_exp - today).days
        if self._index.weekly_expiry:
            expiry_label = "MONTHLY" if is_monthly else "WEEKLY"
        else:
            expiry_label = "MONTHLY"
        human_text = self._human_expiry_text(days_to_expiry)

        return {
            "date": next_exp,
            "display": next_exp.strftime("%d-%b-%Y"),
            "short": f"{next_exp.day}{next_exp.strftime('%b').lower()}",
            "days": days_to_expiry,
            "days_text": human_text,
            "label": expiry_label,
            "is_monthly": is_monthly,
        }

    @staticmethod
    def _last_weekday_of_month(ref_date, weekday: int):
        """Return the last occurrence of `weekday` in ref_date's month."""
        if ref_date.month == 12:
            next_month_first = ref_date.replace(year=ref_date.year + 1, month=1, day=1)
        else:
            next_month_first = ref_date.replace(month=ref_date.month + 1, day=1)
        last_day = next_month_first - timedelta(days=1)
        offset = (last_day.weekday() - weekday) % 7
        return last_day - timedelta(days=offset)

    def get_option_strike_suggestion(self, trend_signal: TrendSignal) -> Dict:
        """
        Suggest appropriate option strike prices based on current trend.
        Includes the next expiry date for clarity.
        """
        current_price = trend_signal.current_price
        strike_interval = self._index.strike_interval
        atm_strike = round(current_price / strike_interval) * strike_interval
        expiry = self._get_next_expiry()

        base = {
            'atm_strike': atm_strike,
            'expiry_date': expiry["display"],
            'expiry_label': expiry["label"],
            'days_to_expiry': expiry["days"],
            'days_text': expiry["days_text"],
            'expiry_short': expiry["short"],
        }

        if trend_signal.trend == Trend.BULLISH:
            return {
                **base,
                'option_type': 'CE',
                'itm_strike': atm_strike - strike_interval,
                'otm_strike': atm_strike + strike_interval,
                'suggested': atm_strike,
                'reason': f"Bullish trend - RSI: {trend_signal.rsi:.1f}, Price above SMA"
            }
        elif trend_signal.trend == Trend.BEARISH:
            return {
                **base,
                'option_type': 'PE',
                'itm_strike': atm_strike + strike_interval,
                'otm_strike': atm_strike - strike_interval,
                'suggested': atm_strike,
                'reason': f"Bearish trend - RSI: {trend_signal.rsi:.1f}, Price below SMA"
            }
        else:
            return {
                **base,
                'option_type': None,
                'suggested': None,
                'reason': "Neutral trend - No clear direction, avoid trading"
            }
    
    def format_analysis_report(self, signal: TrendSignal) -> str:
        """Format trend signal as a readable report"""
        
        strike_suggestion = self.get_option_strike_suggestion(signal)
        
        # Determine signal counts
        bullish_count = signal.details.get('bullish_count', 0)
        bearish_count = signal.details.get('bearish_count', 0)
        total_signals = signal.details.get('total_signals', 8)
        
        # Supertrend direction indicator
        st_arrow = "^" if signal.supertrend_direction == "UP" else "v"
        
        # Build strike suggestion block
        suggested = strike_suggestion.get('suggested')
        opt_type = strike_suggestion.get('option_type')
        expiry_label = strike_suggestion.get('expiry_label', '')
        expiry_date = strike_suggestion.get('expiry_date', 'N/A')
        days_text = strike_suggestion.get('days_text', '')
        expiry_short = strike_suggestion.get('expiry_short', '')

        if suggested and opt_type:
            strike_block = f"""  STRIKE SUGGESTION:
  - Suggested: {suggested} {opt_type} ({expiry_label})
  - Expiry: {expiry_date} -- {days_text}
  - ATM Strike: {strike_suggestion.get('atm_strike', 'N/A')}
  - Option Type: {opt_type}
  - Reason: {strike_suggestion.get('reason', 'N/A')}
  
  >>> To deep-analyze, type: strike {suggested} {opt_type} {expiry_short}"""
        else:
            strike_block = f"""  STRIKE SUGGESTION:
  - No trade suggested (Neutral trend)
  - ATM Strike: {strike_suggestion.get('atm_strike', 'N/A')}
  - Next Expiry: {expiry_date} ({expiry_label}) -- {days_text}
  - Reason: {strike_suggestion.get('reason', 'N/A')}"""

        idx_name = self._index.display_name
        report = f"""
================================================================
       {idx_name} TREND ANALYSIS REPORT                     
================================================================
  Time: {signal.timestamp.strftime('%Y-%m-%d %H:%M:%S')}
  
  CURRENT PRICE: Rs.{signal.current_price:,.2f}
  
  TREND: {signal.trend.value} (Strength: {signal.strength:.0f}%)
  RECOMMENDATION: {signal.recommendation}
  Signals: {bullish_count} Bullish / {bearish_count} Bearish (of {total_signals})
  
----------------------------------------------------------------
  MOVING AVERAGES:
  - SMA(20): Rs.{signal.sma_short:,.2f}
  - SMA(50): Rs.{signal.sma_long:,.2f}
  - EMA(9):  Rs.{signal.ema_9:,.2f}
  - EMA(21): Rs.{signal.ema_21:,.2f}
  - VWAP:    Rs.{signal.vwap:,.2f}
  
----------------------------------------------------------------
  MOMENTUM INDICATORS:
  - RSI(14): {signal.rsi:.2f} {'(Oversold!)' if signal.rsi < 30 else '(Overbought!)' if signal.rsi > 70 else ''}
  - Stoch RSI: {signal.stoch_rsi:.2f} {'(Oversold!)' if signal.stoch_rsi < 20 else '(Overbought!)' if signal.stoch_rsi > 80 else ''}
  - MACD: {signal.macd:.2f}
  - Signal: {signal.macd_signal:.2f}
  
----------------------------------------------------------------
  TREND INDICATORS:
  - Supertrend: Rs.{signal.supertrend:,.2f} ({st_arrow} {signal.supertrend_direction})
  - Bollinger Upper: Rs.{signal.bollinger_upper:,.2f}
  - Bollinger Mid:   Rs.{signal.bollinger_mid:,.2f}
  - Bollinger Lower: Rs.{signal.bollinger_lower:,.2f}
  
----------------------------------------------------------------
  VOLATILITY:
  - ATR(14): Rs.{signal.atr:,.2f}
  
----------------------------------------------------------------
{strike_block}
  
================================================================
"""
        return report


# Standalone testing
if __name__ == "__main__":
    import sys
    from loguru import logger
    
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    
    analyzer = TrendAnalyzer()
    signal = analyzer.analyze()
    
    if signal:
        print(analyzer.format_analysis_report(signal))
    else:
        print("Could not analyze trend - check data availability")
