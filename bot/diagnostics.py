"""
bot/diagnostics.py — Standalone diagnostic runner for the trading bot.

Run with:
    python bot/diagnostics.py
    python -m bot.diagnostics

Checks data freshness, strategy conditions, and filter settings to identify
why signals are (or aren't) being generated.  Safe to run at any time —
read-only, no trades placed.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yfinance as yf
import pandas as pd
from datetime import datetime, time
from zoneinfo import ZoneInfo
from loguru import logger

# Suppress strategy-internal log noise so diagnostics output stays clean.
# The engine's own logs are still useful during live operation, but here we
# only want the formatted diagnostic table.
logger.disable("bot.trend_analyzer")
logger.disable("bot.orb_strategy")
logger.disable("bot.vwap_strategy")
logger.disable("bot.market_regime")

from config import settings
from bot.index_config import NIFTY, BANKNIFTY, SENSEX

IST = ZoneInfo("Asia/Kolkata")
INDICES = [NIFTY, BANKNIFTY, SENSEX]
W = 60  # line width for headers


# ── formatting helpers ────────────────────────────────────────────────────────

def _hdr(title: str) -> str:
    return f"\n{'─' * W}\n{title}\n{'─' * W}"


def _now_ist() -> datetime:
    return datetime.now(IST)


# ── data helpers ──────────────────────────────────────────────────────────────

def _fetch_5m(symbol: str) -> pd.DataFrame:
    """Fetch today's 5-minute candles for a Yahoo Finance symbol."""
    try:
        return yf.Ticker(symbol).history(period="1d", interval="5m")
    except Exception:
        return pd.DataFrame()


def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    """Return the latest RSI value (period bars), or 50.0 if not computable."""
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
    """Calculate intraday VWAP using today's candles only."""
    now_date = _now_ist().date()
    mask = pd.Series(
        [(ts.date() == now_date if hasattr(ts, "date") else False) for ts in df.index],
        index=df.index,
    )
    today = df[mask]
    if today.empty:
        today = df  # fallback: use all available candles
    tp = (today["High"] + today["Low"] + today["Close"]) / 3
    if "Volume" in today.columns and today["Volume"].sum() > 0:
        return float((tp * today["Volume"]).sum() / today["Volume"].sum())
    return float(tp.mean())


def _orb_range(df: pd.DataFrame) -> tuple:
    """
    Compute the opening range from today's opening candles.

    Returns (range_low, range_high, n_candles).
    """
    window_min = settings.orb.window_minutes
    now_date = _now_ist().date()
    market_open_dt = datetime(now_date.year, now_date.month, now_date.day, 9, 15, tzinfo=IST)
    range_end_dt = market_open_dt + __import__("datetime").timedelta(minutes=window_min)

    mask = (df.index >= market_open_dt) & (df.index < range_end_dt)
    candles = df[mask]
    if candles.empty:
        return 0.0, 0.0, 0
    return float(candles["Low"].min()), float(candles["High"].max()), len(candles)


# ── section 1: market data ───────────────────────────────────────────────────

def check_data_freshness() -> tuple:
    """Fetch 5m candles and VIX; print freshness report. Returns (data_map, vix_val)."""
    print(_hdr("[1] MARKET DATA FRESHNESS"))
    data_map: dict = {}

    for idx in INDICES:
        df = _fetch_5m(idx.yahoo_symbol)
        if df.empty:
            print(f"  ❌ {idx.name:<12} ({idx.yahoo_symbol}): fetch returned empty")
            data_map[idx.name] = None
            continue

        last_ts = df.index[-1]
        last_ts_ist = last_ts.tz_convert(IST) if hasattr(last_ts, "tz_convert") else last_ts
        age_min = (_now_ist() - last_ts_ist).total_seconds() / 60
        stale = "  ⚠️ STALE" if age_min > 20 else ""
        mark = "✅" if age_min <= 20 else "⚠️"
        print(
            f"  {mark} {idx.name:<12} ({idx.yahoo_symbol}): "
            f"{len(df)} candles | latest {last_ts_ist.strftime('%H:%M')} IST "
            f"| age {age_min:.0f}min{stale}"
        )
        data_map[idx.name] = df

    # India VIX
    vix_val: float | None = None
    try:
        vix_df = yf.Ticker("^INDIAVIX").history(period="1d", interval="5m")
        if not vix_df.empty:
            vix_val = float(vix_df["Close"].iloc[-1])
            print(f"  ✅ India VIX              : {vix_val:.2f}")
        else:
            print(f"  ❌ India VIX              : fetch returned empty")
    except Exception as e:
        print(f"  ❌ India VIX              : {e}")

    return data_map, vix_val


