"""
Opening Range Breakout (ORB) Strategy

Captures the high/low of the first 30 minutes of trading (9:15–9:45 IST),
then trades breakouts above/below that range with volume confirmation.

Rules:
  - Build range  : 9:15 AM → 9:30 AM IST  (configurable window)
  - Enter trades : 9:30 AM → 11:30 AM IST (configurable end)
  - Stop loss    : opposite end of opening range
  - Target 1     : entry ± range_width × 1.5
  - Target 2     : entry ± range_width × 2.0
  - Max 1 trade per calendar day (no overtrading)
"""

import yfinance as yf
import pandas as pd
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


class ORBState(Enum):
    WAITING   = "WAITING"    # Before 9:15 or weekend
    BUILDING  = "BUILDING"   # 9:15–9:45: collecting opening range candles
    READY     = "READY"      # Range set, watching for breakout
    TRIGGERED = "TRIGGERED"  # Signal fired today (1 trade/day limit)
    EXPIRED   = "EXPIRED"    # Past entry window (11:30+) or past force-exit


@dataclass
class ORBSignal:
    """Breakout signal emitted by ORB strategy"""
    direction: str          # "LONG" → buy CE  |  "SHORT" → buy PE
    breakout_price: float
    range_high: float
    range_low: float
    range_width: float
    stop_loss: float
    target_1: float
    target_2: float
    strength: float         # 0–100 confidence score
    volume_confirmed: bool
    timestamp: datetime


