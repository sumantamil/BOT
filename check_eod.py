#!/usr/bin/env python3
"""
check_eod.py
============
Diagnose the EOD Closing Momentum strategy using the bot's real config
and yfinance data. No live bot process required.

Usage:
    python check_eod.py          # full status report
    python check_eod.py --live   # loop and re-check every 60 s (for use 14:30–15:00)
    python check_eod.py --perf   # show historical EOD win/loss stats

The EOD strategy fires ONCE per index per day between 14:30–15:00 IST.
It reads the last COMPLETED 15-minute candle and buys CE (bullish candle)
or PE (bearish candle) if the body is ≥ 50% of the candle's range.
"""
from __future__ import annotations

import json
import os
import sys
import time as _time
from datetime import datetime, date, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

SEP  = "=" * 70
SEP2 = "-" * 70
_IST = ZoneInfo("Asia/Kolkata")

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

from config import settings
from bot.index_config import NIFTY, BANKNIFTY, SENSEX


def _hdr(title: str) -> None:
    print(); print(SEP); print(f"  {title}"); print(SEP)


def _now_ist() -> datetime:
    return datetime.now(_IST)


# ---------------------------------------------------------------------------
# Section 1 — Config
# ---------------------------------------------------------------------------

def show_config() -> None:
    _hdr("⚙️  EOD STRATEGY CONFIGURATION")
    cfg = settings.eod

    print(f"  EOD_ENABLED          : {'✅ YES' if cfg.enabled else '❌ NO'}")
    print(f"  Entry window         : {cfg.entry_start_hour:02d}:{cfg.entry_start_minute:02d} – "
          f"{cfg.entry_end_hour:02d}:{cfg.entry_end_minute:02d} IST")
    print(f"  EOD_MIN_BODY_PCT     : {cfg.min_body_pct:.0%}  (candle body / range)")
    print(f"  Force-exit at        : 15:27 IST  (hardcoded in engine)")
    print()

    if not cfg.enabled:
        print("  ⚠️  EOD disabled. Add EOD_ENABLED=true to .env to activate.")
        return

    if cfg.min_body_pct > 0.65:
        print(f"  ⚠️  min_body_pct={cfg.min_body_pct:.0%} is strict — few candles will qualify.")
    if cfg.min_body_pct < 0.30:
        print(f"  ⚠️  min_body_pct={cfg.min_body_pct:.0%} is relaxed — doji candles may trade.")


# ---------------------------------------------------------------------------
# Section 2 — Time status
# ---------------------------------------------------------------------------

def show_time_status() -> bool:
    """Print current status and return True if inside entry window."""
    _hdr("🕐 CURRENT TIME STATUS")
    cfg  = settings.eod
    now  = _now_ist()
    cur  = now.time()

    start = dtime(cfg.entry_start_hour, cfg.entry_start_minute)
    end   = dtime(cfg.entry_end_hour,   cfg.entry_end_minute)
    force = dtime(15, 27)
    close = dtime(15, 30)

    print(f"  Current IST time : {now.strftime('%H:%M:%S')}")
    print()

    def _fmt(t: dtime) -> str:
        return t.strftime("%H:%M IST")

    rows = [
        ("Entry window opens",  start,  cur < start),
        ("Entry window closes", end,    start <= cur < end),
        ("Force-exit (15:27)",  force,  end <= cur < force),
        ("Market closes",       close,  force <= cur < close),
    ]

    for label, t, active in rows:
        marker = "◀ NOW" if active else ""
        print(f"  {t.strftime('%H:%M')}  {label:<28}  {marker}")

    print()

    if cur < start:
        delta_min = int(((datetime.combine(date.today(), start) - now.replace(tzinfo=None)).total_seconds()) / 60)
        print(f"  ⏳ EOD window opens in ~{delta_min} minutes.")
        print(f"     Bot will NOT check EOD signals before {_fmt(start)}.")
        return False
    elif start <= cur < end:
        elapsed = int((now.replace(tzinfo=None) - datetime.combine(date.today(), start)).total_seconds() / 60)
        remaining = int((datetime.combine(date.today(), end) - now.replace(tzinfo=None)).total_seconds() / 60)
        print(f"  ✅ INSIDE EOD ENTRY WINDOW")
        print(f"     Elapsed: {elapsed} min  |  Remaining: {remaining} min")
        print(f"     Bot is actively checking for a qualifying candle every ~60 s.")
        return True
    elif end <= cur < force:
        print(f"  🔒 Entry window closed. No new EOD positions.")
        print(f"     Open ESO positions will trail until force-exit at 15:27.")
        return False
    elif force <= cur < close:
        print(f"  🚨 FORCE-EXIT TIME. Engine closes all positions now.")
        return False
    else:
        print(f"  🔴 Market closed.")
        return False


