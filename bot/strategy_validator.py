"""
Strategy Validator
==================
Comprehensive walk-forward and per-strategy backtesting framework.

Runs five independent simulators — one per live strategy — and produces a
single, structured validation report that answers:

  1. Is each strategy statistically profitable (win rate, profit factor, Sharpe)?
  2. Is the Trend strategy OVERFIT (train win-rate − test win-rate > 15 pts)?
  3. Which strategies should be ENABLED vs DISABLED for the next live session?
  4. What is the recommended capital allocation given each strategy's Kelly fraction?

Data sources (all via yfinance — no extra dependencies):
  Daily candles  → Trend, Gap            (up to 2 years)
  5-min candles  → ORB, VWAP            (last 60 days — yfinance limit)
  15-min candles → EOD                   (last 60 days)

Usage:
    # From project root with venv active:
    python -m bot.strategy_validator               # quick (default settings)
    python -m bot.strategy_validator --full        # uses all available history
    python -m bot.strategy_validator --index BANKNIFTY
    python -m bot.strategy_validator --json out.json

    # Or import programmatically:
    from bot.strategy_validator import StrategyValidator
    rpt = StrategyValidator().run_all()
    print(rpt.summary())
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta, time as dtime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.index_config import IndexConfig, NIFTY, BANKNIFTY, SENSEX, get_index

# ──────────────────────────────────────────────────────────────────────────────
# Shared constants
# ──────────────────────────────────────────────────────────────────────────────

_SL_OPTION_PCT   = 15.0   # live stop-loss on options (%)
_TGT_OPTION_PCT  = 30.0   # live tier-1 target on options (%)
_LEVERAGE        = 12.0   # conservative option leverage (ATM ≈12×; farther OTM is lower)
# Read VIX threshold from live settings so validator always mirrors the running bot.
try:
    import sys as _sys; _sys.path.insert(0, '..')
    from config import settings as _cfg
    _VIX_BLOCK = float(_cfg.trading.vix_max)
except Exception:
    _VIX_BLOCK = 27.0   # fallback matching TRADING_VIX_MAX in .env
# Spot equivalents (option % / leverage)
_SL_SPOT_PCT     = _SL_OPTION_PCT  / _LEVERAGE   # 1.0 %
_TGT_SPOT_PCT    = _TGT_OPTION_PCT / _LEVERAGE   # 2.0 %

_MIN_TRADES_FOR_STATS = 10   # fewer → "insufficient sample"
_OVERFIT_THRESHOLD    = 15.0  # pts gap between in-sample and out-of-sample win rate

# Realistic cost model (applied to all simulators)
_SLIPPAGE_ENTRY_PCT = 1.0   # option entry slippage % (bid-ask spread + market impact)
_THETA_HOURLY_PCT   = 0.5   # hourly theta decay as % of option value (intraday holds ~3%/6hr)
_THETA_DAILY_PCT    = 2.5   # daily theta decay as % of option value (overnight holds)


# ──────────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    strategy:     str
    direction:    str        # "CE" or "PE"
    entry_date:   datetime
    exit_date:    datetime
    entry_price:  float
    exit_price:   float
    pnl_pct:      float      # option-equivalent P&L in %
    pnl_pts:      float      # spot-point P&L
    win:          bool
    exit_reason:  str        # "TARGET" | "SL" | "TIME" | "REVERSAL" | "CLOSE"
    strength:     float      # signal strength at entry (0-100)


@dataclass
class PeriodResult:
    """Stats for one time window (in-sample or out-of-sample)."""
    label:          str
    start:          str
    end:            str
    n_trades:       int
    win_rate:       float    # 0-100
    avg_win_pct:    float
    avg_loss_pct:   float
    profit_factor:  float
    sharpe:         float
    max_drawdown_pct: float
    total_return_pct: float
    kelly_fraction: float    # optimal bet size as fraction of capital
    trades:         List[Trade] = field(default_factory=list)

    def is_valid(self) -> bool:
        return self.n_trades >= _MIN_TRADES_FOR_STATS

    def verdict(self) -> str:
        if not self.is_valid():
            return "INSUFFICIENT_DATA"
        # Strong pass: high WR + positive expectancy + good risk-adjusted return
        if self.win_rate >= 55 and self.profit_factor >= 1.3 and self.sharpe >= 0.5:
            return "PASS"
        # High PF + good Sharpe = positive expectancy regardless of WR.
        # e.g. VWAP: WR=45% but PF=1.78, Sharpe=3.76 → winners far outsize losers.
        if self.profit_factor >= 1.5 and self.sharpe >= 1.5:
            return "PASS"
        # Borderline: modest edge, keep paper-trading
        if self.win_rate >= 50 and self.profit_factor >= 1.0:
            return "MARGINAL"
        if self.profit_factor >= 1.2 and self.sharpe >= 0.5:
            return "MARGINAL"
        return "FAIL"


@dataclass
class WalkForwardResult:
    """Train/test folds for overfitting detection."""
    strategy:         str
    folds:            List[Tuple[PeriodResult, PeriodResult]]  # (in_sample, out_of_sample)
    avg_train_wr:     float
    avg_test_wr:      float
    overfit_detected: bool
    overfit_gap:      float   # train_wr - test_wr in percentage points

    def verdict(self) -> str:
        if self.avg_train_wr < 0:
            return "⚪ SPARSE — fewer than 2 folds had ≥5 trades on both sides; overfitting cannot be determined"
        if self.overfit_detected:
            return f"⚠️  OVERFIT (train {self.avg_train_wr:.1f}% vs test {self.avg_test_wr:.1f}% — gap {self.overfit_gap:.1f} pts)"
        return f"✅ OK (train {self.avg_train_wr:.1f}% vs test {self.avg_test_wr:.1f}% — gap {self.overfit_gap:.1f} pts)"


@dataclass
class StrategyReport:
    """Full validation result for a single strategy."""
    strategy:     str
    index:        str
    full_period:  PeriodResult
    walk_forward: Optional[WalkForwardResult]
    note:         str                          # human-readable summary
    recommended:  bool                         # should this strategy be ENABLED?

    def one_liner(self) -> str:
        fp = self.full_period
        verdict = fp.verdict()
        icon = {"PASS": "✅", "MARGINAL": "🟡", "FAIL": "❌", "INSUFFICIENT_DATA": "⚪"}.get(verdict, "")
        wr_str = f"{fp.win_rate:.1f}% WR" if fp.is_valid() else "—"
        pf_str = f"PF {fp.profit_factor:.2f}" if fp.is_valid() else "—"
        sh_str = f"Sharpe {fp.sharpe:.2f}" if fp.is_valid() else "—"
        n_str  = f"{fp.n_trades} trades"
        wf_str = ""
        if self.walk_forward:
            wf_str = f"  |  WF: {self.walk_forward.verdict()}"
        return (
            f"{icon} {self.strategy:<8} {wr_str:>12}  {pf_str:>10}  {sh_str:>12}  "
            f"{n_str:>10}{wf_str}"
        )


@dataclass
class ValidationReport:
    """Top-level report returned by StrategyValidator.run_all()."""
    generated_at: str
    index:        str
    strategies:   List[StrategyReport]

    def summary(self) -> str:
        lines = [
            "",
            "╔══════════════════════════════════════════════════════════════════════════╗",
            "║           STRATEGY VALIDATION REPORT  —  NIFTY Options Bot              ║",
            f"║  Index: {self.index:<10}  Generated: {self.generated_at}                ║",
            "╚══════════════════════════════════════════════════════════════════════════╝",
            "",
            f"  {'Strategy':<10} {'Win Rate':>12}  {'Prof.Factor':>10}  {'Sharpe':>12}  {'Trades':>10}",
            "  " + "─" * 68,
        ]
        for rpt in self.strategies:
            lines.append("  " + rpt.one_liner())
        lines += ["", "  RECOMMENDATIONS:", "  " + "─" * 68]
        for rpt in self.strategies:
            sym = "✅ ENABLE " if rpt.recommended else "❌ DISABLE"
            lines.append(f"  {sym}  {rpt.strategy:<10}  {rpt.note}")
        lines += [
            "",
            "  " + "─" * 68,
            "  Filters active (matching live bot):",
            f"    • India VIX ≥ {_VIX_BLOCK:.0f}  →  entry skipped (all strategies)",
            "    • ADX < 25        →  trend entry skipped",
            "    • Leverage        →  8×–12× dynamic (ATM=12×, 1%OTM=10×, 2%+OTM=8×)",
            "    • Entry slippage  →  1% (bid-ask spread + market impact)",
            "    • Theta decay     →  intraday 0.5%/hr (~3% for 6-hr hold); daily 2.5%/day",
            "",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        def _convert(obj):
            if isinstance(obj, (datetime, date)):
                return str(obj)
            if hasattr(obj, "__dict__"):
                return {k: _convert(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, list):
                return [_convert(i) for i in obj]
            if isinstance(obj, tuple):
                return [_convert(i) for i in obj]
            return obj
        return _convert(self)


# ──────────────────────────────────────────────────────────────────────────────
# Statistics helpers
# ──────────────────────────────────────────────────────────────────────────────

def _calc_stats(trades: List[Trade], label: str, start: str, end: str) -> PeriodResult:
    """Compute all statistics from a list of Trade objects."""
    n = len(trades)
    if n == 0:
        return PeriodResult(
            label=label, start=start, end=end, n_trades=0,
            win_rate=0, avg_win_pct=0, avg_loss_pct=0,
            profit_factor=0, sharpe=0, max_drawdown_pct=0,
            total_return_pct=0, kelly_fraction=0,
        )

    pnls = [t.pnl_pct for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate   = len(wins) / n * 100
    avg_win    = float(np.mean(wins))   if wins   else 0.0
    avg_loss   = float(np.mean(losses)) if losses else 0.0
    gross_win  = sum(wins)             if wins   else 0.0
    gross_loss = abs(sum(losses))      if losses else 1e-9

    profit_factor = gross_win / gross_loss

    # Sharpe (annualised, ~250 trading days, using option-pct returns)
    if len(pnls) > 1:
        mu  = float(np.mean(pnls))
        sig = float(np.std(pnls))
        sharpe = (mu / sig) * math.sqrt(250) if sig > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown (peak-to-trough on cumulative P&L)
    cum = list(np.cumsum(pnls))
    peak = cum[0]
    max_dd = 0.0
    for c in cum:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd

    total_return = sum(pnls)

    # Kelly fraction: f = (win_rate/100) - (1 - win_rate/100) / (avg_win / abs(avg_loss))
    kelly = 0.0
    if avg_win > 0 and avg_loss < 0:
        w = win_rate / 100
        r = avg_win / abs(avg_loss)
        kelly = max(0.0, w - (1 - w) / r)
        kelly = min(kelly, 0.25)  # cap at 25% of capital

    return PeriodResult(
        label=label, start=start, end=end, n_trades=n,
        win_rate=round(win_rate, 1),
        avg_win_pct=round(avg_win, 2),
        avg_loss_pct=round(avg_loss, 2),
        profit_factor=round(profit_factor, 3),
        sharpe=round(sharpe, 3),
        max_drawdown_pct=round(max_dd, 2),
        total_return_pct=round(total_return, 2),
        kelly_fraction=round(kelly, 3),
        trades=trades,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Indicator helpers (stateless functions)
# ──────────────────────────────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def _supertrend_dir(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Returns +1 (uptrend) or -1 (downtrend) for each bar."""
    atr  = _atr(df, period)
    hl2  = (df["High"] + df["Low"]) / 2
    upb  = hl2 + mult * atr
    dnb  = hl2 - mult * atr
    dirs = pd.Series(0, index=df.index)
    for i in range(period + 1, len(df)):
        prev = dirs.iloc[i - 1]
        if df["Close"].iloc[i] > upb.iloc[i - 1]:
            dirs.iloc[i] = 1
        elif df["Close"].iloc[i] < dnb.iloc[i - 1]:
            dirs.iloc[i] = -1
        else:
            dirs.iloc[i] = prev
    return dirs