class ORBStrategy:
    """
    Opening Range Breakout strategy.

    Watches the first ORB_WINDOW_MINUTES of the trading day to build a
    price range, then emits a LONG or SHORT signal when price breaks
    above/below that range with sufficient volume.

    One signal maximum per calendar day.
    """

    def __init__(self, index_config: IndexConfig = None):
        self._index = index_config or NIFTY
        cfg = settings.orb
        self._cfg = cfg

        # Intraday state — reset each calendar day
        self._today: Optional[date] = None
        self._range_high: float = 0.0
        self._range_low: float = 0.0
        self._range_width: float = 0.0
        self._range_avg_volume: float = 0.0
        self._state: ORBState = ORBState.WAITING
        self._signal_fired: bool = False
        self._breakout_count: int = 0   # consecutive closes that confirm the breakout

    # ─────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────

    def set_index(self, index_config: IndexConfig):
        """Switch to a different index and reset daily state."""
        if self._index.name == index_config.name:
            return  # already on this index -- skip unnecessary reset
        self._index = index_config
        # Preserve _signal_fired and _today: the 'one ORB trade per calendar day'
        # limit must survive index switches (and browser 'index sensex' commands).
        # Only reset range prices/volume so the new index builds a fresh range.
        fired  = self._signal_fired
        today  = self._today
        bcount = self._breakout_count
        self._reset_daily()
        self._signal_fired  = fired   # restore after _reset_daily cleared it
        self._today         = today   # keep the same date so daily guard stays active
        self._breakout_count = bcount # preserve mid-candle confirmation count
        if fired:
            self._state = ORBState.TRIGGERED  # keep in triggered state

    def analyze(self) -> Optional[ORBSignal]:
        """
        Run ORB analysis for the current moment.

        Returns:
            ORBSignal if a new breakout is detected, otherwise None.
        """
        self._ensure_daily_reset()
        cfg = self._cfg

        if not cfg.enabled:
            logger.debug(f"ORB [{self._index.display_name}]: disabled (ORB_ENABLED=false)")
            return None

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        t = now.time()

        # Before market open
        if t < self._market_open():
            self._state = ORBState.WAITING
            return None

        # Force-exit window (3:15 PM+)
        if t >= time(15, 15):
            self._state = ORBState.EXPIRED
            return None

        # Past entry window → no new signals
        if t >= self._entry_end():
            if self._state not in (ORBState.TRIGGERED, ORBState.EXPIRED):
                self._state = ORBState.EXPIRED
                logger.debug(
                    f"ORB [{self._index.display_name}]: entry window closed "
                    f"(past {self._entry_end().strftime('%H:%M')} IST) — no new signals"
                )
            return None

        # Still in range-building window
        if t < self._range_end():
            self._state = ORBState.BUILDING
            self._try_build_range()
            _rh, _rl = self._range_high, self._range_low
            if _rh > 0:
                logger.debug(
                    f"ORB [{self._index.display_name}]: BUILDING range "
                    f"{_rl:.0f}–{_rh:.0f} (window closes {self._range_end().strftime('%H:%M')})"
                )
            else:
                logger.debug(
                    f"ORB [{self._index.display_name}]: BUILDING — range not yet established"
                )
            return None

        # Already fired today → enforce 1 trade/day limit
        if self._signal_fired:
            self._state = ORBState.TRIGGERED
            logger.debug(f"ORB [{self._index.display_name}]: signal already fired today (1/day limit)")
            return None

        # Ensure range was built before checking breakout
        if self._range_high == 0.0:
            self._try_build_range()
            if self._range_high == 0.0:
                logger.info(
                    f"ORB [{self._index.display_name}]: READY but range is 0 — "
                    f"data fetch failed (yfinance issue?)"
                )
                return None

        self._state = ORBState.READY
        return self._check_breakout(now)

    def get_status(self) -> Dict:
        """Return current ORB state as a dict."""
        self._ensure_daily_reset()
        return {
            "state": self._state.value,
            "range_high": round(self._range_high, 2),
            "range_low": round(self._range_low, 2),
            "range_width": round(self._range_width, 2),
            "signal_fired": self._signal_fired,
            "index": self._index.display_name,
            "window_minutes": self._cfg.window_minutes,
        }

    def format_status(self) -> str:
        """Format ORB status for the chat console."""
        self._ensure_daily_reset()
        cfg = self._cfg
        re_t = self._range_end()
        ee_t = self._entry_end()
        buf  = cfg.breakout_buffer_pct / 100

        mo_t = self._market_open()
        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"  ORB STRATEGY  —  {self._index.display_name}",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"  Range Window : {mo_t.strftime('%H:%M')} → {re_t.strftime('%H:%M')} IST  ({cfg.window_minutes} min)",
            f"  Entry Window : {re_t.strftime('%H:%M')} → {ee_t.strftime('%H:%M')} IST",
            f"  Status       : {self._state.value}",
        ]

        if self._range_high > 0:
            ul = self._range_high * (1 + buf)
            ll = self._range_low  * (1 - buf)
            lines += [
                "",
                f"  Range High   : {self._range_high:>10,.2f}",
                f"  Range Low    : {self._range_low:>10,.2f}",
                f"  Range Width  : {self._range_width:>10,.2f}  pts",
                "",
                f"  LONG  if Close > {ul:,.2f}  →  BUY CE",
                f"  SHORT if Close < {ll:,.2f}  →  BUY PE",
                "",
                f"  Targets      : ×{cfg.target_multiplier} (T1)  /  ×{cfg.target_multiplier_2} (T2)",
                f"  Stop Loss    : opposite end of opening range",
            ]
        elif self._state == ORBState.BUILDING:
            lines.append("  Collecting opening range candles…")
        else:
            lines.append("  Waiting for 09:15 IST market open.")

        if self._signal_fired:
            lines += ["", "  NOTE: Signal already fired today (1 trade/day limit)."]

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    # ─────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────

    @staticmethod
    def _market_open() -> time:
        return time(9, 15)

    def _range_end(self) -> time:
        mins = 15 + self._cfg.window_minutes
        return time(9 + mins // 60, mins % 60)

    def _entry_end(self) -> time:
        return time(self._cfg.entry_end_hour, self._cfg.entry_end_minute)

    def _ensure_daily_reset(self):
        today = datetime.now().date()
        if self._today != today:
            self._reset_daily()

    def _reset_daily(self):
        self._today = datetime.now().date()
        self._range_high = 0.0
        self._range_low  = 0.0
        self._range_width = 0.0
        self._range_avg_volume = 0.0
        self._state = ORBState.WAITING
        self._signal_fired = False
        self._breakout_count = 0
        logger.debug(f"ORB: Daily reset [{self._index.display_name}] for {self._today}")

    def _fetch_intraday(self) -> pd.DataFrame:
        try:
            ticker = yf.Ticker(self._index.yahoo_symbol)
            # Use 2d period so yfinance always has enough candles even early in the session.
            # Filter to today's date afterwards so ORB range stays intraday-only.
            data = ticker.history(period="2d", interval="5m")
            if not data.empty:
                try:
                    from zoneinfo import ZoneInfo
                    _ist = ZoneInfo("Asia/Kolkata")
                    _today_str = datetime.now(_ist).strftime("%Y-%m-%d")
                    _idx_ist = data.index.tz_convert(_ist) if data.index.tz is not None else data.index
                    _today_mask = _idx_ist.strftime("%Y-%m-%d") == _today_str
                    if _today_mask.any():
                        data = data[_today_mask]
                except Exception:
                    pass  # fall back to full 2d data if tz filtering fails
            if data.empty:
                logger.warning(
                    f"ORB [{self._index.display_name}]: _fetch_intraday returned empty "
                    f"(symbol={self._index.yahoo_symbol}, period=2d, interval=5m)"
                )
                return pd.DataFrame()
            # Log row count + latest candle timestamp in IST
            try:
                from zoneinfo import ZoneInfo
                _ist = ZoneInfo("Asia/Kolkata")
                _last_ts = data.index[-1]
                _last_ts_ist = _last_ts.tz_convert(_ist) if hasattr(_last_ts, "tz_convert") else _last_ts
                _age_min = (datetime.now(_ist) - _last_ts_ist).total_seconds() / 60
                _stale = f" ⚠️ STALE ({_age_min:.0f} min old — yfinance lag?)" if _age_min > 20 else ""
                logger.debug(
                    f"ORB [{self._index.display_name}]: fetched {len(data)} candles "
                    f"| latest={_last_ts_ist.strftime('%H:%M IST')} "
                    f"| age={_age_min:.0f}min{_stale}"
                )
            except Exception as _tz_err:
                logger.debug(
                    f"ORB [{self._index.display_name}]: fetched {len(data)} candles "
                    f"(TZ check skipped: {_tz_err})"
                )
            return data
        except Exception as e:
            logger.error(f"ORB [{self._index.display_name}]: data fetch failed — {e}")
            return pd.DataFrame()

    def _try_build_range(self):
        """Fetch today's 5m candles and extract the opening range high/low."""
        data = self._fetch_intraday()
        if data.empty:
            return

        market_open = self._market_open()
        range_end   = self._range_end()

        try:
            if hasattr(data.index, "tz") and data.index.tz is not None:
                from zoneinfo import ZoneInfo
                ist = ZoneInfo("Asia/Kolkata")
                local_idx = data.index.tz_convert(ist)
            else:
                local_idx = data.index

            mask = [
                (market_open <= ts.time() < range_end)
                for ts in local_idx
            ]
            or_candles = data[mask]
        except Exception as e:
            logger.warning(f"ORB: TZ conversion failed ({e}), using first {self._cfg.window_minutes // 5} candles")
            or_candles = data.head(self._cfg.window_minutes // 5)

        if len(or_candles) < 2:
            logger.debug(f"ORB: Only {len(or_candles)} opening range candles so far")
            return

        self._range_high = float(or_candles["High"].max())
        self._range_low  = float(or_candles["Low"].min())
        self._range_width = self._range_high - self._range_low

        if "Volume" in or_candles.columns:
            self._range_avg_volume = float(or_candles["Volume"].mean())

        logger.info(
            f"ORB range built [{self._index.display_name}]: "
            f"H={self._range_high:.2f}  L={self._range_low:.2f}  "
            f"W={self._range_width:.2f}  ({len(or_candles)} candles)"
        )

    def _check_breakout(self, now: datetime) -> Optional[ORBSignal]:
        """Fetch latest price and check if it has broken the opening range."""
        data = self._fetch_intraday()
        if data.empty:
            logger.warning(
                f"ORB [{self._index.display_name}]: breakout check skipped "
                f"— no intraday data returned"
            )
            return None

        # Require a minimum range width to avoid noise breakouts on ultra-low-
        # volatility mornings.  Use 1.5× the index strike interval as minimum:
        #   NIFTY     (interval=50)  → need ≥ 75 pts
        #   BANKNIFTY (interval=100) → need ≥ 150 pts
        #   SENSEX    (interval=100) → need ≥ 150 pts
        # A narrow opening range means the market is in a tiny equilibrium zone;
        # any "breakout" is more likely to be noise than a genuine directional move.
        _min_range = self._index.strike_interval * 1.5
        if self._range_width < _min_range:
            logger.info(
                f"ORB [{self._index.display_name}]: Skipping — opening range "
                f"{self._range_width:.1f} pts too narrow (need ≥{_min_range:.0f} pts)"
            )
            return None

        latest = data.iloc[-1]
        price  = float(latest["Close"])
        volume = float(latest.get("Volume", 0))
        cfg    = self._cfg

        # ── Stale data guard ──────────────────────────────────────────────────
        # yfinance 5m data can lag up to 15-20 min in practice (free tier).
        # Skip only if the last candle is older than 25 minutes — beyond that
        # the breakout has very likely already reversed or been missed entirely.
        try:
            from zoneinfo import ZoneInfo
            _ist = ZoneInfo("Asia/Kolkata")
            _last_ts = data.index[-1]
            if hasattr(_last_ts, "tz_convert"):
                _last_ts = _last_ts.tz_convert(_ist)
            elif hasattr(_last_ts, "replace"):
                _last_ts = _last_ts.replace(tzinfo=_ist)
            _candle_age_min = (datetime.now(_ist) - _last_ts).total_seconds() / 60
            if _candle_age_min > 25:
                logger.debug(
                    f"ORB: Data stale ({_candle_age_min:.1f} min old) — "
                    f"skipping breakout check until fresh candle arrives"
                )
                return None
        except Exception:
            pass  # TZ check failed — proceed with available data

        # Volume confirmation: current candle must be ≥ 1.2× avg opening range volume
        volume_ok = True
        if cfg.volume_confirmation and self._range_avg_volume > 0:
            volume_ok = volume >= (self._range_avg_volume * 1.2)

        buf           = cfg.breakout_buffer_pct / 100
        long_trigger  = self._range_high * (1 + buf)
        short_trigger = self._range_low  * (1 - buf)

        long_break  = price > long_trigger
        short_break = price < short_trigger

        # ── Diagnostic scoreboard (logged every cycle while READY) ──────────
        _range_pct = self._range_width / self._range_high * 100 if self._range_high > 0 else 0
        _vol_tag   = "vol✓" if volume_ok else "vol✗"
        _now_str   = now.strftime("%H:%M:%S")
        if not long_break and not short_break:
            self._breakout_count = 0   # price retreated — reset counter
            logger.info(
                f"ORB [{self._index.display_name}] {_now_str} → FAIL "
                f"| range={self._range_low:.0f}–{self._range_high:.0f} "
                f"({self._range_width:.0f}pts, {_range_pct:.2f}%) "
                f"| price={price:.0f} "
                f"| need >{long_trigger:.0f} or <{short_trigger:.0f} "
                f"| {_vol_tag}"
            )
            return None

        # Require 2 consecutive closes beyond trigger before firing
        self._breakout_count += 1
        direction_hint = "LONG" if long_break else "SHORT"
        if self._breakout_count < 2:
            logger.info(
                f"ORB [{self._index.display_name}] {_now_str} → WAIT (candle {self._breakout_count}/2) "
                f"| {direction_hint} breakout @ {price:.0f} vs trigger {long_trigger if long_break else short_trigger:.0f} "
                f"| range {self._range_width:.0f}pts ({_range_pct:.2f}%) | {_vol_tag}"
            )
            return None

        direction = "LONG" if long_break else "SHORT"
        rw        = self._range_width

        if direction == "LONG":
            stop_loss = self._range_low
            target_1  = price + rw * cfg.target_multiplier
            target_2  = price + rw * cfg.target_multiplier_2
            gap_pct   = (price - self._range_high) / self._range_high * 100
        else:
            stop_loss = self._range_high
            target_1  = price - rw * cfg.target_multiplier
            target_2  = price - rw * cfg.target_multiplier_2
            gap_pct   = (self._range_low - price) / self._range_low * 100

        # Confidence score: 60 base + 20 volume + up to 25 for decisive gap.
        # Gap bonus uses ×8 multiplier so a 1.5% gap (NIFTY ~330 pts) = 12 pts
        # and a 2.5% gap (NIFTY ~550 pts / an 800-pt crash day) = 20 pts.
        # This ensures genuine large breakouts qualify WITHOUT unreliable volume.
        strength = 60.0
        if volume_ok:
            strength += 20.0
        strength += min(gap_pct * 8, 25.0)
        strength = min(strength, 100.0)

        # Time decay: ORB breakouts after 11:00 AM are progressively weaker.
        # Each 6-minute slot past 11:00 costs 2 strength points (10 pts per hour).
        # A valid 9:35 AM breakout at 90% stays 90%; a 11:00 AM breakout loses up
        # to 20 pts. This naturally pushes late entries below the 75% entry threshold.
        if now.hour >= 11:
            _late_mins = (now.hour - 11) * 60 + now.minute
            _late_penalty = min(20, (_late_mins // 6) * 2)
            strength = max(60.0, strength - _late_penalty)
            if _late_penalty > 0:
                logger.debug(
                    f"ORB: Late-entry penalty −{_late_penalty:.0f} pts "
                    f"→ adjusted strength={strength:.0f}%  "
                    f"({_late_mins} min past 11:00)"
                )

        self._state = ORBState.TRIGGERED

        signal = ORBSignal(
            direction=direction,
            breakout_price=price,
            range_high=self._range_high,
            range_low=self._range_low,
            range_width=rw,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            strength=strength,
            volume_confirmed=volume_ok,
            timestamp=now,
        )

        _range_pct = rw / self._range_high * 100 if self._range_high > 0 else 0
        logger.info(
            f"ORB [{self._index.display_name}] {now.strftime('%H:%M:%S')} → PASS\u2713 "
            f"| range={self._range_low:.0f}–{self._range_high:.0f} "
            f"({rw:.0f}pts, {_range_pct:.2f}%) "
            f"| {direction} breakout @ {price:.0f} "
            f"| strength={strength:.0f}% "
            f"| vol={'✓' if volume_ok else '✗'}"
        )
        return signal