# ---------------------------------------------------------------------------
# Section 3 — Live candle check
# ---------------------------------------------------------------------------

def check_candle(idx=NIFTY) -> None:
    """Fetch the last completed 15m candle and simulate what the engine does."""
    _hdr(f"🕯️  LAST COMPLETED 15m CANDLE  —  {idx.display_name}")
    import yfinance as yf
    import pandas as pd

    cfg = settings.eod
    try:
        ticker = yf.Ticker(idx.yahoo_symbol)
        data   = ticker.history(period="1d", interval="15m")
    except Exception as e:
        print(f"  ❌ yfinance error: {e}")
        return

    if data is None or data.empty or len(data) < 2:
        print("  ❌ Insufficient 15m data. Market may be closed or data unavailable.")
        return

    # IST candle filter (same logic as engine._check_eod_signal)
    try:
        if hasattr(data.index, "tz") and data.index.tz:
            data_ist = data.copy()
            data_ist.index = data_ist.index.tz_convert(_IST)
        else:
            data_ist = data.copy()
            data_ist.index = data_ist.index.tz_localize(_IST)

        now_ist  = datetime.now(_IST)
        completed = data_ist[
            data_ist.index + pd.Timedelta(minutes=15) <= pd.Timestamp(now_ist)
        ]
        if completed.empty:
            print("  ⚠️  No completed 15m candle yet (current candle still forming).")
            return
        last      = completed.iloc[-1]
        candle_ts = completed.index[-1]
    except Exception as err:
        print(f"  ⚠️  IST filter failed ({err}) — using iloc[-2] fallback")
        last      = data.iloc[-2]
        candle_ts = data.index[-2]

    o = float(last["Open"])
    h = float(last["High"])
    l = float(last["Low"])
    c = float(last["Close"])

    candle_range = h - l
    body         = abs(c - o)
    body_pct     = body / candle_range if candle_range > 0 else 0
    is_bullish   = c > o
    direction    = "BULLISH ↑" if is_bullish else "BEARISH ↓"
    signal       = "CE" if is_bullish else "PE"

    print(f"  Candle time  : {candle_ts.strftime('%H:%M')} IST  (15m)")
    print(f"  Open         : {o:,.2f}")
    print(f"  High         : {h:,.2f}")
    print(f"  Low          : {l:,.2f}")
    print(f"  Close        : {c:,.2f}")
    print(f"  Range        : {candle_range:.2f} pts")
    print(f"  Body         : {body:.2f} pts  ({body_pct:.0%} of range)")
    print(f"  Direction    : {direction}")
    print()

    min_body = cfg.min_body_pct
    if body_pct >= min_body:
        print(f"  ✅ Body {body_pct:.0%} ≥ {min_body:.0%} threshold — SIGNAL QUALIFIES")
        print(f"     Engine would fire: BUY {signal} ATM")
        print(f"     Position exits    : 15:27 IST force-close")
    else:
        pct_needed = int((min_body - body_pct) * 100)
        print(f"  ❌ Body {body_pct:.0%} < {min_body:.0%} threshold — DOJI / INDECISION candle")
        print(f"     Body needs to grow ~{pct_needed}% more of range before signal qualifies.")
        print(f"     Engine will check again next analysis cycle (~60 s).")


# ---------------------------------------------------------------------------
# Section 4 — Historical EOD performance
# ---------------------------------------------------------------------------

def show_perf() -> None:
    _hdr("📊 HISTORICAL EOD WIN/LOSS RECORD")
    perf_file = _ROOT / ".eod_performance.json"
    if not perf_file.exists():
        print("  ℹ️  .eod_performance.json not found.")
        print("  No EOD trades recorded yet (or bot not yet started).")
        return

    with open(perf_file) as f:
        data = json.load(f)
    results: list[bool] = [bool(v) for v in data.get("results", [])]
    n = len(results)
    if n == 0:
        print("  ℹ️  No EOD trades in history yet.")
        return

    wins  = sum(results)
    losses = n - wins
    wr    = wins / n * 100

    print(f"  Total EOD trades : {n}")
    print(f"  Wins             : {wins}")
    print(f"  Losses           : {losses}")
    print(f"  Win Rate         : {wr:.1f}%")
    print()

    # Streak analysis
    streak = 0; max_win_streak = 0; max_loss_streak = 0; cur_streak_type = None
    for r in results:
        if cur_streak_type == r:
            streak += 1
        else:
            cur_streak_type = r
            streak = 1
        if r:
            max_win_streak  = max(max_win_streak,  streak)
        else:
            max_loss_streak = max(max_loss_streak, streak)

    print(f"  Max win streak   : {max_win_streak}")
    print(f"  Max loss streak  : {max_loss_streak}")

    # Recent 10
    recent = results[-10:]
    print()
    print(f"  Last {len(recent)} results (oldest→newest):")
    print("  " + "  ".join("✅" if r else "❌" for r in recent))
    print()

    if n >= 20:
        if wr < 45:
            print(f"  🔴 Win rate {wr:.1f}% < 45% over {n} trades → consider EOD_ENABLED=false")
        elif wr < 55:
            print(f"  🟡 Win rate {wr:.1f}% < 55% over {n} trades → marginal; continue monitoring")
        else:
            print(f"  ✅ Win rate {wr:.1f}% over {n} trades → EOD strategy is performing well")
    else:
        remaining = 20 - n
        print(f"  ℹ️  Need {remaining} more trades (≥20 total) for statistically reliable verdict.")


