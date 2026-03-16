"""
Market Regime Detector

Classifies the current market into one of four regimes:
  TRENDING_UP   - Strong directional move up, good for CE
  TRENDING_DOWN - Strong directional move down, good for PE
  RANGING       - Sideways chop, AVOID trading (trend signals will whipsaw)
  VOLATILE      - High volatility with no direction, DANGEROUS

Uses ADX (Average Directional Index), ATR percentile, and Hurst exponent
to determine regime. This prevents the bot from entering trades during
choppy markets where trend-following strategies get destroyed.

Chat commands:
    regime        - Show current market regime
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
from loguru import logger
from enum import Enum

from bot.index_config import IndexConfig, NIFTY


class Regime(Enum):
    TRENDING_UP = "TRENDING UP"
    TRENDING_DOWN = "TRENDING DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"


@dataclass
class RegimeAnalysis:
    regime: Regime
    adx: float            # 0-100, >25 = trending
    atr_percentile: float # where current ATR sits vs last 60 days
    hurst: float          # <0.5 mean-reverting, 0.5 random, >0.5 trending
    trend_quality: float  # 0-100 composite score
    message: str
    should_trade: bool
    timestamp: datetime


class MarketRegimeDetector:
    """
    Detects market regime to filter out bad trading conditions.

    The bot's trend-following strategy works well in TRENDING regimes
    but gets whipsawed in RANGING markets and blown up in VOLATILE markets.
    """

    def __init__(self):
        self._index: IndexConfig = NIFTY
        self._last_regime: Optional[RegimeAnalysis] = None

    def set_index(self, idx: IndexConfig):
        self._index = idx

    def analyze(self) -> Optional[RegimeAnalysis]:
        try:
            ticker = yf.Ticker(self._index.yahoo_symbol)
            data = ticker.history(period="3mo", interval="1d")
        except Exception as e:
            logger.error(f"Regime detection data fetch failed: {e}")
            return None

        if data.empty or len(data) < 60:
            return None

        adx = self._calculate_adx(data)
        atr_pctile = self._atr_percentile(data)
        hurst = self._hurst_exponent(data["Close"].values)

        last_close = data["Close"].iloc[-1]
        sma_20 = data["Close"].rolling(20).mean().iloc[-1]
        sma_50 = data["Close"].rolling(50).mean().iloc[-1]
        above_sma = last_close > sma_20 > sma_50

        # Classification logic
        if adx > 25 and hurst > 0.55:
            if above_sma:
                regime = Regime.TRENDING_UP
                msg = "Strong uptrend detected. Trend-following signals are reliable."
                should_trade = True
            else:
                regime = Regime.TRENDING_DOWN
                msg = "Strong downtrend detected. Put-side signals are reliable."
                should_trade = True
        elif adx < 20 and atr_pctile < 40:
            regime = Regime.RANGING
            msg = "Market is choppy/sideways. Trend signals will WHIPSAW. Avoid option buying."
            should_trade = False
        elif atr_pctile > 80:
            regime = Regime.VOLATILE
            msg = "Extremely high volatility. IV is inflated, premiums are expensive. Trade with caution."
            should_trade = adx > 30  # only if directional
        else:
            if adx > 20:
                regime = Regime.TRENDING_UP if above_sma else Regime.TRENDING_DOWN
                msg = "Moderate trend. Signals are somewhat reliable."
                should_trade = True
            else:
                regime = Regime.RANGING
                msg = "Weak trend. Better to stay on the sidelines."
                should_trade = False

        trend_quality = min(100, (adx / 40 * 50) + (hurst * 50))

        result = RegimeAnalysis(
            regime=regime,
            adx=round(adx, 1),
            atr_percentile=round(atr_pctile, 1),
            hurst=round(hurst, 3),
            trend_quality=round(trend_quality, 1),
            message=msg,
            should_trade=should_trade,
            timestamp=datetime.now(),
        )
        self._last_regime = result
        return result

    def _calculate_adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """Average Directional Index -- measures trend strength regardless of direction."""
        high = df["High"]
        low = df["Low"]
        close = df["Close"]

        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

        tr = pd.concat([
            high - low,
            abs(high - close.shift()),
            abs(low - close.shift())
        ], axis=1).max(axis=1)

        atr = tr.rolling(period).mean()
        plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(period).mean() / atr)

        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.rolling(period).mean()

        return float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 20.0

    def _atr_percentile(self, df: pd.DataFrame, period: int = 14, lookback: int = 60) -> float:
        """Where current ATR sits relative to last N days."""
        tr = pd.concat([
            df["High"] - df["Low"],
            abs(df["High"] - df["Close"].shift()),
            abs(df["Low"] - df["Close"].shift())
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()

        current_atr = atr.iloc[-1]
        historical = atr.iloc[-lookback:]
        percentile = (historical < current_atr).sum() / len(historical) * 100
        return float(percentile)

    def _hurst_exponent(self, series: np.ndarray, max_lag: int = 20) -> float:
        """
        Hurst exponent estimates whether a time series is trending or mean-reverting.
        H > 0.5: trending (persistent), H < 0.5: mean-reverting, H = 0.5: random walk.
        """
        if len(series) < max_lag * 3:
            return 0.5

        lags = range(2, max_lag)
        tau = []
        for lag in lags:
            diff = series[lag:] - series[:-lag]
            std = np.std(diff)
            if std > 0:
                tau.append(std)
            else:
                tau.append(0.001)

        if len(tau) < 2:
            return 0.5

        log_lags = np.log(list(lags)[:len(tau)])
        log_tau = np.log(tau)

        try:
            poly = np.polyfit(log_lags, log_tau, 1)
            return float(poly[0])
        except:
            return 0.5

    def format_analysis(self, r: RegimeAnalysis) -> str:
        trade_light = "GREEN" if r.should_trade else "RED"
        trade_icon = ">>>" if r.should_trade else "XXX"

        return f"""
================================================================
  {self._index.display_name} MARKET REGIME ANALYSIS
  {r.timestamp.strftime('%d %b %Y %H:%M')}
================================================================

  REGIME: {r.regime.value}
  TRADE SIGNAL: {trade_icon} {trade_light} {trade_icon}

  {r.message}

----------------------------------------------------------------
  INDICATORS
----------------------------------------------------------------
  ADX (Trend Strength):     {r.adx:.1f}  {"(Strong)" if r.adx > 25 else "(Weak)"}
  ATR Percentile:           {r.atr_percentile:.0f}%  {"(High Vol)" if r.atr_percentile > 70 else "(Normal)" if r.atr_percentile > 30 else "(Low Vol)"}
  Hurst Exponent:           {r.hurst:.3f}  {"(Trending)" if r.hurst > 0.55 else "(Random)" if r.hurst > 0.45 else "(Mean-Reverting)"}
  Trend Quality Score:      {r.trend_quality:.0f}/100

----------------------------------------------------------------
  WHAT THIS MEANS
----------------------------------------------------------------
  {"Your trend-following signals are RELIABLE right now." if r.should_trade else "Your trend-following signals will WHIPSAW. Stay out or use range strategies."}
  {"Option buying is favorable -- directional moves are likely." if r.should_trade and r.atr_percentile < 70 else ""}
  {"IV is HIGH -- premiums are expensive. Consider selling instead of buying." if r.atr_percentile > 70 else ""}
  {"ADX > 25 confirms a strong trend is in play." if r.adx > 25 else "ADX < 25 means no strong trend -- most signals are noise."}
================================================================
"""


regime_detector = MarketRegimeDetector()