def _vwap_series(df: pd.DataFrame) -> pd.Series:
    tp  = (df["High"] + df["Low"] + df["Close"]) / 3
    vol = df.get("Volume", pd.Series(1, index=df.index))
    vol = vol.replace(0, 1)
    return (tp * vol).cumsum() / vol.cumsum()


def _trade_from_entry(
    df: pd.DataFrame,
    i: int,
    direction: str,  # "CE" or "PE"
    strategy: str,
    strength: float,
    sl_pct: float   = _SL_SPOT_PCT,
    tgt_pct: float  = _TGT_SPOT_PCT,
    max_bars: int   = 5,
) -> Trade:
    """
    Simulate a trade entered at bar i and exited when SL/target/time is hit.
    Returns a Trade object.  P&L is expressed in option-equivalent % (× leverage).
    """
    entry_price = df["Close"].iloc[i]
    entry_date  = df.index[i]
    sl_price    = entry_price * (1 - sl_pct / 100)  if direction == "CE" else entry_price * (1 + sl_pct / 100)
    tgt_price   = entry_price * (1 + tgt_pct / 100) if direction == "CE" else entry_price * (1 - tgt_pct / 100)

    exit_price  = entry_price
    exit_date   = entry_date
    exit_reason = "TIME"
    bars_held   = 0

    for j in range(i + 1, min(i + max_bars + 1, len(df))):
        bars_held = j - i
        row  = df.iloc[j]
        hi   = row["High"]
        lo   = row["Low"]
        exit_date = df.index[j]

        if direction == "CE":
            if lo <= sl_price:
                exit_price  = sl_price
                exit_reason = "SL"
                break
            if hi >= tgt_price:
                exit_price  = tgt_price
                exit_reason = "TARGET"
                break
        else:  # PE
            if hi >= sl_price:
                exit_price  = sl_price
                exit_reason = "SL"
                break
            if lo <= tgt_price:
                exit_price  = tgt_price
                exit_reason = "TARGET"
                break
        exit_price = row["Close"]

    # Spot move → option P&L with daily theta decay and entry slippage
    if direction == "CE":
        spot_pct = (exit_price - entry_price) / entry_price * 100
    else:
        spot_pct = (entry_price - exit_price) / entry_price * 100

    theta_cost = min(bars_held * _THETA_DAILY_PCT, 30.0)   # cap at 30%
    opt_pct = spot_pct * _LEVERAGE - _SLIPPAGE_ENTRY_PCT - theta_cost

    return Trade(
        strategy=strategy,
        direction=direction,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl_pct=round(opt_pct, 2),
        pnl_pts=round(exit_price - entry_price if direction == "CE" else entry_price - exit_price, 2),
        win=opt_pct > 0,
        exit_reason=exit_reason,
        strength=strength,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Strategy simulators
# ──────────────────────────────────────────────────────────────────────────────

class _TrendSim:
    """
    Replicates the live Trend strategy signal logic (7-indicator score).
    Operates on DAILY candles.
    """

    def build_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["sma20"]  = df["Close"].rolling(20).mean()
        df["sma50"]  = df["Close"].rolling(50).mean()
        df["ema9"]   = df["Close"].ewm(span=9,  adjust=False).mean()
        df["ema21"]  = df["Close"].ewm(span=21, adjust=False).mean()
        df["rsi"]    = _rsi(df["Close"], 14)
        exp12 = df["Close"].ewm(span=12, adjust=False).mean()
        exp26 = df["Close"].ewm(span=26, adjust=False).mean()
        df["macd"]       = exp12 - exp26
        df["macd_sig"]   = df["macd"].ewm(span=9, adjust=False).mean()
        tp = (df["High"] + df["Low"] + df["Close"]) / 3
        vol = df.get("Volume", pd.Series(1, index=df.index)).replace(0, 1)
        df["vwap"] = (tp * vol).cumsum() / vol.cumsum()
        df["st_dir"] = _supertrend_dir(df, 10, 3.0)
        df["adx"]    = self._adx(df, 14)
        return df

    @staticmethod
    def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        hi, lo, cl = df["High"], df["Low"], df["Close"]
        dm_plus  = (hi - hi.shift()).clip(lower=0)
        dm_minus = (lo.shift() - lo).clip(lower=0)
        tr = pd.concat([
            hi - lo,
            (hi - cl.shift()).abs(),
            (lo - cl.shift()).abs(),
        ], axis=1).max(axis=1)
        atr  = tr.ewm(alpha=1/period, adjust=False).mean()
        di_p = dm_plus .ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-9) * 100
        di_m = dm_minus.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-9) * 100
        dx   = (di_p - di_m).abs() / (di_p + di_m).replace(0, 1e-9) * 100
        return dx.ewm(alpha=1/period, adjust=False).mean()

    def signal(self, row: pd.Series) -> Tuple[Optional[str], float]:
        """Returns (direction, strength) or (None, 50)."""
        if pd.isna(row.get("sma50", float("nan"))):
            return None, 50.0

        bull = sum([
            row["sma20"]    > row["sma50"],
            row["ema9"]     > row["ema21"],
            row["rsi"]      < 55,
            row["macd"]     > row["macd_sig"],
            row["Close"]    > row["sma20"],
            row["Close"]    > row["vwap"],
            row["st_dir"]  == 1,
        ])
        bear = sum([
            row["sma20"]   <= row["sma50"],
            row["ema9"]    <= row["ema21"],
            row["rsi"]      > 45,
            row["macd"]    <= row["macd_sig"],
            row["Close"]   <= row["sma20"],
            row["Close"]   <= row["vwap"],
            row["st_dir"]  != 1,
        ])

        # ADX filter: only trade when market is trending (ADX > 25, matching live bot)
        adx = row.get("adx", 0)
        if pd.isna(adx) or adx < 25:
            return None, 50.0

        if bull >= 5:
            return "CE", round(bull / 7 * 100, 1)
        if bear >= 5:
            return "PE", round(bear / 7 * 100, 1)
        return None, 50.0

    def simulate(self, df: pd.DataFrame, start_i: int, end_i: int, vix_map: dict = None) -> List[Trade]:
        trades: List[Trade] = []
        in_trade = False
        skip_until = 0

        for i in range(max(start_i, 51), end_i):
            if i < skip_until:
                continue
            row = df.iloc[i]
            if in_trade:
                in_trade = False
                continue

            # VIX filter: skip entry on high-volatility days (mirrors live bot VIX guard)
            if vix_map is not None:
                bar_date = df.index[i].date() if hasattr(df.index[i], "date") else None
                if bar_date and vix_map.get(bar_date, 0) >= _VIX_BLOCK:
                    continue

            sig, strength = self.signal(row)
            if sig:
                t = _trade_from_entry(df, i, sig, "TREND", strength, max_bars=5)
                trades.append(t)
                in_trade = True
                skip_until = i + 2   # min 2-day cooldown between trades

        return trades