# ── section 2: market regime ─────────────────────────────────────────────────

def check_market_regime() -> None:
    print(_hdr("[2] MARKET REGIME"))
    try:
        from bot.market_regime import MarketRegimeDetector
    except ImportError as e:
        print(f"  ❌ Could not import MarketRegimeDetector: {e}")
        return

    det = MarketRegimeDetector()
    for idx in INDICES:
        det.set_index(idx)
        r = det.analyze()
        if r is None:
            print(f"  {idx.name:<12}: ❓ N/A (insufficient history — need 60+ daily candles)")
        else:
            trade_str = "trade=YES" if r.should_trade else "trade=NO ← avoid entries"
            print(f"  {'✅' if r.should_trade else '⚠️'} {idx.name:<12}: {r.regime.value:<16} | ADX={r.adx:.1f}  {trade_str}")


# ── section 3: trend strength ────────────────────────────────────────────────

def check_trend() -> dict:
    """Run TrendAnalyzer for each index; returns {name: TrendSignal|None}."""
    print(_hdr("[3] TREND STRENGTH  (5-day 5m window)"))
    try:
        from bot.trend_analyzer import TrendAnalyzer, Trend
    except ImportError as e:
        print(f"  ❌ Could not import TrendAnalyzer: {e}")
        return {}

    results = {}
    for idx in INDICES:
        ta = TrendAnalyzer(index_config=idx)
        sig = ta.analyze()
        if sig is None:
            print(f"  ❌ {idx.name:<12}: analyze() returned None — data fetch may have failed")
            results[idx.name] = None
            continue

        # Re-derive the adaptive threshold so we can show it
        threshold = ta.get_dynamic_confidence_threshold(sig.atr, sig.current_price)
        ok = sig.strength >= threshold
        mark = "✅" if ok else "❌"
        arrow = f"≥ {threshold} ✓" if ok else f"< {threshold} ✗"
        print(
            f"  {mark} {idx.name:<12}: {sig.trend.value:<8}  strength={sig.strength}%  "
            f"threshold={threshold}  ({arrow})"
        )
        print(f"             price={sig.current_price:.0f}  rec={sig.recommendation}")
        results[idx.name] = sig

    return results


# ── section 4: ORB status ────────────────────────────────────────────────────

def check_orb(data_map: dict) -> None:
    print(_hdr("[4] ORB STATUS  (opening range breakout)"))

    window_min = settings.orb.window_minutes
    range_end_h = (9 * 60 + 15 + window_min) // 60
    range_end_m = (9 * 60 + 15 + window_min) % 60
    range_end_str = f"{range_end_h:02d}:{range_end_m:02d}"
    entry_end_str = f"{settings.orb.entry_end_time}" if hasattr(settings.orb, "entry_end_time") else "11:30"
    buf_pct = settings.orb.breakout_buffer_pct

    now = _now_ist()
    now_min = now.hour * 60 + now.minute
    range_locks_min = 9 * 60 + 15 + window_min

    for idx in INDICES:
        df = data_map.get(idx.name)
        if df is None or df.empty:
            print(f"  ❌ {idx.name:<12}: no data")
            continue

        price = float(df["Close"].iloc[-1])
        r_low, r_high, r_candles = _orb_range(df)

        if now.hour * 60 + now.minute < 9 * 60 + 15:
            print(f"  🕐 {idx.name:<12}: WAITING — market opens 09:15 IST")
        elif r_candles == 0:
            print(f"  🕐 {idx.name:<12}: WAITING — no opening candles yet")
        elif now_min < range_locks_min:
            # Still building
            pct = (r_high - r_low) / r_low * 100 if r_low > 0 else 0.0
            print(
                f"  🔨 {idx.name:<12}: BUILDING | so far {r_low:.0f}–{r_high:.0f} "
                f"({r_high - r_low:.0f}pts, {pct:.2f}%) | range locks at {range_end_str}"
            )
        else:
            # Range is locked — evaluate breakout conditions
            pct = (r_high - r_low) / r_low * 100 if r_low > 0 else 0.0
            buf = buf_pct / 100.0
            long_trigger = r_high * (1 + buf)
            short_trigger = r_low * (1 - buf)
            min_range = idx.strike_interval * 1.5

            range_ok = (r_high - r_low) >= min_range
            long_ok = price > long_trigger
            short_ok = price < short_trigger

            range_mark = "✓" if range_ok else f"✗(need ≥{min_range:.0f}pts)"
            long_mark = "✓" if long_ok else f"✗(need >{long_trigger:.0f})"
            short_mark = "✓" if short_ok else f"✗(need <{short_trigger:.0f})"

            status = "✅ READY" if range_ok else "⚠️ READY (range too narrow)"
            print(
                f"  {status} {idx.name:<10}: range {r_low:.0f}–{r_high:.0f} "
                f"({r_high - r_low:.0f}pts, {pct:.2f}%)  size {range_mark}"
            )
            print(
                f"              price={price:.0f}  "
                f"LONG {long_mark}  SHORT {short_mark}  "
                f"(entry window closes {entry_end_str})"
            )


