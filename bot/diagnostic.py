"""
bot/diagnostic.py — Deep diagnostic script for the trading bot.

Tests every precondition and filter for each strategy independently,
simulates synthetic signals to isolate which filter blocks them, and
produces a prioritised recommendations report.

Usage:
    python bot/diagnostic.py
    python -m bot.diagnostic
    python bot/diagnostic.py --index BANKNIFTY   # single index

Output sections:
    [1]  System & config check
    [2]  Market data health
    [3]  Market regime
    [4]  Trend analysis (per index)
    [5]  ORB — preconditions + filter walkthrough
    [6]  VWAP — preconditions + filter walkthrough
    [7]  Order-manager validation chain
    [8]  Signal injection — synthetic signal vs all filters
    [9]  Paper-mode audit
    [10] Recommendations
"""

from __future__ import annotations

import argparse
import os
import sys

# Force UTF-8 output on Windows consoles that default to cp1252
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yfinance as yf
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# Silence internal strategy loggers so diagnostic output stays readable
from loguru import logger
logger.disable("bot.trend_analyzer")
logger.disable("bot.orb_strategy")
logger.disable("bot.vwap_strategy")
logger.disable("bot.market_regime")
logger.disable("bot.multi_timeframe")

from config import settings
from bot.index_config import IndexConfig, NIFTY, BANKNIFTY, SENSEX

IST = ZoneInfo("Asia/Kolkata")
W = 68  # column width


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hdr(title: str) -> str:
    return f"\n{'─' * W}\n  {title}\n{'─' * W}"


def _now() -> datetime:
    return datetime.now(IST)


def _pass(label: str, detail: str = "") -> str:
    return f"  ✅  {label}" + (f"  [{detail}]" if detail else "")


def _fail(label: str, detail: str = "") -> str:
    return f"  ❌  {label}" + (f"  [{detail}]" if detail else "")


def _warn(label: str, detail: str = "") -> str:
    return f"  ⚠️   {label}" + (f"  [{detail}]" if detail else "")


def _info(label: str, detail: str = "") -> str:
    return f"  ℹ️   {label}" + (f"  [{detail}]" if detail else "")


def _skip(label: str, detail: str = "") -> str:
    return f"  ⏭️   {label}" + (f"  [{detail}]" if detail else "")


@dataclass
class FilterResult:
    name: str
    passed: bool
    reason: str
    value: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_5m(idx: IndexConfig) -> pd.DataFrame:
    try:
        df = yf.Ticker(idx.yahoo_symbol).history(period="1d", interval="5m")
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        print(_fail(f"yfinance fetch failed for {idx.yahoo_symbol}", str(e)))
        return pd.DataFrame()


def _fetch_daily(idx: IndexConfig, period: str = "60d") -> pd.DataFrame:
    try:
        df = yf.Ticker(idx.yahoo_symbol).history(period=period, interval="1d")
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _fetch_vix() -> Optional[float]:
    try:
        df = yf.Ticker("^INDIAVIX").history(period="1d", interval="1d")
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
        v = yf.Ticker("^INDIAVIX").fast_info
        val = float(v.get("lastPrice") or 0)
        return val if val > 0 else None
    except Exception:
        return None


def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    v = rsi.iloc[-1]
    return float(v) if not pd.isna(v) else 50.0


def _calc_vwap(df: pd.DataFrame) -> float:
    today_date = _now().date()
    try:
        if hasattr(df.index, "tz_convert"):
            local = df.index.tz_convert(IST)
        else:
            local = df.index
        mask = [ts.date() == today_date for ts in local]
        today = df[mask]
    except Exception:
        today = df
    if today.empty:
        today = df
    tp = (today["High"] + today["Low"] + today["Close"]) / 3
    if "Volume" in today.columns and today["Volume"].sum() > 0:
        return float((tp * today["Volume"]).sum() / today["Volume"].sum())
    return float(tp.mean())