class _ORBSim:
    """
    Simulates ORB on 5-minute intraday data.

    Opening range: 09:15–09:30 IST (first 3 bars on 5m)
    Entry window : 09:30–11:30 IST
    Entry rule   : 2 consecutive closes above/below range with volume
    SL           : opposite range end
    Target       : entry ± range_width × 1.5
    Min range    : 75 pts NIFTY / 150 pts BANKNIFTY/SENSEX
    """

    def __init__(self, index: IndexConfig):
        self._idx = index
        self._min_range = 75 if index.name == "NIFTY" else 150

    def simulate(self, df: pd.DataFrame, vix_map: dict = None) -> List[Trade]:
        """df: 5-minute bars with timezone-aware index."""
        trades: List[Trade] = []

        # Group by trading date
        try:
            df = df.copy()
            # Ensure index is tz-aware (convert to IST)
            if df.index.tz is None:
                df.index = pd.to_datetime(df.index).tz_localize("UTC").tz_convert("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")
        except Exception:
            pass

        dates = sorted(set(df.index.date))

        for day in dates:
            # VIX filter: skip high-volatility days (mirrors live bot VIX guard)
            if vix_map is not None and vix_map.get(day, 0) >= _VIX_BLOCK:
                continue
            day_mask = df.index.date == day
            day_df   = df[day_mask]
            if len(day_df) < 6:
                continue

            # Building range: 09:15–09:30 (3 bars on 5m)
            range_bars = day_df[
                (day_df.index.time >= dtime(9, 15)) &
                (day_df.index.time <  dtime(9, 30))
            ]
            if len(range_bars) < 2:
                continue

            r_high = range_bars["High"].max()
            r_low  = range_bars["Low"].min()
            r_width = r_high - r_low

            if r_width < self._min_range:
                continue   # range too tight — ORB guard

            avg_vol = range_bars["Volume"].mean() if "Volume" in range_bars.columns else 1.0

            # Entry window: 09:30–11:30
            entry_bars = day_df[
                (day_df.index.time >= dtime(9, 30)) &
                (day_df.index.time <  dtime(11, 30))
            ]

            consec_up = consec_dn = 0
            fired = False

            for i, (ts, row) in enumerate(entry_bars.iterrows()):
                close  = row["Close"]
                vol    = row.get("Volume", avg_vol)
                vol_ok = vol > avg_vol * 1.2 if avg_vol > 0 else True

                if close > r_high:
                    consec_up += 1
                    consec_dn  = 0
                elif close < r_low:
                    consec_dn += 1
                    consec_up  = 0
                else:
                    consec_up = consec_dn = 0

                if consec_up >= 2 and vol_ok and not fired:
                    sl_price  = r_low
                    tgt_price = close + r_width * 1.5
                    strength  = min(95, 60 + consec_up * 10 + (10 if vol_ok else 0))
                    # Dynamic leverage: wider ORB range → more OTM option → lower leverage
                    range_pct = r_width / close * 100
                    lev = 12.0 if range_pct < 0.4 else (10.0 if range_pct < 0.8 else 8.0)
                    # FIX: hold for REST OF DAY, not just the entry window
                    rest_of_day = day_df[
                        (day_df.index > ts) &
                        (day_df.index.time <= dtime(15, 15))
                    ]
                    t = self._exit_trade(
                        entry_ts=ts, entry_price=close, direction="CE",
                        sl=sl_price, tgt=tgt_price, future=rest_of_day,
                        strength=strength, leverage=lev,
                    )
                    trades.append(t)
                    fired = True
                    break

                elif consec_dn >= 2 and vol_ok and not fired:
                    sl_price  = r_high
                    tgt_price = close - r_width * 1.5
                    strength  = min(95, 60 + consec_dn * 10 + (10 if vol_ok else 0))
                    range_pct = r_width / close * 100
                    lev = 12.0 if range_pct < 0.4 else (10.0 if range_pct < 0.8 else 8.0)
                    rest_of_day = day_df[
                        (day_df.index > ts) &
                        (day_df.index.time <= dtime(15, 15))
                    ]
                    t = self._exit_trade(
                        entry_ts=ts, entry_price=close, direction="PE",
                        sl=sl_price, tgt=tgt_price, future=rest_of_day,
                        strength=strength, leverage=lev,
                    )
                    trades.append(t)
                    fired = True
                    break

        return trades

    @staticmethod
    def _exit_trade(
        entry_ts, entry_price, direction,
        sl, tgt, future: pd.DataFrame, strength,
        leverage: float = _LEVERAGE,
    ) -> Trade:
        exit_price  = entry_price
        exit_ts     = entry_ts
        exit_reason = "CLOSE"   # end of day

        for ts, row in future.iterrows():
            hi, lo = row["High"], row["Low"]
            exit_ts = ts

            if direction == "CE":
                if lo <= sl:
                    exit_price, exit_reason = sl,  "SL";     break
                if hi >= tgt:
                    exit_price, exit_reason = tgt, "TARGET"; break
            else:
                if hi >= sl:
                    exit_price, exit_reason = sl,  "SL";     break
                if lo <= tgt:
                    exit_price, exit_reason = tgt, "TARGET"; break
            exit_price = row["Close"]

        if direction == "CE":
            spot_pct = (exit_price - entry_price) / entry_price * 100
        else:
            spot_pct = (entry_price - exit_price) / entry_price * 100

        # Apply entry slippage and proportional hourly theta cost
        hold_secs  = max((exit_ts - entry_ts).total_seconds(), 0) if exit_ts != entry_ts else 0
        hold_hours = hold_secs / 3600
        theta_cost = min(hold_hours * _THETA_HOURLY_PCT, 25.0)   # cap at 25%
        opt_pct = spot_pct * leverage - _SLIPPAGE_ENTRY_PCT - theta_cost

        return Trade(
            strategy="ORB",
            direction=direction,
            entry_date=entry_ts,
            exit_date=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=round(opt_pct, 2),
            pnl_pts=round(exit_price - entry_price if direction == "CE" else entry_price - exit_price, 2),
            win=opt_pct > 0,
            exit_reason=exit_reason,
            strength=strength,
        )