# ── section 5: VWAP status ───────────────────────────────────────────────────

def check_vwap(data_map: dict) -> None:
    print(_hdr("[5] VWAP STATUS"))

    dev_threshold = settings.vwap.deviation_pct
    rsi_oversold = settings.vwap.rsi_oversold
    rsi_overbought = settings.vwap.rsi_overbought

    for idx in INDICES:
        df = data_map.get(idx.name)
        if df is None or df.empty:
            print(f"  ❌ {idx.name:<12}: no data")
            continue

        price = float(df["Close"].iloc[-1])
        vwap = _calc_vwap(df)
        dev = (price - vwap) / vwap * 100
        rsi = _calc_rsi(df["Close"])

        long_dev = dev <= -dev_threshold
        long_rsi = rsi < rsi_oversold
        short_dev = dev >= dev_threshold
        short_rsi = rsi > rsi_overbought

        long_ok = long_dev and long_rsi
        short_ok = short_dev and short_rsi
        any_signal = long_ok or short_ok

        mark = "✅" if any_signal else "❌"
        print(
            f"  {mark} {idx.name:<12}: price={price:.0f} vwap={vwap:.0f} "
            f"dev={dev:+.2f}% rsi={rsi:.0f}"
        )

        l_dev_s = "✓" if long_dev else f"✗({dev:+.2f}% need ≤-{dev_threshold}%)"
        l_rsi_s = "✓" if long_rsi else f"✗({rsi:.0f} need <{rsi_oversold})"
        s_dev_s = "✓" if short_dev else f"✗({dev:+.2f}% need ≥+{dev_threshold}%)"
        s_rsi_s = "✓" if short_rsi else f"✗({rsi:.0f} need >{rsi_overbought})"

        long_verdict = "→ ✅ LONG SETUP" if long_ok else "→ ❌ no long"
        short_verdict = "→ ✅ SHORT SETUP" if short_ok else "→ ❌ no short"
        print(f"              LONG:  dev {l_dev_s}  rsi {l_rsi_s}  {long_verdict}")
        print(f"              SHORT: dev {s_dev_s}  rsi {s_rsi_s}  {short_verdict}")


# ── section 6: filters & settings ───────────────────────────────────────────