def _orb_range(df: pd.DataFrame, idx: IndexConfig) -> tuple[float, float, int, float]:
    """Returns (low, high, n_candles, avg_volume)."""
    window_min = settings.orb.window_minutes
    today_date = _now().date()
    market_open = datetime(today_date.year, today_date.month, today_date.day, 9, 15, tzinfo=IST)
    range_end = market_open + timedelta(minutes=window_min)
    try:
        if hasattr(df.index, "tz_convert"):
            local_idx = df.index.tz_convert(IST)
        else:
            local_idx = df.index
        mask = [(market_open <= ts < range_end) for ts in local_idx]
        candles = df[mask]
    except Exception:
        candles = df.head(window_min // 5)
    if len(candles) < 1:
        return 0.0, 0.0, 0, 0.0
    avg_vol = float(candles["Volume"].mean()) if "Volume" in candles.columns else 0.0
    return float(candles["Low"].min()), float(candles["High"].max()), len(candles), avg_vol


def _latest_candle_age_min(df: pd.DataFrame) -> float:
    """Minutes since last candle close (IST-aware)."""
    try:
        last = df.index[-1]
        if hasattr(last, "tz_convert"):
            last = last.tz_convert(IST)
        return (_now() - last).total_seconds() / 60
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Section 1: System & config
# ─────────────────────────────────────────────────────────────────────────────

def _sec1_system() -> None:
    print(_hdr("[1] SYSTEM & CONFIGURATION"))
    t = settings.trading
    o = settings.orb
    v = settings.vwap

    auto = getattr(t, "auto_trade_enabled", False)
    mode = "LIVE (real orders)" if auto else "PAPER (no real orders)"

    print(_info(f"Mode            : {mode}"))
    print(_info(f"Analysis interval: {settings.trend.analysis_interval_seconds}s  "
                f"(bot ticks every {settings.trend.analysis_interval_seconds//60} min)"))

    print()
    print("  ORB settings:")
    range_end_min = 9 * 60 + 15 + o.window_minutes
    print(f"    ORB_ENABLED               = {o.enabled}")
    print(f"    ORB_WINDOW_MINUTES         = {o.window_minutes}  "
          f"(range builds 09:15 → {range_end_min//60:02d}:{range_end_min%60:02d} IST)")
    print(f"    ORB_ENTRY_END              = {o.entry_end_hour:02d}:{o.entry_end_minute:02d} IST")
    print(f"    ORB_BREAKOUT_BUFFER_PCT    = {o.breakout_buffer_pct}%")
    print(f"    ORB_VOLUME_CONFIRMATION    = {o.volume_confirmation}")
    print(f"    Engine strength threshold  = 65% min (+ ≥65% required on NEUTRAL days)")

    print()
    print("  VWAP settings:")
    print(f"    VWAP_ENABLED               = {v.enabled}")
    print(f"    VWAP_DEVIATION_PCT          = {v.deviation_pct}%")
    print(f"    VWAP_RSI_OVERSOLD           = {v.rsi_oversold}  (LONG gate)")
    print(f"    VWAP_RSI_OVERBOUGHT         = {v.rsi_overbought}  (SHORT gate)")
    print(f"    Engine strength threshold  = 60% min")
    print(f"    Entry window               = 09:30 → 14:30 IST")

    print()
    print("  Risk / order gates:")
    print(f"    Max positions              = {t.max_positions}")
    print(f"    Max trades/day             = {t.max_trades_per_day}")
    print(f"    Min gap between trades     = {t.min_time_between_trades_minutes} min")
    print(f"    Max daily loss             = ₹{t.max_daily_loss:,.0f}")
    print(f"    Max consecutive losses     = {t.max_consecutive_losses} → pause {t.pause_after_losses_minutes} min")

    # Warn about common footguns
    if not auto:
        print()
        print(_warn("PAPER MODE: Analysis loop does NOT auto-start at boot.",
                    "click Start in UI or POST /api/start"))
    if not o.enabled:
        print(_fail("ORB_ENABLED=false — ORB will never fire"))
    if not v.enabled:
        print(_fail("VWAP_ENABLED=false — VWAP will never fire"))


# ─────────────────────────────────────────────────────────────────────────────
# Section 2: Market data health
# ─────────────────────────────────────────────────────────────────────────────

def _sec2_data(indices: list[IndexConfig]) -> tuple[dict, Optional[float]]:
    """Returns (data_map: {name: DataFrame|None}, vix: float|None)."""
    print(_hdr("[2] MARKET DATA HEALTH"))
    data_map: dict = {}

    for idx in indices:
        df = _fetch_5m(idx)
        if df is None or df.empty:
            print(_fail(f"{idx.name:<12}", f"yfinance empty — {idx.yahoo_symbol}"))
            data_map[idx.name] = None
            continue
        age = _latest_candle_age_min(df)
        price = float(df["Close"].iloc[-1])
        stale_tag = "  ⚠️ STALE" if age > 20 else ""
        mark = "✅" if age <= 20 else "⚠️"
        try:
            last_ts = df.index[-1]
            if hasattr(last_ts, "tz_convert"):
                last_ts = last_ts.tz_convert(IST)
            ts_str = last_ts.strftime("%H:%M")
        except Exception:
            ts_str = "?"
        print(f"  {mark}  {idx.name:<12}  {len(df):>3} candles  "
              f"last={ts_str} IST  age={age:.0f}min  price={price:,.0f}{stale_tag}")
        data_map[idx.name] = df

    vix = _fetch_vix()
    if vix is not None:
        vix_max = getattr(settings.trading, "vix_max", 20.0)
        mark = "✅" if vix <= vix_max else "❌"
        print(f"  {mark}  India VIX             current={vix:.2f}  limit={vix_max}")
    else:
        print(_warn("India VIX               fetch failed — VIX filter will be BYPASSED"))

    return data_map, vix


# ─────────────────────────────────────────────────────────────────────────────
# Section 3: Market regime
# ─────────────────────────────────────────────────────────────────────────────

def _sec3_regime(indices: list[IndexConfig]) -> dict:
    """Returns {name: regime_result|None}."""
    print(_hdr("[3] MARKET REGIME  (3-month ADX + Hurst)"))
    regimes: dict = {}
    try:
        from bot.market_regime import MarketRegimeDetector
    except ImportError as e:
        print(_fail("Cannot import MarketRegimeDetector", str(e)))
        return regimes

    det = MarketRegimeDetector()
    for idx in indices:
        det.set_index(idx)
        r = det.analyze()
        regimes[idx.name] = r
        if r is None:
            print(_warn(f"{idx.name:<12}  N/A — insufficient history (need 60+ daily candles)"))
        else:
            trade = "trade=YES" if r.should_trade else "trade=NO  ← engine skips non-ranging auto-entries"
            mark = "✅" if r.should_trade else "⚠️"
            print(f"  {mark}  {idx.name:<12}  {r.regime.value:<18}  "
                  f"ADX={r.adx:.1f}  Hurst={r.hurst:.3f}  {trade}")
            if r.regime.value in ("TRENDING UP", "TRENDING DOWN") and r.adx < 25:
                print(_warn(f"    ADX {r.adx:.1f} < 25 — trend label may be unreliable (ADX lagging indicator)"))

    return regimes


# ─────────────────────────────────────────────────────────────────────────────
# Section 4: Trend analysis
# ─────────────────────────────────────────────────────────────────────────────

def _sec4_trend(indices: list[IndexConfig]) -> dict:
    """Returns {name: TrendSignal|None}."""
    print(_hdr("[4] TREND ANALYSIS  (5-day 5m window, adaptive threshold)"))
    try:
        from bot.trend_analyzer import TrendAnalyzer, Trend
    except ImportError as e:
        print(_fail("Cannot import TrendAnalyzer", str(e)))
        return {}

    signals: dict = {}
    for idx in indices:
        ta = TrendAnalyzer(index_config=idx)
        sig = ta.analyze()
        signals[idx.name] = sig
        if sig is None:
            print(_fail(f"{idx.name:<12}  analyze() returned None — yfinance fetch failed?"))
            continue

        try:
            threshold = ta.get_dynamic_confidence_threshold(sig.atr, sig.current_price)
        except Exception:
            threshold = 65

        ok = sig.strength >= threshold
        mark = "✅" if ok else "❌"
        issue = "" if ok else f"  ← BELOW THRESHOLD by {threshold - sig.strength:.1f}pts — auto-trade BLOCKED"
        print(f"  {mark}  {idx.name:<12}  {sig.trend.value:<8}  "
              f"strength={sig.strength:>3}%  threshold={threshold}%  "
              f"rsi={sig.rsi:.1f}  price={sig.current_price:,.0f}{issue}")
        if sig.trend.value == "NEUTRAL":
            print(_info(f"    NEUTRAL: ORB/VWAP can still fire independently — "
                        f"but engine won't call _execute_auto_trade"))
        if not ok and sig.trend.value != "NEUTRAL":
            print(_info(f"    Possible cause: recent large candle in the 5-day window inflating noise."))
            print(_info(f"    Self-corrects naturally when that candle rolls out of the window."))

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Section 5: ORB — preconditions + per-filter walkthrough
# ─────────────────────────────────────────────────────────────────────────────

def _sec5_orb(indices: list[IndexConfig], data_map: dict, signals: dict, regimes: dict) -> dict:
    """
    For each index: run every ORB filter in sequence.
    Returns {name: list[FilterResult]}.
    """
    print(_hdr("[5] ORB — PRECONDITIONS & FILTER WALKTHROUGH"))
    o = settings.orb
    now = _now()
    now_t = now.time()
    results: dict = {}

    range_end_h = (9 * 60 + 15 + o.window_minutes) // 60
    range_end_m = (9 * 60 + 15 + o.window_minutes) % 60
    range_end_t = time(range_end_h, range_end_m)
    entry_end_t = time(o.entry_end_hour, o.entry_end_minute)

    for idx in indices:
        print(f"\n  ── {idx.name} ──")
        filters: list[FilterResult] = []
        df = data_map.get(idx.name)
        sig = signals.get(idx.name)

        # ── F-ORB-0: ORB enabled ──────────────────────────────────────────────
        f = FilterResult("enabled", o.enabled,
                         "ORB_ENABLED=true" if o.enabled else "ORB_ENABLED=false → ALL signals blocked")
        filters.append(f)
        print((_pass if f.passed else _fail)(f"F-ORB-0  enabled          = {o.enabled}"))
        if not f.passed:
            results[idx.name] = filters
            continue

        # ── F-ORB-1: market hours check ───────────────────────────────────────
        in_hours = (now.weekday() <= 4
                    and time(9, 15) <= now_t <= time(15, 15))
        f = FilterResult("market_hours", in_hours,
                         f"now={now_t.strftime('%H:%M')} IST  valid=09:15–15:15")
        filters.append(f)
        print((_pass if in_hours else _warn)(
            f"F-ORB-1  market hours      now={now_t.strftime('%H:%M')}  "
            f"{'in window' if in_hours else 'OUTSIDE 09:15–15:15 → analyze() returns None silently'}"))

        # ── F-ORB-2: entry window ────────────────────────────────────────────
        in_entry = now_t < entry_end_t
        f = FilterResult("entry_window", in_entry,
                         f"entry window closes {entry_end_t.strftime('%H:%M')} IST")
        filters.append(f)
        print((_pass if in_entry else _fail)(
            f"F-ORB-2  entry window      closes {entry_end_t.strftime('%H:%M')}  "
            f"{'OK' if in_entry else 'EXPIRED — no new signals'}"))

        # ── F-ORB-3: range building complete ─────────────────────────────────
        r_low = r_high = 0.0
        r_candles = 0
        r_avg_vol = 0.0
        if df is not None and not df.empty:
            r_low, r_high, r_candles, r_avg_vol = _orb_range(df, idx)
        range_built = (r_high > 0 and r_candles >= 2)
        still_building = in_hours and now_t < range_end_t

        if still_building:
            f = FilterResult("range_building", True,
                             f"BUILDING — {r_candles} candles so far, locks at {range_end_t.strftime('%H:%M')}")
            filters.append(f)
            print(_info(f"F-ORB-3  range_building    BUILDING — "
                        f"{r_candles} candles so far  locks at {range_end_t.strftime('%H:%M')}  "
                        f"(current: {r_low:.0f}–{r_high:.0f})"))
        elif range_built:
            f = FilterResult("range_built", True,
                             f"{r_low:.0f}–{r_high:.0f}  width={r_high-r_low:.0f}pts")
            filters.append(f)
            print(_pass(f"F-ORB-3  range_built       {r_low:.0f}–{r_high:.0f}  "
                        f"width={r_high-r_low:.0f}pts  "
                        f"({r_candles} candles)"))
        else:
            f = FilterResult("range_built", False,
                             "range is 0 — yfinance returned no opening candles yet")
            filters.append(f)
            print(_fail(f"F-ORB-3  range_built       NO RANGE — "
                        f"{r_candles} opening candles (need ≥2). yfinance lag?"))

        # ── F-ORB-4: minimum range width ─────────────────────────────────────
        min_range_pts = idx.strike_interval * 1.5
        range_width = r_high - r_low
        range_wide = range_built and range_width >= min_range_pts
        f = FilterResult("range_width", range_wide,
                         f"{range_width:.0f}pts vs {min_range_pts:.0f}pts minimum")
        filters.append(f)
        if range_built:
            print((_pass if range_wide else _fail)(
                f"F-ORB-4  range_width       {range_width:.0f}pts  "
                f"need ≥{min_range_pts:.0f}pts (1.5× strike interval {idx.strike_interval})  "
                f"{'OK' if range_wide else 'TOO NARROW — no breakout will fire'}"))

        # ── F-ORB-5: stale data guard ─────────────────────────────────────────
        data_age = _latest_candle_age_min(df) if (df is not None and not df.empty) else 999.0
        data_fresh = data_age <= 25
        f = FilterResult("data_fresh", data_fresh,
                         f"latest candle {data_age:.0f}min old (limit 25min)")
        filters.append(f)
        print((_pass if data_fresh else _fail)(
            f"F-ORB-5  data_freshness    last candle {data_age:.0f}min ago  "
            f"{'OK' if data_fresh else 'STALE >25min → breakout check skipped'}"))

        # ── F-ORB-6: breakout price check (simulation) ───────────────────────
        if range_built and range_wide:
            price = float(df["Close"].iloc[-1]) if (df is not None and not df.empty) else 0.0
            buf = o.breakout_buffer_pct / 100
            long_trig = r_high * (1 + buf)
            short_trig = r_low * (1 - buf)
            long_break = price > long_trig
            short_break = price < short_trig
            any_break = long_break or short_break
            direction = "LONG" if long_break else ("SHORT" if short_break else "—")
            f = FilterResult("price_breakout", any_break,
                             f"price={price:.0f}  long>{long_trig:.0f}  short<{short_trig:.0f}")
            filters.append(f)
            print((_pass if any_break else _fail)(
                f"F-ORB-6  price_breakout   price={price:.0f}  "
                f"need >{long_trig:.0f} LONG  or <{short_trig:.0f} SHORT  "
                f"{'→ ' + direction if any_break else '→ INSIDE RANGE'}"))
        else:
            direction = None
            any_break = False

        # ── F-ORB-7: strength threshold ───────────────────────────────────────
        # Simulate the strength that would be scored given current conditions
        if range_built and range_wide and any_break and df is not None:
            price = float(df["Close"].iloc[-1])
            buf = o.breakout_buffer_pct / 100
            if direction == "LONG":
                gap_pct = (price - r_high) / r_high * 100
            else:
                gap_pct = (r_low - price) / r_low * 100

            # Volume: last candle vs avg range volume
            last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
            volume_ok = (o.volume_confirmation and r_avg_vol > 0 and last_vol >= r_avg_vol * 1.2)
            strength = 60.0 + (20.0 if volume_ok else 0.0) + min(gap_pct * 8, 25.0)

            # Time decay
            if now.hour >= 11:
                late_mins = (now.hour - 11) * 60 + now.minute
                penalty = min(20, (late_mins // 6) * 2)
                strength = max(60.0, strength - penalty)
            strength = min(strength, 100.0)

            strength_ok = strength >= 65
            f = FilterResult("strength", strength_ok,
                             f"score={strength:.0f}% threshold=65%")
            filters.append(f)
            print((_pass if strength_ok else _fail)(
                f"F-ORB-7  strength          {strength:.0f}%  threshold=65%  "
                f"vol={'✓' if volume_ok else f'✗(last={last_vol:.0f} avg={r_avg_vol:.0f})'}  "
                f"gap_bonus={min(gap_pct*8, 25.0):.1f}pts  "
                f"{'OK' if strength_ok else 'BELOW THRESHOLD'}"))
        else:
            strength_ok = True  # can't evaluate yet

        # ── F-ORB-8: trend direction (counter-trend filter) ───────────────────
        if sig and any_break and direction:
            trend_val = sig.trend.value
            counter = (direction == "LONG" and trend_val == "BEARISH") or \
                      (direction == "SHORT" and trend_val == "BULLISH")
            f = FilterResult("trend_direction", not counter,
                             f"ORB={direction}  trend={trend_val}")
            filters.append(f)
            print((_pass if not counter else _fail)(
                f"F-ORB-8  trend_direction   ORB={direction}  5m_trend={trend_val}  "
                f"{'OK — aligned' if not counter else 'COUNTER-TREND BLOCKED'}"))
        elif sig:
            print(_info(f"F-ORB-8  trend_direction   5m_trend={sig.trend.value}  "
                        f"(no breakout to evaluate direction against)"))
        else:
            print(_warn("F-ORB-8  trend_direction   no TrendSignal — filter will be skipped by engine"))

        # ── F-ORB-9: RSI quality filter ───────────────────────────────────────
        if sig and df is not None and not df.empty:
            rsi = _calc_rsi(df["Close"])
            ce_rsi_ok = not (direction == "LONG" and rsi >= 55)
            pe_rsi_ok = not (direction == "SHORT" and rsi <= 45)
            rsi_ok = ce_rsi_ok and pe_rsi_ok
            rsi_reason = ""
            if not ce_rsi_ok:
                rsi_reason = f"RSI={rsi:.1f} ≥55 → CE extended"
            elif not pe_rsi_ok:
                rsi_reason = f"RSI={rsi:.1f} ≤45 → PE oversold"
            else:
                rsi_reason = f"RSI={rsi:.1f} OK"
            f = FilterResult("rsi_quality", rsi_ok, rsi_reason)
            filters.append(f)
            print((_pass if rsi_ok else _fail)(
                f"F-ORB-9  rsi_quality       RSI={rsi:.1f}  "
                f"CE_gate=<55  PE_gate=>45  {rsi_reason}"))
        else:
            print(_skip("F-ORB-9  rsi_quality       (no RSI computable)"))

        # ── F-ORB-10: 15m MTF confluence ─────────────────────────────────────
        try:
            from bot.multi_timeframe import MultiTimeframeEngine
            mtf = MultiTimeframeEngine()
            mtf.set_index(idx)
            mtf_result = mtf.analyze()
            if mtf_result and mtf_result.signals.get("15m"):
                _15m = mtf_result.signals["15m"]
                mtf_ok = not (
                    (direction == "LONG" and _15m.trend == "BEARISH")
                    or (direction == "SHORT" and _15m.trend == "BULLISH")
                )
                f = FilterResult("mtf_15m", mtf_ok,
                                 f"ORB={direction}  15m={_15m.trend}")
                filters.append(f)
                print((_pass if mtf_ok else _fail)(
                    f"F-ORB-10 mtf_15m           ORB={direction}  15m={_15m.trend}  "
                    f"{'OK — aligned' if mtf_ok else 'NO MTF CONFLUENCE — BLOCKED'}"))
            else:
                print(_info("F-ORB-10 mtf_15m           15m signal unavailable — MTF guard skipped"))
        except Exception as e:
            print(_warn(f"F-ORB-10 mtf_15m           MTF check failed ({e}) — guard will be skipped"))

        results[idx.name] = filters

        # Summary
        blocked = [f for f in filters if not f.passed]
        if blocked:
            print(f"\n    ⛔  {idx.name} ORB blocked at: {', '.join(f.name for f in blocked)}")
        else:
            print(f"\n    ✅  {idx.name} ORB — all current filters PASS")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Section 6: VWAP — preconditions + per-filter walkthrough
# ─────────────────────────────────────────────────────────────────────────────

def _sec6_vwap(indices: list[IndexConfig], data_map: dict, signals: dict, regimes: dict) -> dict:
    """Returns {name: list[FilterResult]}."""
    print(_hdr("[6] VWAP — PRECONDITIONS & FILTER WALKTHROUGH"))
    v = settings.vwap
    now = _now()
    now_t = now.time()
    results: dict = {}

    for idx in indices:
        print(f"\n  ── {idx.name} ──")
        filters: list[FilterResult] = []
        df = data_map.get(idx.name)
        sig = signals.get(idx.name)
        regime = regimes.get(idx.name)

        # ── F-VWAP-0: enabled ────────────────────────────────────────────────
        f = FilterResult("enabled", v.enabled,
                         "VWAP_ENABLED=true" if v.enabled else "VWAP_ENABLED=false → blocked")
        filters.append(f)
        print((_pass if f.passed else _fail)(f"F-VWAP-0  enabled          = {v.enabled}"))
        if not f.passed:
            results[idx.name] = filters
            continue

        # ── F-VWAP-1: time window ─────────────────────────────────────────────
        in_window = time(9, 30) <= now_t < time(14, 30)
        f = FilterResult("time_window", in_window,
                         f"now={now_t.strftime('%H:%M')}  valid=09:30–14:30")
        filters.append(f)
        print((_pass if in_window else _warn)(
            f"F-VWAP-1  time_window       now={now_t.strftime('%H:%M')}  "
            f"{'in window' if in_window else 'OUTSIDE 09:30–14:30 → analyze() returns None'}"))

        # ── F-VWAP-2: compute VWAP dev + RSI ─────────────────────────────────
        price = vwap = dev = rsi = 0.0
        if df is not None and not df.empty:
            price = float(df["Close"].iloc[-1])
            vwap = _calc_vwap(df)
            dev = (price - vwap) / vwap * 100 if vwap > 0 else 0.0
            rsi = _calc_rsi(df["Close"])
        else:
            print(_fail("F-VWAP-2  data              no 5m data available"))
            results[idx.name] = filters
            continue

        long_dev = dev <= -v.deviation_pct
        long_rsi = rsi < v.rsi_oversold
        short_dev = dev >= v.deviation_pct
        short_rsi = rsi > v.rsi_overbought
        long_ok = long_dev and long_rsi
        short_ok = short_dev and short_rsi
        direction = "LONG" if long_ok else ("SHORT" if short_ok else None)

        print(_info(f"F-VWAP-2  indicators       price={price:.0f}  vwap={vwap:.0f}  "
                    f"dev={dev:+.2f}%  rsi={rsi:.1f}"))
        l_dev_s = "✓" if long_dev else f"✗({dev:+.2f}% need ≤-{v.deviation_pct}%)"
        l_rsi_s = "✓" if long_rsi else f"✗(rsi={rsi:.0f} need <{v.rsi_oversold})"
        s_dev_s = "✓" if short_dev else f"✗({dev:+.2f}% need ≥+{v.deviation_pct}%)"
        s_rsi_s = "✓" if short_rsi else f"✗(rsi={rsi:.0f} need >{v.rsi_overbought})"
        print(f"             LONG:  dev {l_dev_s:<40}  rsi {l_rsi_s}")
        print(f"             SHORT: dev {s_dev_s:<40}  rsi {s_rsi_s}")

        raw_signal = FilterResult("raw_signal", bool(direction),
                                  f"direction={direction or 'NONE'}")
        filters.append(raw_signal)
        if direction:
            print(_pass(f"F-VWAP-2  raw_signal        {direction} setup present"))
        else:
            print(_fail("F-VWAP-2  raw_signal        no LONG or SHORT setup — "
                        "price not sufficiently far from VWAP or RSI not extreme enough"))

        if not direction:
            results[idx.name] = filters
            continue

        # ── F-VWAP-3: strength (min 60%) ──────────────────────────────────────
        # Replicate the ORBStrategy._score() logic from vwap_strategy.py
        avg_vol = float(df["Volume"].iloc[-20:].mean()) if "Volume" in df.columns else 0.0
        last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
        vol_ok = avg_vol > 0 and last_vol >= avg_vol * 1.15
        base = 50.0
        dev_abs = abs(dev)
        dev_score = min((dev_abs - v.deviation_pct) / v.deviation_pct * 25, 25)
        if direction == "LONG":
            rsi_extreme = max(0.0, (v.rsi_oversold - rsi) / v.rsi_oversold * 25)
        else:
            rsi_extreme = max(0.0, (rsi - v.rsi_overbought) / (100 - v.rsi_overbought) * 25)
        vol_score = 20.0 if vol_ok else 0.0
        strength = base + dev_score + rsi_extreme + vol_score
        strength = min(strength, 100.0)
        strength_ok = strength >= 60
        f = FilterResult("strength", strength_ok,
                         f"{strength:.0f}% vs 60% threshold")
        filters.append(f)
        print((_pass if strength_ok else _fail)(
            f"F-VWAP-3  strength          {strength:.0f}%  threshold=60%  "
            f"(base=50 dev+{dev_score:.0f} rsi+{rsi_extreme:.0f} vol+{vol_score:.0f})  "
            f"{'OK' if strength_ok else 'BELOW 60% — signal blocked'}"))

        # ── F-VWAP-4: regime direction guard ─────────────────────────────────
        if regime:
            regime_val = regime.regime.value
            reg_blocked = (
                (regime_val == "TRENDING DOWN" and direction == "LONG") or
                (regime_val == "TRENDING UP" and direction == "SHORT")
            )
            f = FilterResult("regime_direction", not reg_blocked,
                             f"regime={regime_val}  direction={direction}")
            filters.append(f)
            print((_pass if not reg_blocked else _fail)(
                f"F-VWAP-4  regime_direction  "
                f"regime={regime_val}  VWAP={direction}  "
                f"{'OK — aligned' if not reg_blocked else 'COUNTER-REGIME BLOCKED'}"))
        else:
            print(_skip("F-VWAP-4  regime_direction  no regime result — guard skipped"))

        # ── F-VWAP-5: 5m trend direction guard ───────────────────────────────
        if sig:
            trend_val = sig.trend.value
            trend_blocked = (
                (direction == "LONG" and trend_val == "BEARISH") or
                (direction == "SHORT" and trend_val == "BULLISH")
            )
            f = FilterResult("5m_trend", not trend_blocked,
                             f"5m_trend={trend_val}  direction={direction}")
            filters.append(f)
            print((_pass if not trend_blocked else _fail)(
                f"F-VWAP-5  5m_trend          5m={trend_val}  VWAP={direction}  "
                f"{'OK' if not trend_blocked else 'COUNTER-TREND BLOCKED (crash/rally day guard)'}"))
        else:
            print(_skip("F-VWAP-5  5m_trend          no TrendSignal — guard skipped"))

        results[idx.name] = filters

        blocked = [f for f in filters if not f.passed]
        if blocked:
            print(f"\n    ⛔  {idx.name} VWAP blocked at: {', '.join(f.name for f in blocked)}")
        else:
            print(f"\n    ✅  {idx.name} VWAP — all current filters PASS")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Section 7: Order-manager validation chain
# ─────────────────────────────────────────────────────────────────────────────

def _sec7_order_manager() -> list[FilterResult]:
    print(_hdr("[7] ORDER-MANAGER VALIDATION CHAIN  (can_place_order gates)"))
    t = settings.trading
    filters: list[FilterResult] = []
    now = _now()

    # Gate 1: market hours
    in_hours = (
        now.weekday() <= 4
        and now.replace(hour=t.market_open_hour, minute=t.market_open_minute,
                        second=0, microsecond=0)
        <= now
        <= now.replace(hour=t.market_close_hour, minute=t.market_close_minute,
                       second=0, microsecond=0)
    )
    f = FilterResult("market_hours", in_hours,
                     f"{now.strftime('%H:%M')} IST  valid={t.market_open_hour:02d}:"
                     f"{t.market_open_minute:02d}–{t.market_close_hour:02d}:{t.market_close_minute:02d}")
    filters.append(f)
    print((_pass if in_hours else _warn)(
        f"G1  market_hours      {now.strftime('%H:%M')} IST  "
        f"window={t.market_open_hour:02d}:{t.market_open_minute:02d}–"
        f"{t.market_close_hour:02d}:{t.market_close_minute:02d}  "
        f"{'OK' if in_hours else 'OUTSIDE — order would be blocked (or AMO if enabled)'}"))

    # Gate 2: consecutive-loss cool-off (no live state available in standalone mode)
    print(_info("G2  cool_off          (runtime state — visible in 'can_place_order: GATE[cool_off]' logs)"))

    # Gate 3: daily trade limit
    print(_info(f"G3  daily_limit       max_trades_per_day={t.max_trades_per_day}  "
                f"(runtime count — visible in bot logs)"))

    # Gate 4: min time between trades
    print(_info(f"G4  min_gap           min_time_between_trades={t.min_time_between_trades_minutes}min  "
                f"(skipped when positions>0)"))

    # Gate 5: max positions
    print(_info(f"G5  max_positions     max_positions={t.max_positions}  "
                f"(runtime count — visible in bot logs)"))

    # Gate 6: daily loss cap
    print(_info(f"G6  daily_loss_cap    max_daily_loss=₹{t.max_daily_loss:,.0f}  "
                f"(persisted in .daily_pnl.json)"))

    # Pre-trade risk gate
    print(_info(f"G7  pre_trade_risk    forward-projects worst-case SL loss before order  "
                f"sl={t.stop_loss_percentage}%  "
                f"(blocks if projected_pnl < -₹{t.max_daily_loss:,.0f})"))

    # VIX gate
    vix_enabled = getattr(t, "vix_filter_enabled", True)
    vix_max = getattr(t, "vix_max", 20.0)
    print(_info(f"G8  vix_filter        enabled={vix_enabled}  max={vix_max}  "
                f"(only applies to Auto/ORB/VWAP/GAP sources — not Manual)"))

    return filters


# ─────────────────────────────────────────────────────────────────────────────
# Section 8: Signal injection — synthetic signal through all filters
# ─────────────────────────────────────────────────────────────────────────────

def _sec8_signal_injection(indices: list[IndexConfig], data_map: dict) -> None:
    """
    Inject a synthetic "ideal" signal for each index and walk it through
    every ORB and VWAP engine filter to show exactly where it would be blocked.
    """
    print(_hdr("[8] SIGNAL INJECTION — synthetic ideal signal vs all filters"))

    for idx in indices:
        df = data_map.get(idx.name)
        if df is None or df.empty:
            print(f"\n  ── {idx.name}  (skipped — no data) ──")
            continue

        price = float(df["Close"].iloc[-1])
        rsi = _calc_rsi(df["Close"])
        vwap = _calc_vwap(df)
        dev = (price - vwap) / vwap * 100 if vwap > 0 else 0.0

        print(f"\n  ── {idx.name}  price={price:,.0f}  rsi={rsi:.1f}  dev={dev:+.2f}%  ──")

        print("  Synthetic ORB signal (LONG, strength=85%, 2-candle confirmed):")
        _simulate_orb_signal(idx, df, "LONG", 85.0, rsi)

        print()
        print("  Synthetic VWAP signal (direction from current deviation):")
        _simulate_vwap_signal(idx, df, dev, rsi)


def _simulate_orb_signal(idx: IndexConfig, df: pd.DataFrame,
                          direction: str, strength: float, rsi: float) -> None:
    """Walk 'direction+strength' ORB signal through engine filter chain."""
    o = settings.orb
    now = _now()
    now_t = now.time()
    entry_end_t = time(o.entry_end_hour, o.entry_end_minute)
    blocks: list[str] = []

    # 1) time window
    if not (time(9, 15) <= now_t <= time(15, 15)):
        blocks.append(f"time_window (now={now_t.strftime('%H:%M')})")
    # 2) entry end
    elif now_t >= entry_end_t:
        blocks.append(f"entry_window_closed ({entry_end_t.strftime('%H:%M')})")
    # 3) strength
    if strength < 65:
        blocks.append(f"strength ({strength:.0f}% < 65%)")
    # 4) trend direction — use live data
    try:
        from bot.trend_analyzer import TrendAnalyzer
        sig = TrendAnalyzer(index_config=idx).analyze()
        if sig:
            trend = sig.trend.value
            if direction == "LONG" and trend == "BEARISH":
                blocks.append(f"trend_direction (ORB=LONG, trend=BEARISH)")
            elif direction == "SHORT" and trend == "BULLISH":
                blocks.append(f"trend_direction (ORB=SHORT, trend=BULLISH)")
            elif trend == "NEUTRAL" and strength < 65:
                blocks.append(f"neutral_bar (NEUTRAL needs ≥65%, got {strength:.0f}%)")
    except Exception:
        pass
    # 5) RSI
    ce_rsi_blocked = direction == "LONG" and rsi >= 55
    pe_rsi_blocked = direction == "SHORT" and rsi <= 45
    if ce_rsi_blocked:
        blocks.append(f"rsi_quality (CE RSI={rsi:.1f}≥55)")
    if pe_rsi_blocked:
        blocks.append(f"rsi_quality (PE RSI={rsi:.1f}≤45)")
    # 6) MTF
    try:
        from bot.multi_timeframe import MultiTimeframeEngine
        mtf = MultiTimeframeEngine()
        mtf.set_index(idx)
        res = mtf.analyze()
        if res and res.signals.get("15m"):
            t15 = res.signals["15m"].trend
            if direction == "LONG" and t15 == "BEARISH":
                blocks.append(f"mtf_15m (CE, 15m=BEARISH)")
            elif direction == "SHORT" and t15 == "BULLISH":
                blocks.append(f"mtf_15m (PE, 15m=BULLISH)")
    except Exception:
        pass

    if blocks:
        for b in blocks:
            print(_fail(f"    ORB simulate ⛔  {b}"))
    else:
        print(_pass(f"    ORB simulate ✅  signal would reach paper_buy / _execute_orb_direct"))


def _simulate_vwap_signal(idx: IndexConfig, df: pd.DataFrame,
                           dev: float, rsi: float) -> None:
    v = settings.vwap
    now = _now()
    now_t = now.time()
    blocks: list[str] = []
    direction = "LONG" if dev < 0 else "SHORT"

    # 1) time window
    if not (time(9, 30) <= now_t < time(14, 30)):
        blocks.append(f"time_window (now={now_t.strftime('%H:%M')})")
    # 2) deviation threshold
    if direction == "LONG" and dev > -v.deviation_pct:
        blocks.append(f"deviation ({dev:+.2f}% need ≤-{v.deviation_pct}%)")
    elif direction == "SHORT" and dev < v.deviation_pct:
        blocks.append(f"deviation ({dev:+.2f}% need ≥+{v.deviation_pct}%)")
    # 3) RSI threshold
    if direction == "LONG" and rsi >= v.rsi_oversold:
        blocks.append(f"rsi_oversold (rsi={rsi:.1f} need <{v.rsi_oversold})")
    elif direction == "SHORT" and rsi <= v.rsi_overbought:
        blocks.append(f"rsi_overbought (rsi={rsi:.1f} need >{v.rsi_overbought})")
    # 4) strength
    avg_vol = float(df["Volume"].iloc[-20:].mean()) if "Volume" in df.columns else 0.0
    last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
    vol_ok = avg_vol > 0 and last_vol >= avg_vol * 1.15
    dev_abs = abs(dev)
    dev_score = min((dev_abs - v.deviation_pct) / v.deviation_pct * 25, 25) if dev_abs > v.deviation_pct else 0
    rsi_extreme = 0.0
    if direction == "LONG":
        rsi_extreme = max(0.0, (v.rsi_oversold - rsi) / v.rsi_oversold * 25)
    else:
        rsi_extreme = max(0.0, (rsi - v.rsi_overbought) / (100 - v.rsi_overbought) * 25)
    strength = 50 + dev_score + rsi_extreme + (20.0 if vol_ok else 0.0)
    if strength < 60:
        blocks.append(f"strength ({strength:.0f}% < 60%)")
    # 5) trend direction
    try:
        from bot.trend_analyzer import TrendAnalyzer
        sig = TrendAnalyzer(index_config=idx).analyze()
        if sig:
            trend = sig.trend.value
            if direction == "LONG" and trend == "BEARISH":
                blocks.append(f"5m_trend (VWAP=LONG, 5m=BEARISH)")
            elif direction == "SHORT" and trend == "BULLISH":
                blocks.append(f"5m_trend (VWAP=SHORT, 5m=BULLISH)")
    except Exception:
        pass

    if blocks:
        for b in blocks:
            print(_fail(f"    VWAP simulate ⛔  {b}"))
    else:
        print(_pass(f"    VWAP simulate ✅  signal would reach paper_buy / _execute_option_with_research"))


# ─────────────────────────────────────────────────────────────────────────────
# Section 9: Paper-mode audit
# ─────────────────────────────────────────────────────────────────────────────

def _sec9_paper_audit() -> None:
    print(_hdr("[9] PAPER-MODE AUDIT"))
    t = settings.trading
    auto = getattr(t, "auto_trade_enabled", False)

    # Startup behaviour
    if not auto:
        print(_warn("Loop startup", "auto_trade_enabled=false means analysis loop does NOT "
                    "auto-start. You must click 'Start' in the UI or POST /api/start."))
    else:
        print(_pass("Loop startup", "auto_trade_enabled=true — loop starts automatically at boot"))

    # Check for paper_trades.json
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    paper_file = os.path.join(base, "paper_trades.json")
    if os.path.exists(paper_file):
        import json
        try:
            with open(paper_file) as f:
                data = json.load(f)
            trades = data if isinstance(data, list) else data.get("trades", [])
            open_pos = [tr for tr in trades if tr.get("status") == "OPEN"]
            closed = [tr for tr in trades if tr.get("status") == "CLOSED"]
            wins = sum(1 for tr in closed if tr.get("pnl", 0) > 0)
            losses = sum(1 for tr in closed if tr.get("pnl", 0) < 0)
            print(_pass(f"paper_trades.json exists",
                        f"{len(open_pos)} open  {len(closed)} closed  "
                        f"(W={wins} L={losses})"))
            if open_pos:
                print(_info("Open paper positions:"))
                for p in open_pos:
                    print(f"      #{p.get('id','')}  {p.get('index_name','')} "
                          f"{p.get('option_type','')} [{p.get('strategy','')}]  "
                          f"entry={p.get('index_entry',0):,.0f}  "
                          f"pnl={p.get('pnl',0):+.1f}%")
            if closed and len(closed) >= 5:
                total_pnl = sum(tr.get("pnl_pct", 0) for tr in closed)
                avg_pnl = total_pnl / len(closed)
                win_rate = wins / len(closed) * 100 if closed else 0
                print(_info(f"Closed trade stats",
                            f"avg_pnl={avg_pnl:+.1f}%  win_rate={win_rate:.0f}%"))
        except Exception as e:
            print(_warn(f"paper_trades.json parse error: {e}"))
    else:
        print(_info("paper_trades.json", "not found — no paper trades recorded yet"))

    # Check deduplication note
    print(_info("Dedup rule",
                "paper_buy() rejects (index, strategy, option_type) duplicates per day — "
                "logged at INFO since last fix"))

    # Check _index_trend_triggered reset
    print(_info("TREND daily guard",
                "_index_trend_triggered resets at midnight — "
                "one TREND paper_buy per index per calendar day"))


# ─────────────────────────────────────────────────────────────────────────────
# Section 10: Recommendations
# ─────────────────────────────────────────────────────────────────────────────

def _sec10_recommendations(
    data_map: dict,
    signals: dict,
    regimes: dict,
    orb_filters: dict,
    vwap_filters: dict,
    vix: Optional[float],
) -> None:
    print(_hdr("[10] RECOMMENDATIONS"))
    t = settings.trading
    o = settings.orb
    v_cfg = settings.vwap
    now = _now()
    now_t = now.time()

    recs: list[tuple[str, str]] = []   # (priority, text)  priority: HIGH / MED / LOW

    # ── config issues ────────────────────────────────────────────────────────
    auto = getattr(t, "auto_trade_enabled", False)
    if not auto:
        recs.append(("HIGH",
                     "Analysis loop does NOT auto-start in paper mode. "
                     "If you restarted the bot today without clicking 'Start', "
                     "zero ticks have run. → POST /api/start or click Start."))

    if not o.enabled:
        recs.append(("HIGH", "ORB_ENABLED=false — ORB strategy will never fire. "
                              "Set ORB_ENABLED=true in .env to re-enable."))

    if not v_cfg.enabled:
        recs.append(("HIGH", "VWAP_ENABLED=false — VWAP will never fire. "
                              "Set VWAP_ENABLED=true in .env."))

    # ── data issues ──────────────────────────────────────────────────────────
    for name, df in data_map.items():
        if df is None or df.empty:
            recs.append(("HIGH", f"yfinance returned NO data for {name}. "
                                  "Check network / yahoo finance availability. "
                                  "Bot will run zero strategy checks for this index."))
        elif _latest_candle_age_min(df) > 20:
            age = _latest_candle_age_min(df)
            recs.append(("MED", f"{name}: latest candle is {age:.0f}min old. "
                                  "yfinance free tier lags 15–20min during market hours. "
                                  "This is expected. Stale data guard (25min) may block ORB."))

    if vix is None:
        recs.append(("MED", "Could not fetch India VIX. "
                             "VIX filter will be bypassed and a warning is now logged. "
                             "Check network."))
    elif getattr(t, "vix_filter_enabled", True) and vix > getattr(t, "vix_max", 20.0):
        recs.append(("HIGH", f"India VIX={vix:.1f} > {getattr(t,'vix_max',20.0)} — "
                              f"all live auto-entries are blocked. "
                              f"Wait for VIX to drop, or raise TRADING_VIX_MAX in .env."))

    # ── trend / regime ────────────────────────────────────────────────────────
    try:
        from bot.trend_analyzer import Trend
        neutral_indices = [n for n, s in signals.items() if s and s.trend == Trend.NEUTRAL]
        if neutral_indices:
            recs.append(("MED",
                         f"Trend is NEUTRAL for {'/'.join(neutral_indices)}. "
                         "ORB and VWAP still run independently, but _execute_auto_trade "
                         "won't fire. Likely caused by a large crash/rally candle in "
                         "the 5-day yfinance window — self-corrects after ~5 trading days."))
    except ImportError:
        pass

    for name, r in regimes.items():
        if r and not r.should_trade:
            recs.append(("MED", f"{name} regime={r.regime.value} — "
                                  "MarketRegime says skip. "
                                  "VWAP/ORB still run (engine checks should_trade only for auto-trades). "
                                  "If this persists, check ADX calculation window."))

    # ── time-of-day ───────────────────────────────────────────────────────────
    if now_t < time(9, 15):
        recs.append(("LOW", "Pre-market: all strategies return None. "
                             "No diagnosable signal activity until 09:15 IST."))
    elif now_t >= time(11, 30):
        recs.append(("LOW", f"ORB entry window closed at 11:30. "
                             "Any ORB breakout detected now is too late. "
                             "VWAP remains active until 14:30."))
    elif now_t >= time(14, 30):
        recs.append(("LOW", "VWAP entry window closed. Only EOD strategy active (14:30–15:00)."))

    # ── per-strategy filter blocks ────────────────────────────────────────────
    for name, filters in orb_filters.items():
        hard_blocks = [f.name for f in filters if not f.passed
                       and f.name not in ("time_window", "market_hours")]
        if hard_blocks:
            recs.append(("MED", f"ORB [{name}] blocked by: {', '.join(hard_blocks)}. "
                                  "Check filter details in section [5]."))

    for name, filters in vwap_filters.items():
        hard_blocks = [f.name for f in filters if not f.passed
                       and f.name not in ("time_window",)]
        if hard_blocks:
            recs.append(("MED", f"VWAP [{name}] blocked by: {', '.join(hard_blocks)}. "
                                  "Check filter details in section [6]."))

    # ── logging visibility ────────────────────────────────────────────────────
    recs.append(("LOW",
                 "Most ORB/VWAP block reasons log at INFO. "
                 "If you see NO strategy output in logs during market hours, "
                 "the loop may not be running. "
                 "Check: 'HEARTBEAT tick=N' line — should appear every ~10 min."))

    # ── render ───────────────────────────────────────────────────────────────
    high = [r for p, r in recs if p == "HIGH"]
    med  = [r for p, r in recs if p == "MED"]
    low  = [r for p, r in recs if p == "LOW"]

    if high:
        print("\n  🔴  HIGH PRIORITY")
        for i, r in enumerate(high, 1):
            print(f"    {i}. {r}")
    if med:
        print("\n  🟡  MEDIUM")
        for i, r in enumerate(med, 1):
            print(f"    {i}. {r}")
    if low:
        print("\n  🟢  LOW / INFORMATIONAL")
        for i, r in enumerate(low, 1):
            print(f"    {i}. {r}")

    if not recs:
        print("  All systems green — no recommendations. Monitor live logs for tick-level traces.")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python bot/diagnostic.py",
        description="Deep diagnostic for the trading bot — read-only, no trades placed.",
    )
    parser.add_argument(
        "--index", choices=["NIFTY", "BANKNIFTY", "SENSEX", "ALL"],
        default="ALL", help="Limit diagnostic to one index (default: ALL)",
    )
    args = parser.parse_args()

    index_map = {"NIFTY": NIFTY, "BANKNIFTY": BANKNIFTY, "SENSEX": SENSEX}
    if args.index == "ALL":
        indices = [NIFTY, BANKNIFTY, SENSEX]
    else:
        indices = [index_map[args.index]]

    now = datetime.now(IST)
    print(f"\n{'═' * W}")
    print(f"  TRADING BOT DEEP DIAGNOSTIC")
    print(f"  {now.strftime('%a %d %b %Y  %H:%M:%S IST')}  "
          f"  indices={[i.name for i in indices]}")
    print(f"{'═' * W}")

    _sec1_system()
    data_map, vix = _sec2_data(indices)
    regimes = _sec3_regime(indices)
    signals = _sec4_trend(indices)
    orb_filters = _sec5_orb(indices, data_map, signals, regimes)
    vwap_filters = _sec6_vwap(indices, data_map, signals, regimes)
    _sec7_order_manager()
    _sec8_signal_injection(indices, data_map)
    _sec9_paper_audit()
    _sec10_recommendations(data_map, signals, regimes, orb_filters, vwap_filters, vix)

    print(f"{'═' * W}")
    print(f"  Diagnostic complete — {now.strftime('%H:%M:%S IST')}")
    print(f"{'═' * W}\n")


if __name__ == "__main__":
    main()