class _VWAPSim:
    """
    Simulates VWAP Mean Reversion on 5-minute intraday data.

    Entry: price deviates ≥ 0.6% from intraday VWAP + RSI confirmation (<38 / >62)
    Target: VWAP reversion
    SL: 0.4% further from VWAP (beyond the trigger level)
    Min confidence: 60% (RSI must be extreme or volume spike present)
    """

    _DEV_PCT    = 0.6
    _RSI_LONG   = 38
    _RSI_SHORT  = 62

    def simulate(self, df: pd.DataFrame, vix_map: dict = None) -> List[Trade]:
        trades: List[Trade] = []

        try:
            df = df.copy()
            if df.index.tz is None:
                df.index = pd.to_datetime(df.index).tz_localize("UTC").tz_convert("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")
        except Exception:
            pass

        dates = sorted(set(df.index.date))

        for day in dates:
            # VIX filter: skip high-volatility days (mirrors live bot VIX guard)
            if vix_map is not None and vix_map.get(day, 0) >= _VIX_BLOCK:
                continue
            day_mask = df.index.date == day
            day_df   = df[day_mask].copy()
            if len(day_df) < 10:
                continue

            # Entry window: 09:30–14:30
            window = day_df[
                (day_df.index.time >= dtime(9, 30)) &
                (day_df.index.time <  dtime(14, 30))
            ].copy()
            if len(window) < 5:
                continue

            # Compute intraday VWAP and RSI on this day's data
            window["vwap"] = _vwap_series(window)
            window["rsi"]  = _rsi(window["Close"], 14)
            avg_vol = window["Volume"].mean() if "Volume" in window.columns and window["Volume"].mean() > 0 else 1.0

            long_fired = short_fired = False

            for i, (ts, row) in enumerate(window.iterrows()):
                price = row["Close"]
                vwap  = row["vwap"]
                rsi   = row["rsi"]
                vol   = row.get("Volume", avg_vol)
                if pd.isna(vwap) or pd.isna(rsi) or vwap == 0:
                    continue

                dev_pct = (price - vwap) / vwap * 100

                # LONG: price below VWAP, RSI oversold
                if not long_fired and dev_pct <= -self._DEV_PCT and rsi < self._RSI_LONG:
                    vol_ok = vol > avg_vol * 1.2 if avg_vol > 0 else False
                    conf   = 60 + (15 if rsi < 30 else 0) + (15 if vol_ok else 0) + min(10, abs(dev_pct) * 5)
                    if conf >= 60:
                        tgt = vwap
                        sl  = price * (1 - 0.4 / 100)
                        future = window.iloc[i + 1:]
                        trades.append(self._exit(ts, price, "CE", sl, tgt, future, min(conf, 95)))
                        long_fired = True

                # SHORT: price above VWAP, RSI overbought
                elif not short_fired and dev_pct >= self._DEV_PCT and rsi > self._RSI_SHORT:
                    vol_ok = vol > avg_vol * 1.2 if avg_vol > 0 else False
                    conf   = 60 + (15 if rsi > 70 else 0) + (15 if vol_ok else 0) + min(10, abs(dev_pct) * 5)
                    if conf >= 60:
                        tgt = vwap
                        sl  = price * (1 + 0.4 / 100)
                        future = window.iloc[i + 1:]
                        trades.append(self._exit(ts, price, "PE", sl, tgt, future, min(conf, 95)))
                        short_fired = True

        return trades

    @staticmethod
    def _exit(entry_ts, entry_price, direction, sl, tgt, future, strength) -> Trade:
        exit_price, exit_ts, exit_reason = entry_price, entry_ts, "CLOSE"

        for ts, row in future.iterrows():
            hi, lo, cl = row["High"], row["Low"], row["Close"]
            exit_ts = ts

            if direction == "CE":
                if lo <= sl:
                    exit_price, exit_reason = sl,  "SL";     break
                if hi >= tgt:
                    exit_price, exit_reason = tgt, "TARGET"; break
            else:
                if hi >= sl:
                    exit_price, exit_reason = sl,  "SL";     break
                if lo <= tgt:
                    exit_price, exit_reason = tgt, "TARGET"; break
            exit_price = cl

        if direction == "CE":
            spot_pct = (exit_price - entry_price) / entry_price * 100
        else:
            spot_pct = (entry_price - exit_price) / entry_price * 100

        # Apply entry slippage and proportional hourly theta cost
        hold_secs  = max((exit_ts - entry_ts).total_seconds(), 0) if exit_ts != entry_ts else 0
        hold_hours = hold_secs / 3600
        theta_cost = min(hold_hours * _THETA_HOURLY_PCT, 15.0)   # VWAP trades are short — cap 15%
        opt_pct = spot_pct * _LEVERAGE - _SLIPPAGE_ENTRY_PCT - theta_cost

        return Trade(
            strategy="VWAP",
            direction=direction,
            entry_date=entry_ts,
            exit_date=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=round(opt_pct, 2),
            pnl_pts=round(exit_price - entry_price if direction == "CE" else entry_price - exit_price, 2),
            win=opt_pct > 0,
            exit_reason=exit_reason,
            strength=strength,
        )