def check_filters(vix_val: float | None) -> None:
    print(_hdr("[6] FILTERS & SETTINGS"))
    t = settings.trading

    auto = getattr(t, "auto_trade_enabled", False)
    mode_str = "ENABLED — live order execution" if auto else "DISABLED — paper mode only"
    print(f"  {'auto_trade':<22}: {mode_str}")

    # VIX filter
    vix_enabled = getattr(t, "vix_filter_enabled", True)
    vix_max = getattr(t, "vix_max", 20.0)
    if vix_enabled:
        if vix_val is not None:
            vix_ok = vix_val <= vix_max
            mark = "✅" if vix_ok else "❌"
            res = f"VIX={vix_val:.2f} ≤ {vix_max} → OK" if vix_ok else f"VIX={vix_val:.2f} > {vix_max} → BLOCK"
        else:
            mark = "⚠️"
            res = "VIX unavailable — would default to allow"
        print(f"  {mark} {'VIX filter':<22}: ENABLED | {res}")
    else:
        print(f"     {'VIX filter':<22}: DISABLED (TRADING_VIX_FILTER_ENABLED=false)")

    # IV filter
    iv_enabled = getattr(t, "iv_filter_enabled", False)
    iv_max = getattr(t, "iv_percentile_max", 80.0)
    if iv_enabled:
        print(f"  ⚠️  {'IV filter':<22}: ENABLED | threshold = {iv_max:.0f}th percentile")
    else:
        print(f"     {'IV filter':<22}: DISABLED (iv_filter_enabled=false)")

    # Position cap
    max_pos = getattr(t, "max_positions", 2)
    max_loss = getattr(t, "max_daily_loss", 6000.0)
    sl_pct = getattr(t, "stop_loss_percentage", 20.0)
    target_pct = getattr(t, "target_percentage", 50.0)
    print(f"  {'Position cap':<22}: 0 / {max_pos} open (standalone — no live positions)")
    print(f"  {'Stop loss / Target':<22}: {sl_pct:.0f}% / {target_pct:.0f}%  |  daily loss cap ₹{max_loss:,.0f}")


# ── section 7: verdict ───────────────────────────────────────────────────────

def verdict(data_map: dict, vix_val: float | None, trend_results: dict) -> None:
    print(_hdr("[7] VERDICT"))

    ok_list: list[str] = []
    issues: list[str] = []

    # Data
    bad_data = [name for name, df in data_map.items() if df is None or df.empty]
    if bad_data:
        issues.append(f"Data fetch failed: {', '.join(bad_data)}")
    else:
        ok_list.append("Market data fresh for all indices")

    # VIX
    vix_enabled = getattr(settings.trading, "vix_filter_enabled", True)
    vix_max = getattr(settings.trading, "vix_max", 20.0)
    auto = getattr(settings.trading, "auto_trade_enabled", False)
    if vix_enabled and auto and vix_val is not None and vix_val > vix_max:
        issues.append(f"VIX={vix_val:.1f} > {vix_max} — live entries would be blocked")
    elif not vix_enabled or not auto:
        ok_list.append("VIX filter not blocking (paper mode or filter off)")
    else:
        ok_list.append(f"VIX={vix_val:.1f} within limit ({vix_max})")

    # Trend
    try:
        from bot.trend_analyzer import Trend
        neutral = [name for name, sig in trend_results.items() if sig is None or sig.trend == Trend.NEUTRAL]
        if neutral:
            issues.append(f"TREND NEUTRAL for {'/'.join(neutral)} — ORB/VWAP still possible but trend gate blocks engine")
        else:
            ok_list.append("Trend conditions met for all indices")
    except ImportError:
        pass

    # Paper mode note
    if not auto:
        ok_list.append("Paper mode — positions tracked without real orders")

    print()
    for msg in ok_list:
        print(f"  ✅ {msg}")
    for msg in issues:
        print(f"  ❌ {msg}")

    if not issues:
        print("\n  All conditions green — monitor live logs for signal traces.")
    else:
        print(f"\n  {len(issues)} issue(s) identified above.")

    # Time-of-day advisory
    now = _now_ist()
    now_t = now.time()
    print()
    if now_t < time(9, 15):
        print("  ℹ️  Pre-market: ORB/VWAP signals only fire after 09:15 IST.")
    elif now_t > time(15, 30):
        print("  ℹ️  After market close: no new signals until next trading day.")
    else:
        print("  ℹ️  Market hours: engine should be scanning every ~120s.")

    print()


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    now = _now_ist()
    print(f"\n{'═' * W}")
    print(f"  TRADING BOT DIAGNOSTICS — {now.strftime('%a %d %b %Y  %H:%M:%S IST')}")
    print(f"{'═' * W}")

    data_map, vix_val = check_data_freshness()
    check_market_regime()
    trend_results = check_trend()
    check_orb(data_map)
    check_vwap(data_map)
    check_filters(vix_val)
    verdict(data_map, vix_val, trend_results)


if __name__ == "__main__":
    main()