# ---------------------------------------------------------------------------
# Section 5 — Simulate EOD day timeline
# ---------------------------------------------------------------------------

def show_timeline() -> None:
    _hdr("📅 EOD STRATEGY TIMELINE (TODAY)")
    cfg = settings.eod
    print(f"  {date.today()}  —  EOD Closing Momentum  ({NIFTY.display_name} / BANKNIFTY / SENSEX)")
    print()
    timeline = [
        ("09:15", "Market opens",                "Gap & ORB strategies active"),
        ("09:30", "ORB entry window opens",       "ORB monitoring begins"),
        ("09:30", "VWAP monitoring begins",       "VWAP deviation tracking"),
        ("14:00", "Auto-trend cutoff",            "No new trend entries after 14:00"),
        (f"{cfg.entry_start_hour:02d}:{cfg.entry_start_minute:02d}",
                  "EOD entry window OPENS",       "Bot reads completed 15m candles every ~60 s"),
        ("14:45", "Typical candle check",         f"Body ≥ {cfg.min_body_pct:.0%} → BUY CE or PE"),
        (f"{cfg.entry_end_hour:02d}:{cfg.entry_end_minute:02d}",
                  "EOD entry window CLOSES",      "No new EOD entries; existing positions monitored"),
        ("15:15", "ORB force-exit (if any)",      "ORB positions closed"),
        ("15:27", "🚨 Full force-exit",           "Engine closes ALL open positions"),
        ("15:30", "Market closes",                "Session complete"),
    ]

    now_str = _now_ist().strftime("%H:%M")
    for t, event, note in timeline:
        marker = "◀ NOW " if t <= now_str < (timeline[timeline.index((t, event, note)) + 1][0] if timeline.index((t, event, note)) < len(timeline) - 1 else "99:99") else "      "
        print(f"  {t}  {marker}{event:<35} {note}")


# ---------------------------------------------------------------------------
# Live loop
# ---------------------------------------------------------------------------

def live_loop() -> None:
    print()
    print(SEP)
    print("  🔴 LIVE EOD MONITOR  (press Ctrl+C to stop)")
    print(SEP)

    try:
        while True:
            now = _now_ist()
            print(f"\n  [{now.strftime('%H:%M:%S')}]  Checking…")
            in_window = show_time_status()
            if in_window:
                for idx in [NIFTY, BANKNIFTY, SENSEX]:
                    check_candle(idx)
            else:
                cur = now.time()
                if cur >= dtime(15, 27):
                    print("  Market force-exit time reached — stopping monitor.")
                    break
            print(f"\n  Next check in 60 s  (Ctrl+C to stop)…")
            _time.sleep(60)
    except KeyboardInterrupt:
        print("\n\n  Monitor stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = set(sys.argv[1:])

    if "--live" in args:
        live_loop()
        return

    print()
    print(SEP)
    print("  🕐 EOD CLOSING MOMENTUM — DIAGNOSTIC")
    print(f"  {datetime.now(_IST).strftime('%Y-%m-%d  %H:%M:%S')} IST")
    print(SEP)

    show_config()
    in_window = show_time_status()
    show_timeline()

    if "--perf" in args:
        show_perf()
        print()
        return

    show_perf()

    # Only fetch live candle data if close to or inside the EOD window
    cur = _now_ist().time()
    if dtime(14, 0) <= cur < dtime(15, 30):
        for idx in [NIFTY, BANKNIFTY, SENSEX]:
            check_candle(idx)
    else:
        print()
        _hdr("ℹ️  CANDLE CHECK")
        print("  Currently outside 14:00–15:30 IST.")
        print("  Run between 14:30–15:00 to see a live candle analysis.")
        print("  Or use --live flag to auto-refresh every 60 s during the window.")

    print()
    print("  Tips:")
    print("    python check_eod.py --live   — live refresh every 60 s (14:30–15:00 IST)")
    print("    python check_eod.py --perf   — show win/loss history only")
    print()


if __name__ == "__main__":
    main()