class _GapSim:
    """
    Simulates Gap strategy on DAILY candles.

    Entry: opening gap (today open vs yesterday close)

    Enhanced logic (Phase 2):
      EXHAUSTION gap (>2.5%)  → simulate GAP_FADE  (bet on gap fill)
      RUNAWAY gap   (1.5-2.5%) → simulate GAP_CONTINUATION
      BREAKAWAY gap (0.8-1.5%) → simulate GAP_CONTINUATION
      COMMON gap    (<0.8%)    → skip (too unreliable)

    Legacy fallback (when use_enhanced=False):
      Strong gap (>1.5%) → enter CE/PE at open
      Moderate gap       → skip

    Metrics returned (in addition to Trade list):
      fill_statistics — detect_all_gaps() + calculate_gap_fill_stats()
    """

    _MIN_GAP     = 0.75
    _STRONG_GAP  = 1.50
    _COMMON_THR  = 0.3
    _BREAKAWAY   = 0.8
    _RUNAWAY     = 1.5
    _EXHAUSTION  = 2.5

    def _classify(self, abs_pct: float) -> str:
        if abs_pct < self._COMMON_THR:
            return "NONE"
        if abs_pct < self._BREAKAWAY:
            return "COMMON"
        if abs_pct < self._RUNAWAY:
            return "BREAKAWAY"
        if abs_pct < self._EXHAUSTION:
            return "RUNAWAY"
        return "EXHAUSTION"

    def detect_all_gaps(self, df: pd.DataFrame) -> List[dict]:
        """Scan daily data for all gaps with metadata."""
        gaps = []
        df   = df.copy()
        df["prev_close"] = df["Close"].shift(1)
        for i in range(1, len(df)):
            row     = df.iloc[i]
            prev_cl = row["prev_close"]
            open_p  = row["Open"]
            if pd.isna(prev_cl) or prev_cl == 0:
                continue
            gap_pct = (open_p - prev_cl) / prev_cl * 100
            if abs(gap_pct) < self._COMMON_THR:
                continue
            cat = self._classify(abs(gap_pct))
            gaps.append({
                "date":          df.index[i],
                "prev_close":    float(prev_cl),
                "open_price":    float(open_p),
                "gap_pct":       round(float(gap_pct), 4),
                "direction":     "UP" if gap_pct > 0 else "DOWN",
                "category":      cat,
                "filled_same_day": self._check_fill(df.iloc[i], prev_cl, gap_pct),
            })
        return gaps

    def _check_fill(self, row: "pd.Series", prev_close: float, gap_pct: float) -> bool:
        """Approximate: gap filled if same-day high/low touched prev_close range."""
        if gap_pct > 0:    # gap-up → filled if Low <= prev_close
            return float(row["Low"]) <= prev_close
        else:              # gap-down → filled if High >= prev_close
            return float(row["High"]) >= prev_close

    def calculate_gap_fill_stats(self, gaps: List[dict]) -> dict:
        """Aggregate gap fill statistics by category."""
        if not gaps:
            return {"total_gaps": 0, "filled_same_day": 0,
                    "fill_rate_pct": 0.0, "by_type": {}}
        filled    = sum(1 for g in gaps if g.get("filled_same_day"))
        by_type: dict = {}
        cats = set(g["category"] for g in gaps)
        for cat in cats:
            cat_gaps  = [g for g in gaps if g["category"] == cat]
            cat_fill  = sum(1 for g in cat_gaps if g.get("filled_same_day"))
            by_type[cat] = {
                "count":     len(cat_gaps),
                "fill_rate": round(cat_fill / len(cat_gaps) * 100, 1) if cat_gaps else 0.0,
            }
        return {
            "total_gaps":     len(gaps),
            "filled_same_day": filled,
            "fill_rate_pct":  round(filled / len(gaps) * 100, 1),
            "by_type":        by_type,
        }

    def simulate(self, df: pd.DataFrame, vix_map: dict = None) -> List[Trade]:
        """Simulate gap trades — enhanced (fade/continuation) + legacy path."""
        trades: List[Trade] = []
        df = df.copy()
        df["prev_close"] = df["Close"].shift(1)

        for i in range(1, len(df)):
            row       = df.iloc[i]
            prev_cl   = row["prev_close"]
            open_p    = row["Open"]

            if pd.isna(prev_cl) or prev_cl == 0:
                continue

            # VIX filter: skip entry on high-volatility days (mirrors live bot VIX guard)
            if vix_map is not None:
                bar_date = df.index[i].date() if hasattr(df.index[i], "date") else None
                if bar_date and vix_map.get(bar_date, 0) >= _VIX_BLOCK:
                    continue

            gap_pct = (open_p - prev_cl) / prev_cl * 100
            abs_pct = abs(gap_pct)
            cat     = self._classify(abs_pct)

            if cat in ("NONE", "COMMON"):
                continue   # too unreliable

            # Decide strategy based on category
            if cat == "EXHAUSTION":
                # GAP_FADE — bet that gap will fill (reverse direction)
                direction = "PE" if gap_pct > 0 else "CE"
                strategy  = "GAP_FADE"
                # Fade SL = 0.3% beyond open (tight — if it extends it's not exhaustion)
                sl_pct   = _SL_SPOT_PCT * 0.8
                tgt_pct  = _TGT_SPOT_PCT * 1.5
            else:
                # GAP_CONTINUATION — BREAKAWAY / RUNAWAY
                direction = "CE" if gap_pct > 0 else "PE"
                strategy  = "GAP_CONTINUATION"
                sl_pct    = _SL_SPOT_PCT * 1.5   # wider SL (gap volatility)
                tgt_pct   = _TGT_SPOT_PCT * 2.0  # bigger target

            strength = min(95, 60 + abs_pct * 10)

            future = df.iloc[i:]
            t = _trade_from_entry(
                df=future.reset_index(drop=False).rename(columns={"index": "_dt"}),
                i=0,
                direction=direction,
                strategy=strategy,
                strength=strength,
                sl_pct=sl_pct,
                tgt_pct=tgt_pct,
                max_bars=3,
            )
            # Re-attach original date
            t.entry_date = df.index[i]
            trades.append(t)

        return trades


