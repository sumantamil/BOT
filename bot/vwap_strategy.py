"""
VWAP Mean Reversion Strategy

Used automatically when the market regime is RANGING (sideways).
Price oscillates around VWAP intraday — buy dips below VWAP, sell pops above.

Logic:
  LONG  signal : price is > VWAP_DEVIATION_PCT % below VWAP
                 AND RSI < RSI_OVERSOLD (confirms oversold dip)
                 AND volume spike (price move is real, not thin)
                 AND price is above intraday support (BB lower band)

  SHORT signal : price is > VWAP_DEVIATION_PCT % above VWAP
                 AND RSI > RSI_OVERBOUGHT (confirms overbought pop)
                 AND volume spike
                 AND price is below intraday resistance (BB upper band)

  Target  : VWAP itself  (mean reversion target)
  Stop    : 0.3% beyond the deviation level that triggered the signal

Only active during RANGING regime (enforced by the engine's strategy selector).
One signal per direction per day — if both fire, only the first is acted on.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, time, date
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Optional, Dict
from enum import Enum
from loguru import logger

import sys
sys.path.append('..')
from config import settings
from bot.index_config import IndexConfig, NIFTY


class VWAPState(Enum):
    WAITING   = "WAITING"    # Before market open
    WATCHING  = "WATCHING"   # Actively looking for deviations
    TRIGGERED = "TRIGGERED"  # Signal fired (1 signal/direction/day limit)
    EXPIRED   = "EXPIRED"    # Past trading hours


@dataclass
class VWAPSignal:
    """Mean-reversion signal emitted by VWAP strategy"""
    direction: str       # "LONG" → buy CE  |  "SHORT" → buy PE
    current_price: float
    vwap: float
    deviation_pct: float  # how far price is from VWAP (%)
    rsi: float
    stop_loss: float
    target: float         # VWAP is the reversion target
    strength: float       # 0–100 confidence
    volume_confirmed: bool
    timestamp: datetime


class VWAPStrategy:
    """
    VWAP Mean Reversion strategy.

    Detects when NIFTY/BANKNIFTY/SENSEX is significantly below or above
    its intraday VWAP with RSI and volume confirmation, then fires a
    mean-reversion signal (buy the dip or sell the pop).

    Designed to be active only during RANGING market regimes.
    """

    def __init__(self, index_config: IndexConfig = None):
        self._index = index_config or NIFTY
        self._cfg = settings.vwap

        self._today: Optional[date] = None
        self._long_fired: bool = False
        self._short_fired: bool = False
        self._state: VWAPState = VWAPState.WAITING

    # ─────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────

    def set_index(self, index_config: IndexConfig):
        if self._index.name == index_config.name:
            return  # already on this index — skip unnecessary reset
        self._index = index_config
        self._reset_daily()

    def analyze(self) -> Optional[VWAPSignal]:
        """
        Run VWAP mean-reversion analysis.

        Returns:
            VWAPSignal if a deviation+confirmation setup is detected, else None.
        """
        # Respect 'vwap off' command — mirrors the same guard in ORBStrategy
        if not self._cfg.enabled:
            return None

        self._ensure_daily_reset()

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        t = now.time()

        if t < time(9, 30):   # Let VWAP settle for the first 15 min
            self._state = VWAPState.WAITING
            return None

        if t >= time(14, 30):  # No new reversions in the last hour
            self._state = VWAPState.EXPIRED
            return None

        logger.debug(f"VWAP [{self._index.display_name}]: analyze() called at {now.strftime('%H:%M:%S')}")
        self._state = VWAPState.WATCHING

        data = self._fetch_intraday()
        if data is None or data.empty or len(data) < 5:
            return None

        try:
            latest = data.iloc[-1]
            price  = float(latest["Close"])
            volume = float(latest.get("Volume", 0))

            vwap        = self._calc_vwap(data)
            rsi         = self._calc_rsi(data)
            bb_upper, bb_lower = self._calc_bb(data)
            avg_vol     = float(data["Volume"].iloc[-20:].mean()) if "Volume" in data.columns and len(data) >= 20 else 0

            if vwap == 0:
                logger.warning(f"VWAP [{self._index.display_name}]: VWAP is zero — skipping analysis")
                return None

            deviation_pct = (price - vwap) / vwap * 100
            volume_ok     = (avg_vol > 0) and (volume >= avg_vol * 1.15)
        except Exception:
            logger.exception(f"VWAP [{self._index.display_name}]: indicator calculation failed")
            return None

        cfg = self._cfg

        # ── LONG setup: price dipped below VWAP ──
        if (deviation_pct <= -cfg.deviation_pct
                and rsi < cfg.rsi_oversold
                and price > bb_lower        # still above support band
                and not self._long_fired):

            stop_loss = price * (1 - cfg.stop_pct / 100)
            signal = VWAPSignal(
                direction="LONG",
                current_price=price,
                vwap=round(vwap, 2),
                deviation_pct=round(deviation_pct, 2),
                rsi=round(rsi, 1),
                stop_loss=round(stop_loss, 2),
                target=round(vwap, 2),
                strength=self._score(abs(deviation_pct), rsi, volume_ok, long=True),
                volume_confirmed=volume_ok,
                timestamp=now,
            )
            self._long_fired = True
            self._state = VWAPState.TRIGGERED
            logger.info(
                f"VWAP LONG [{self._index.display_name}]: price={price:.2f}  "
                f"vwap={vwap:.2f}  dev={deviation_pct:.2f}%  rsi={rsi:.1f}  "
                f"strength={signal.strength:.0f}%"
            )
            return signal

        # ── SHORT setup: price popped above VWAP ──
        if (deviation_pct >= cfg.deviation_pct
                and rsi > cfg.rsi_overbought
                and price < bb_upper        # still below resistance band
                and not self._short_fired):

            stop_loss = price * (1 + cfg.stop_pct / 100)
            signal = VWAPSignal(
                direction="SHORT",
                current_price=price,
                vwap=round(vwap, 2),
                deviation_pct=round(deviation_pct, 2),
                rsi=round(rsi, 1),
                stop_loss=round(stop_loss, 2),
                target=round(vwap, 2),
                strength=self._score(abs(deviation_pct), rsi, volume_ok, long=False),
                volume_confirmed=volume_ok,
                timestamp=now,
            )
            self._short_fired = True
            self._state = VWAPState.TRIGGERED
            logger.info(
                f"VWAP SHORT [{self._index.display_name}]: price={price:.2f}  "
                f"vwap={vwap:.2f}  dev={deviation_pct:.2f}%  rsi={rsi:.1f}  "
                f"strength={signal.strength:.0f}%"
            )
            return signal

        # ── Neither condition met — log why (throttled to once per 10 min) ───
        _now_min = now.hour * 60 + now.minute
        _last_log = getattr(self, '_last_no_signal_log_min', -999)
        if _now_min - _last_log >= 10:
            self._last_no_signal_log_min = _now_min
            _vol_tag = "vol✓" if volume_ok else f"vol✗(cur={volume:.0f} avg={avg_vol:.0f})"
            _now_str = now.strftime("%H:%M:%S")
            if self._long_fired and self._short_fired:
                logger.info(
                    f"VWAP [{self._index.display_name}] {_now_str} → SKIP "
                    f"| both signals already fired today"
                )
            else:
                # Build a per-condition PASS/FAIL breakdown
                _long_dev  = "✓" if deviation_pct <= -cfg.deviation_pct else f"✗(dev={deviation_pct:+.2f}%,need<-{cfg.deviation_pct:.1f}%)"
                _long_rsi  = "✓" if rsi < cfg.rsi_oversold else f"✗(RSI={rsi:.0f},need<{cfg.rsi_oversold:.0f})"
                _long_bb   = "✓" if float(data["Close"].iloc[-1]) > float((data["Close"].rolling(20).mean() - 2*data["Close"].rolling(20).std()).iloc[-1]) else "✗(below BB)"
                _short_dev = "✓" if deviation_pct >= cfg.deviation_pct else f"✗(dev={deviation_pct:+.2f}%,need>+{cfg.deviation_pct:.1f}%)"
                _short_rsi = "✓" if rsi > cfg.rsi_overbought else f"✗(RSI={rsi:.0f},need>{cfg.rsi_overbought:.0f})"
                logger.info(
                    f"VWAP [{self._index.display_name}] {_now_str} → FAIL "
                    f"| price={price:.0f} vwap={vwap:.0f} dev={deviation_pct:+.2f}% rsi={rsi:.0f} {_vol_tag} "
                    f"| LONG[dev={_long_dev} rsi={_long_rsi} bb={_long_bb}] "
                    f"| SHORT[dev={_short_dev} rsi={_short_rsi}]"
                )

        return None

    def get_status(self) -> Dict:
        self._ensure_daily_reset()
        return {
            "state": self._state.value,
            "long_fired": self._long_fired,
            "short_fired": self._short_fired,
            "index": self._index.display_name,
            "deviation_threshold_pct": self._cfg.deviation_pct,
        }

    def format_status(self) -> str:
        """Format VWAP strategy status for the chat console."""
        self._ensure_daily_reset()
        cfg = self._cfg

        data = self._fetch_intraday()
        vwap_line = "  (no intraday data yet)"
        if data is not None and not data.empty and len(data) >= 5:
            price   = float(data.iloc[-1]["Close"])
            vwap    = self._calc_vwap(data)
            rsi     = self._calc_rsi(data)
            dev_pct = (price - vwap) / vwap * 100
            long_trig  = vwap * (1 - cfg.deviation_pct / 100)
            short_trig = vwap * (1 + cfg.deviation_pct / 100)
            vwap_line = (
                f"\n"
                f"  Current Price : {price:>10,.2f}\n"
                f"  VWAP          : {vwap:>10,.2f}\n"
                f"  Deviation     : {dev_pct:>+9.2f}%\n"
                f"  RSI           : {rsi:>10.1f}\n"
                f"\n"
                f"  LONG  if Close < {long_trig:,.2f}  (dev <= -{cfg.deviation_pct}%)  AND RSI < {cfg.rsi_oversold}\n"
                f"  SHORT if Close > {short_trig:,.2f}  (dev >= +{cfg.deviation_pct}%)  AND RSI > {cfg.rsi_overbought}\n"
                f"  Target (both) : VWAP = {vwap:,.2f}\n"
            )

        fired = []
        if self._long_fired:
            fired.append("LONG")
        if self._short_fired:
            fired.append("SHORT")
        fired_str = ", ".join(fired) if fired else "None"

        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  VWAP MEAN REVERSION  —  {self._index.display_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  Status        : {self._state.value}\n"
            f"  Active window : 09:30 → 14:30 IST\n"
            f"{vwap_line}"
            f"  Signals fired : {fired_str}\n"
            f"  Stop distance : {cfg.stop_pct}% from entry\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  NOTE: Active only in RANGING regime."
        )

    # ─────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────

    def _ensure_daily_reset(self):
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        if self._today != today:
            self._reset_daily()

    def _reset_daily(self):
        self._today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        self._long_fired  = False
        self._short_fired = False
        self._state = VWAPState.WAITING
        logger.debug(f"VWAP: Daily reset [{self._index.display_name}]")

    def _fetch_intraday(self) -> Optional[pd.DataFrame]:
        try:
            ticker = yf.Ticker(self._index.yahoo_symbol)
            # Use 2d so yfinance has enough candles early in the session; filter to today after.
            data = ticker.history(period="2d", interval="5m")
            if data is not None and not data.empty:
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    _ist2 = _ZI("Asia/Kolkata")
                    _today_str = datetime.now(_ist2).strftime("%Y-%m-%d")
                    _idx_ist = data.index.tz_convert(_ist2) if data.index.tz is not None else data.index
                    _mask = _idx_ist.strftime("%Y-%m-%d") == _today_str
                    if _mask.any():
                        data = data[_mask]
                except Exception:
                    pass  # fall back to full 2d data if tz filtering fails
            if data is None or data.empty:
                logger.warning(
                    f"VWAP [{self._index.display_name}]: _fetch_intraday returned empty "
                    f"(symbol={self._index.yahoo_symbol}, period=2d, interval=5m)"
                )
                return None
            # Log row count + latest candle timestamp in IST
            try:
                from zoneinfo import ZoneInfo
                _ist = ZoneInfo("Asia/Kolkata")
                _last_ts = data.index[-1]
                _last_ts_ist = _last_ts.tz_convert(_ist) if hasattr(_last_ts, "tz_convert") else _last_ts
                _age_min = (datetime.now(_ist) - _last_ts_ist).total_seconds() / 60
                _stale = f" ⚠️ STALE ({_age_min:.0f} min old — yfinance lag?)" if _age_min > 20 else ""
                logger.debug(
                    f"VWAP [{self._index.display_name}]: fetched {len(data)} candles "
                    f"| latest={_last_ts_ist.strftime('%H:%M IST')} "
                    f"| age={_age_min:.0f}min{_stale}"
                )
            except Exception as _tz_err:
                logger.debug(
                    f"VWAP [{self._index.display_name}]: fetched {len(data)} candles "
                    f"(TZ check skipped: {_tz_err})"
                )
            return data
        except Exception as e:
            logger.error(f"VWAP [{self._index.display_name}]: data fetch failed — {e}")
            return None

    @staticmethod
    def _calc_vwap(data: pd.DataFrame) -> float:
        typical = (data["High"] + data["Low"] + data["Close"]) / 3
        if "Volume" in data.columns and data["Volume"].sum() > 0:
            vwap = (typical * data["Volume"]).cumsum() / data["Volume"].cumsum()
        else:
            vwap = typical.rolling(window=20).mean()
        return float(vwap.iloc[-1])

    @staticmethod
    def _calc_rsi(data: pd.DataFrame, period: int = 14) -> float:
        delta = data["Close"].diff()
        gain  = delta.where(delta > 0, 0).rolling(period).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = 100 - (100 / (1 + rs))
        val   = rsi.iloc[-1]
        return float(val) if not pd.isna(val) else 50.0

    @staticmethod
    def _calc_bb(data: pd.DataFrame, period: int = 20, std: float = 2.0):
        mid    = data["Close"].rolling(period).mean()
        sigma  = data["Close"].rolling(period).std()
        upper  = float((mid + std * sigma).iloc[-1])
        lower  = float((mid - std * sigma).iloc[-1])
        return upper, lower

    def _score(self, dev_pct: float, rsi: float, volume_ok: bool, long: bool) -> float:
        """Confidence 0–100."""
        score = 50.0
        # Deeper deviation → stronger reversion pull
        score += min(dev_pct * 5, 20.0)
        # RSI extremity
        if long:
            score += max(0, (self._cfg.rsi_oversold - rsi) * 0.5)
        else:
            score += max(0, (rsi - self._cfg.rsi_overbought) * 0.5)
        if volume_ok:
            score += 10.0
        return round(min(score, 100.0), 1)