class _EODSim:
    """
    Simulates EOD Closing Momentum on 15-minute candles.

    Entry: 14:30 candle body ≥ 50% of high-low range, trend-confirmed
    Exit: next day open (or 15:27 IST close)
    """

    _BODY_MIN_PCT = 0.50

    def simulate(self, df: pd.DataFrame, vix_map: dict = None) -> List[Trade]:
        trades: List[Trade] = []

        try:
            df = df.copy()
            if df.index.tz is None:
                df.index = pd.to_datetime(df.index).tz_localize("UTC").tz_convert("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")
        except Exception:
            pass

        dates = sorted(set(df.index.date))

        for day in dates:
            # VIX filter: skip high-volatility days (mirrors live bot VIX guard)
            if vix_map is not None and vix_map.get(day, 0) >= _VIX_BLOCK:
                continue
            day_mask = df.index.date == day
            day_df   = df[day_mask]

            # Find the 14:30 candle
            candle_14_30 = day_df[day_df.index.time == dtime(14, 30)]
            if candle_14_30.empty:
                # Fallback: use the last candle before 15:00
                before_15 = day_df[day_df.index.time < dtime(15, 0)]
                if before_15.empty:
                    continue
                candle_14_30 = before_15.iloc[[-1]]

            row    = candle_14_30.iloc[0]
            hi, lo = row["High"], row["Low"]
            op, cl = row["Open"], row["Close"]
            rng    = hi - lo

            if rng < 1e-6:
                continue

            body_pct = abs(cl - op) / rng
            if body_pct < self._BODY_MIN_PCT:
                continue   # doji / indecision — EOD guard

            direction = "CE" if cl > op else "PE"

            # Trend alignment: use price vs intraday VWAP as proxy
            day_vwap = _vwap_series(day_df).iloc[-1]
            if direction == "CE" and cl < day_vwap * 0.998:
                continue  # counter-trend block
            if direction == "PE" and cl > day_vwap * 1.002:
                continue

            # Entry at close of 14:30 candle; exit at EOD (15:27)
            bars_after = day_df[day_df.index.time > candle_14_30.index[-1].time()]
            entry_price = cl
            entry_ts    = candle_14_30.index[-1]
            exit_price  = bars_after["Close"].iloc[-1] if not bars_after.empty else cl
            exit_ts     = bars_after.index[-1]         if not bars_after.empty else entry_ts

            if direction == "CE":
                spot_pct = (exit_price - entry_price) / entry_price * 100
            else:
                spot_pct = (entry_price - exit_price) / entry_price * 100

            # Apply entry slippage and ~45-60 min intraday theta cost
            hold_secs  = max((exit_ts - entry_ts).total_seconds(), 0) if exit_ts != entry_ts else 0
            hold_hours = hold_secs / 3600
            theta_cost = min(hold_hours * _THETA_HOURLY_PCT, 10.0)   # cap at 10%
            opt_pct  = spot_pct * _LEVERAGE - _SLIPPAGE_ENTRY_PCT - theta_cost
            strength = 60 + body_pct * 30

            trades.append(Trade(
                strategy="EOD",
                direction=direction,
                entry_date=entry_ts,
                exit_date=exit_ts,
                entry_price=round(entry_price, 2),
                exit_price=round(exit_price, 2),
                pnl_pct=round(opt_pct, 2),
                pnl_pts=round(exit_price - entry_price if direction == "CE" else entry_price - exit_price, 2),
                win=opt_pct > 0,
                exit_reason="CLOSE",
                strength=round(strength, 1),
            ))

        return trades


# ──────────────────────────────────────────────────────────────────────────────
# Walk-Forward engine
# ──────────────────────────────────────────────────────────────────────────────

def _walk_forward_trend(df: pd.DataFrame, sim: _TrendSim, vix_map: dict = None) -> WalkForwardResult:
    """
    Rolling walk-forward on daily data.
    Train window: 3 months (~63 bars)
    Test  window: 1 month  (~21 bars)
    Step by 1 month each fold.
    """
    df = df.copy()
    n  = len(df)
    train_bars = 63
    test_bars  = 21
    folds = []

    i = 51  # need 50+ bars for indicators to warm up
    while i + train_bars + test_bars <= n:
        train_end = i + train_bars
        test_end  = train_end + test_bars

        train_trades = sim.simulate(df, i, train_end, vix_map=vix_map)
        test_trades  = sim.simulate(df, train_end, test_end, vix_map=vix_map)

        t_start = str(df.index[i])[:10]
        t_end   = str(df.index[train_end - 1])[:10]
        s_start = str(df.index[train_end])[:10]
        s_end   = str(df.index[test_end - 1])[:10]

        in_sample  = _calc_stats(train_trades, f"Train {t_start}→{t_end}", t_start, t_end)
        out_sample = _calc_stats(test_trades,  f"Test  {s_start}→{s_end}", s_start, s_end)
        folds.append((in_sample, out_sample))

        i += test_bars  # roll forward by test window

    if not folds:
        return WalkForwardResult(
            strategy="TREND", folds=[], avg_train_wr=0, avg_test_wr=0,
            overfit_detected=False, overfit_gap=0
        )

    # Soft threshold: need ≥5 trades on BOTH sides of a fold to count
    # (prevent 0-trade folds dragging the average to artificially low, misleadingly stable values)
    paired_folds = [(tr, te) for tr, te in folds if tr.n_trades >= 5 and te.n_trades >= 5]
    if len(paired_folds) < 2:
        # Not enough paired data — report SPARSE rather than a meaningless number
        return WalkForwardResult(
            strategy="TREND", folds=folds,
            avg_train_wr=-1, avg_test_wr=-1,
            overfit_detected=False, overfit_gap=0,
        )

    avg_train = float(np.mean([tr.win_rate for tr, te in paired_folds]))
    avg_test  = float(np.mean([te.win_rate for tr, te in paired_folds]))
    gap       = avg_train - avg_test
    overfit   = gap > _OVERFIT_THRESHOLD

    return WalkForwardResult(
        strategy="TREND",
        folds=folds,
        avg_train_wr=round(avg_train, 1),
        avg_test_wr =round(avg_test,  1),
        overfit_detected=overfit,
        overfit_gap=round(gap, 1),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main validator
# ──────────────────────────────────────────────────────────────────────────────

class StrategyValidator:
    """
    Runs all five strategy simulators and produces a ValidationReport.

    Example usage:
        from bot.strategy_validator import StrategyValidator
        report = StrategyValidator(index=BANKNIFTY).run_all()
        print(report.summary())
    """

    def __init__(self, index: IndexConfig = None, full: bool = False):
        self._idx  = index or NIFTY
        self._full = full   # True = use maximum available history

    # ─────────────────────────────────────────────
    # Data fetchers
    # ─────────────────────────────────────────────

    def _fetch_daily(self) -> pd.DataFrame:
        period = "2y" if self._full else "1y"
        logger.info(f"Fetching {self._idx.display_name} daily data ({period})…")
        df = yf.Ticker(self._idx.yahoo_symbol).history(period=period, interval="1d")
        if df.empty:
            raise RuntimeError(f"No daily data returned for {self._idx.yahoo_symbol}")
        return df

    def _fetch_5m(self) -> pd.DataFrame:
        logger.info(f"Fetching {self._idx.display_name} 5m intraday data (60d)…")
        df = yf.Ticker(self._idx.yahoo_symbol).history(period="60d", interval="5m")
        if df.empty:
            raise RuntimeError(f"No 5m data returned for {self._idx.yahoo_symbol}")
        return df

    def _fetch_15m(self) -> pd.DataFrame:
        logger.info(f"Fetching {self._idx.display_name} 15m intraday data (60d)…")
        df = yf.Ticker(self._idx.yahoo_symbol).history(period="60d", interval="15m")
        if df.empty:
            raise RuntimeError(f"No 15m data returned for {self._idx.yahoo_symbol}")
        return df

    def _fetch_vix(self) -> dict:
        """Fetch India VIX close prices keyed by date. Returns {} on failure."""
        period = "2y" if self._full else "1y"
        logger.info("Fetching India VIX for live-filter replication…")
        try:
            vix_df = yf.Ticker("^INDIAVIX").history(period=period, interval="1d")
            if vix_df.empty:
                logger.warning("No India VIX data — VIX filter disabled in validator")
                return {}
            result: dict = {}
            for dt, row in vix_df.iterrows():
                d = dt.date() if hasattr(dt, "date") else dt
                result[d] = float(row["Close"])
            lo, hi = min(result.values()), max(result.values())
            logger.info(f"VIX loaded: {len(result)} days, range {lo:.1f}–{hi:.1f}")
            return result
        except Exception as e:
            logger.warning(f"VIX fetch failed ({e}) — VIX filter disabled")
            return {}

    # ─────────────────────────────────────────────
    # Per-strategy runners
    # ─────────────────────────────────────────────

    def _validate_trend(self, daily: pd.DataFrame, vix_map: dict = None) -> StrategyReport:
        logger.info("Validating TREND strategy…")
        sim = _TrendSim()
        df  = sim.build_indicators(daily)

        # Full-period stats
        all_trades = sim.simulate(df, 0, len(df), vix_map=vix_map)
        start = str(df.index[0])[:10]
        end   = str(df.index[-1])[:10]
        full  = _calc_stats(all_trades, f"Full {start}→{end}", start, end)

        # Walk-forward overfit detection
        wf = _walk_forward_trend(df, sim, vix_map=vix_map)

        # Recommendation
        verdict = full.verdict()
        overfit = wf.overfit_detected

        if verdict == "PASS" and not overfit:
            recommended = True
            note = f"Profitable and NOT overfit. ✅"
        elif overfit:
            recommended = False
            note = (
                f"OVERFIT detected (gap={wf.overfit_gap:.1f} pts). "
                f"Live win-rate likely ≈ {wf.avg_test_wr:.1f}%. Reduce to 5 key indicators."
            )
        elif verdict == "MARGINAL":
            recommended = True
            note = f"Marginal — monitor first 20 live trades before scaling."
        else:
            recommended = False
            note = f"Underperforming. win_rate={full.win_rate:.1f}%, PF={full.profit_factor:.2f}."

        return StrategyReport(
            strategy="TREND", index=self._idx.name,
            full_period=full, walk_forward=wf,
            note=note, recommended=recommended,
        )

    def _validate_orb(self, df_5m: pd.DataFrame, vix_map: dict = None) -> StrategyReport:
        logger.info("Validating ORB strategy…")
        sim    = _ORBSim(self._idx)
        trades = sim.simulate(df_5m, vix_map=vix_map)

        dates  = sorted(set(df_5m.index.date)) if hasattr(df_5m.index, "date") else []
        start  = str(dates[0])  if dates else "?"
        end    = str(dates[-1]) if dates else "?"
        full   = _calc_stats(trades, f"Full {start}→{end}", start, end)
        verdict = full.verdict()

        if verdict == "PASS":
            note = f"Strong breakout strategy. ✅"
            rec  = True
        elif verdict == "MARGINAL":
            note = f"Marginal on last 60 days. Keep enabled but track live results."
            rec  = True
        elif not full.is_valid():
            note = f"Only {full.n_trades} trades in 60-day window — insufficient data."
            rec  = True   # default to enabled for real session
        else:
            note = f"Underperforming on recent data. Check ORB min-range / time-decay settings."
            rec  = False

        return StrategyReport(
            strategy="ORB", index=self._idx.name,
            full_period=full, walk_forward=None,
            note=note, recommended=rec,
        )

    def _validate_vwap(self, df_5m: pd.DataFrame, vix_map: dict = None) -> StrategyReport:
        logger.info("Validating VWAP strategy…")
        sim    = _VWAPSim()
        trades = sim.simulate(df_5m, vix_map=vix_map)

        dates  = sorted(set(df_5m.index.date)) if hasattr(df_5m.index, "date") else []
        start  = str(dates[0])  if dates else "?"
        end    = str(dates[-1]) if dates else "?"
        full   = _calc_stats(trades, f"Full {start}→{end}", start, end)
        verdict = full.verdict()

        note = {
            "PASS":              "Best-performing strategy historically. ✅",
            "MARGINAL":          "Marginal — confirm deviation and RSI thresholds still appropriate.",
            "FAIL":              "Below minimum. Check VWAP_DEVIATION_PCT and RSI thresholds.",
            "INSUFFICIENT_DATA": f"Only {full.n_trades} trades — RANGING regime may have been rare lately.",
        }.get(verdict, "")

        rec = verdict in ("PASS", "MARGINAL", "INSUFFICIENT_DATA")

        return StrategyReport(
            strategy="VWAP", index=self._idx.name,
            full_period=full, walk_forward=None,
            note=note, recommended=rec,
        )

    def _validate_gap(self, daily: pd.DataFrame, vix_map: dict = None) -> StrategyReport:
        logger.info("Validating GAP strategy…")
        sim    = _GapSim()
        trades = sim.simulate(daily, vix_map=vix_map)

        # Enhanced: split fade vs continuation metrics
        fade_trades  = [t for t in trades if t.strategy == "GAP_FADE"]
        cont_trades  = [t for t in trades if t.strategy == "GAP_CONTINUATION"]

        start  = str(daily.index[0])[:10]
        end    = str(daily.index[-1])[:10]
        full   = _calc_stats(trades, f"Full {start}→{end}", start, end)
        verdict = full.verdict()

        # Gap fill statistics
        all_gaps  = sim.detect_all_gaps(daily)
        fill_stat = sim.calculate_gap_fill_stats(all_gaps)

        fade_full  = _calc_stats(fade_trades, "GAP_FADE",         start, end)
        cont_full  = _calc_stats(cont_trades, "GAP_CONTINUATION", start, end)

        note_parts = []
        if fill_stat["total_gaps"] > 0:
            note_parts.append(
                f"{fill_stat['total_gaps']} gaps detected  |  "
                f"fill_rate={fill_stat['fill_rate_pct']:.0f}%"
            )
            by_type = fill_stat.get("by_type", {})
            for cat, st in by_type.items():
                if st["count"] >= 2:
                    note_parts.append(
                        f"  {cat}: n={st['count']} fill_rate={st['fill_rate']:.0f}%"
                    )

        if fade_full.n_trades >= 3:
            note_parts.append(
                f"FADE ({fade_full.n_trades} trades): WR={fade_full.win_rate:.0f}%  "
                f"avg_win={fade_full.avg_win_pct:.1f}%  avg_loss={fade_full.avg_loss_pct:.1f}%"
            )
        if cont_full.n_trades >= 3:
            note_parts.append(
                f"CONTINUATION ({cont_full.n_trades} trades): WR={cont_full.win_rate:.0f}%  "
                f"avg_win={cont_full.avg_win_pct:.1f}%  avg_loss={cont_full.avg_loss_pct:.1f}%"
            )

        verdict_note = {
            "PASS":              "Gap trading working well. ✅",
            "MARGINAL":          "Marginal. Review fade vs continuation split above.",
            "FAIL":              "Weak performance. Check if gap threshold is too aggressive.",
            "INSUFFICIENT_DATA": f"Only {full.n_trades} gap trades — rare event in this window.",
        }.get(verdict, "")
        note_parts.append(verdict_note)

        rec = verdict in ("PASS", "MARGINAL", "INSUFFICIENT_DATA")

        return StrategyReport(
            strategy="GAP", index=self._idx.name,
            full_period=full, walk_forward=None,
            note="  |  ".join(note_parts), recommended=rec,
        )

    def _validate_eod(self, df_15m: pd.DataFrame, vix_map: dict = None) -> StrategyReport:
        logger.info("Validating EOD strategy…")
        sim    = _EODSim()
        trades = sim.simulate(df_15m, vix_map=vix_map)

        dates  = sorted(set(df_15m.index.date)) if hasattr(df_15m.index, "date") else []
        start  = str(dates[0])  if dates else "?"
        end    = str(dates[-1]) if dates else "?"
        full   = _calc_stats(trades, f"Full {start}→{end}", start, end)
        verdict = full.verdict()

        if not full.is_valid():
            note = (
                f"Only {full.n_trades} trades in 60-day window. "
                f"Keep EOD_ENABLED=false until live _eod_tracker shows ≥ 20 trades."
            )
            rec = False
        elif verdict == "PASS":
            note = "EOD valid on recent data. Enable cautiously — thin-market slippage not captured here. ✅"
            rec  = True
        elif verdict == "MARGINAL":
            note = "Marginal. EOD slippage (2:30–3 PM) will likely push live result below backtest. Keep disabled."
            rec  = False
        else:
            note = "Underperforming even before accounting for real EOD slippage. Keep EOD_ENABLED=false."
            rec  = False

        return StrategyReport(
            strategy="EOD", index=self._idx.name,
            full_period=full, walk_forward=None,
            note=note, recommended=rec,
        )

    # ─────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────

    def run_all(self) -> ValidationReport:
        """Fetch data and validate all five strategies. Returns ValidationReport."""
        logger.info(f"=== Strategy Validator starting — index: {self._idx.display_name} ===")

        # Fetch data (shared between strategies where possible)
        try:
            daily = self._fetch_daily()
        except Exception as e:
            logger.error(f"Daily data fetch failed: {e}")
            daily = pd.DataFrame()

        try:
            df_5m = self._fetch_5m()
        except Exception as e:
            logger.error(f"5m data fetch failed: {e}")
            df_5m = pd.DataFrame()

        try:
            df_15m = self._fetch_15m()
        except Exception as e:
            logger.error(f"15m data fetch failed: {e}")
            df_15m = pd.DataFrame()

        reports: List[StrategyReport] = []

        # Fetch India VIX to replicate live bot’s VIX ≥ 20 entry block
        vix_map = self._fetch_vix()

        # Run each validator; catch individual failures so others still run
        def _run(fn, *args, name=""):
            try:
                return fn(*args)
            except Exception as e:
                logger.error(f"{name} validation failed: {e}")
                empty = _calc_stats([], name, "?", "?")
                return StrategyReport(
                    strategy=name, index=self._idx.name,
                    full_period=empty, walk_forward=None,
                    note=f"Error: {e}", recommended=False,
                )

        if not daily.empty:
            reports.append(_run(self._validate_trend, daily,  vix_map, name="TREND"))
            reports.append(_run(self._validate_gap,   daily,  vix_map, name="GAP"))
        if not df_5m.empty:
            reports.append(_run(self._validate_orb,   df_5m,  vix_map, name="ORB"))
            reports.append(_run(self._validate_vwap,  df_5m,  vix_map, name="VWAP"))
        if not df_15m.empty:
            reports.append(_run(self._validate_eod,   df_15m, vix_map, name="EOD"))

        return ValidationReport(
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            index=self._idx.display_name,
            strategies=reports,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Detailed per-fold formatter
# ──────────────────────────────────────────────────────────────────────────────

def _print_walk_forward_detail(wf: WalkForwardResult):
    print(f"\n  Walk-Forward detail — {wf.strategy}")
    print(f"  {'Fold':<6}  {'Train window':<22}  {'Train WR':>8}  {'Test window':<22}  {'Test WR':>8}  {'Gap':>6}")
    print("  " + "─" * 82)
    for n, (tr, te) in enumerate(wf.folds, 1):
        twr = f"{tr.win_rate:.1f}%" if tr.is_valid() else "  few"
        swr = f"{te.win_rate:.1f}%" if te.is_valid() else "  few"
        gap = f"{tr.win_rate - te.win_rate:+.1f}" if (tr.is_valid() and te.is_valid()) else "  —"
        print(f"  {n:<6}  {tr.start}–{tr.end:<10}  {twr:>8}  {te.start}–{te.end:<10}  {swr:>8}  {gap:>6}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def _main():
    parser = argparse.ArgumentParser(
        description="NIFTY Options Bot — Strategy Validator",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--index", default="NIFTY",
        help="Index to validate: NIFTY (default), BANKNIFTY, SENSEX"
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Use maximum available history (2 years daily; default = 1 year)"
    )
    parser.add_argument(
        "--json", metavar="FILE",
        help="Save full results to a JSON file (e.g. validation.json)"
    )
    parser.add_argument(
        "--wf-detail", action="store_true",
        help="Print walk-forward fold-by-fold detail for Trend strategy"
    )
    args = parser.parse_args()

    idx = get_index(args.index)
    if not idx:
        print(f"Unknown index '{args.index}'. Choose NIFTY, BANKNIFTY, or SENSEX.")
        sys.exit(1)

    validator = StrategyValidator(index=idx, full=args.full)
    report    = validator.run_all()

    # Print main summary
    print(report.summary())

    # Optionally print walk-forward detail
    if args.wf_detail:
        for rpt in report.strategies:
            if rpt.walk_forward and rpt.walk_forward.folds:
                _print_walk_forward_detail(rpt.walk_forward)

    # Optionally save JSON
    if args.json:
        try:
            with open(args.json, "w") as f:
                json.dump(report.to_dict(), f, indent=2, default=str)
            print(f"Results saved to {args.json}")
        except Exception as e:
            print(f"Could not save JSON: {e}")


if __name__ == "__main__":
    _main()
