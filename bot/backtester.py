"""
Strategy Backtester

Tests the bot's trend-following signals against historical NIFTY data
to calculate actual win rate, average P&L, max drawdown, and Sharpe ratio.

Usage via chat:
    backtest              - Run default 3-month backtest
    backtest 6m           - Run 6-month backtest
    backtest 1y           - Run 1-year backtest
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from loguru import logger

from bot.index_config import IndexConfig, NIFTY

import sys
sys.path.append('..')
from config import settings as _cfg


@dataclass
class BacktestTrade:
    entry_date: datetime
    exit_date: datetime
    direction: str  # CE or PE
    entry_price: float
    exit_price: float
    pnl_points: float
    pnl_pct: float
    holding_bars: int
    signal_strength: float


@dataclass
class BacktestResult:
    period: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_points: float
    avg_loss_points: float
    total_pnl_points: float
    max_drawdown_points: float
    profit_factor: float
    sharpe_ratio: float
    avg_holding_bars: float
    best_trade: float
    worst_trade: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    monthly_breakdown: Dict[str, Dict]
    trades: List[BacktestTrade] = field(default_factory=list)


class StrategyBacktester:
    """
    Backtests the trend-following logic against historical data
    for the active index to produce honest win/loss stats.
    """

    def __init__(self):
        self._index: IndexConfig = NIFTY
        self._sma_short = 20
        self._sma_long = 50
        self._rsi_period = 14
        self._rsi_oversold = 30
        self._rsi_overbought = 70
        # Mirror live config ratio: option SL%/Target% scaled to spot moves
        # Options move ~15x spot on average; divide by 15 to get equivalent spot %
        scale = 15.0
        self._sl_pct = _cfg.trading.stop_loss_percentage / scale      # e.g. 15/15 = 1.0%
        self._target_pct = _cfg.trading.target_percentage / scale      # e.g. 30/15 = 2.0%
        self._min_strength = 62

    def set_index(self, idx: IndexConfig):
        self._index = idx

    def run(self, period: str = "3mo") -> Optional[BacktestResult]:
        """Run backtest over the specified period."""
        logger.info(f"Running backtest for {self._index.display_name} {period}")

        try:
            ticker = yf.Ticker(self._index.yahoo_symbol)
            data = ticker.history(period=period, interval="1d")
        except Exception as e:
            logger.error(f"Failed to fetch data: {e}")
            return None

        if data.empty or len(data) < self._sma_long + 10:
            logger.warning("Insufficient data for backtest")
            return None

        data = self._compute_indicators(data)
        trades = self._simulate_trades(data)

        return self._compile_results(trades, period)

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["sma_short"] = df["Close"].rolling(self._sma_short).mean()
        df["sma_long"] = df["Close"].rolling(self._sma_long).mean()

        # EMA
        df["ema_9"] = df["Close"].ewm(span=9, adjust=False).mean()
        df["ema_21"] = df["Close"].ewm(span=21, adjust=False).mean()

        # RSI
        delta = df["Close"].diff()
        gain = delta.where(delta > 0, 0).rolling(self._rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self._rsi_period).mean()
        rs = gain / loss
        df["rsi"] = 100 - (100 / (1 + rs))

        # MACD
        exp_fast = df["Close"].ewm(span=12, adjust=False).mean()
        exp_slow = df["Close"].ewm(span=26, adjust=False).mean()
        df["macd"] = exp_fast - exp_slow
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

        # VWAP proxy
        tp = (df["High"] + df["Low"] + df["Close"]) / 3
        if "Volume" in df.columns and df["Volume"].sum() > 0:
            df["vwap"] = (tp * df["Volume"]).cumsum() / df["Volume"].cumsum()
        else:
            df["vwap"] = tp.rolling(20).mean()

        # Supertrend (simplified)
        hl2 = (df["High"] + df["Low"]) / 2
        tr = pd.concat([
            df["High"] - df["Low"],
            abs(df["High"] - df["Close"].shift()),
            abs(df["Low"] - df["Close"].shift())
        ], axis=1).max(axis=1)
        atr = tr.rolling(10).mean()
        df["st_upper"] = hl2 + 3 * atr
        df["st_lower"] = hl2 - 3 * atr
        df["st_dir"] = 0
        for i in range(11, len(df)):
            if df["Close"].iloc[i] > df["st_upper"].iloc[i - 1]:
                df.iloc[i, df.columns.get_loc("st_dir")] = 1
            elif df["Close"].iloc[i] < df["st_lower"].iloc[i - 1]:
                df.iloc[i, df.columns.get_loc("st_dir")] = -1
            else:
                df.iloc[i, df.columns.get_loc("st_dir")] = df["st_dir"].iloc[i - 1]

        return df

    def _compute_signal(self, row) -> Tuple[Optional[str], float]:
        """Replicate the bot's signal logic. Returns (direction, strength)."""
        bullish = sum([
            row["sma_short"] > row["sma_long"],
            row["ema_9"] > row["ema_21"],
            row["rsi"] < self._rsi_oversold,
            row["macd"] > row["macd_signal"],
            row["Close"] > row["sma_short"],
            row["Close"] > row["vwap"],
            row["st_dir"] == 1,
        ])
        bearish = sum([
            row["sma_short"] <= row["sma_long"],
            row["ema_9"] <= row["ema_21"],
            row["rsi"] > self._rsi_overbought,
            row["macd"] <= row["macd_signal"],
            row["Close"] <= row["sma_short"],
            row["Close"] <= row["vwap"],
            row["st_dir"] != 1,
        ])

        if bullish >= 5 and row["rsi"] <= self._rsi_overbought:
            strength = min(int(bullish / 7 * 100), 100)
            return ("CE", strength)
        elif bearish >= 5 and row["rsi"] >= self._rsi_oversold:
            strength = min(int(bearish / 7 * 100), 100)
            return ("PE", strength)
        return (None, 50)

    def _simulate_trades(self, df: pd.DataFrame) -> List[BacktestTrade]:
        trades = []
        in_trade = False
        entry_price = 0.0
        entry_date = None
        direction = None
        entry_strength = 0.0
        bars_held = 0

        for i in range(self._sma_long + 1, len(df)):
            row = df.iloc[i]

            if pd.isna(row["sma_long"]) or pd.isna(row["rsi"]):
                continue

            if not in_trade:
                sig, strength = self._compute_signal(row)
                if sig and strength >= self._min_strength:
                    in_trade = True
                    direction = sig
                    entry_price = row["Close"]
                    entry_date = row.name
                    entry_strength = strength
                    bars_held = 0
            else:
                bars_held += 1
                price = row["Close"]

                if direction == "CE":
                    pnl_pct = (price - entry_price) / entry_price * 100
                else:
                    pnl_pct = (entry_price - price) / entry_price * 100

                hit_target = pnl_pct >= self._target_pct
                hit_sl = pnl_pct <= -self._sl_pct
                signal_flipped, _ = self._compute_signal(row)
                exit_signal = (signal_flipped is not None and signal_flipped != direction)
                max_hold = bars_held >= 5

                if hit_target or hit_sl or exit_signal or max_hold:
                    trades.append(BacktestTrade(
                        entry_date=entry_date,
                        exit_date=row.name,
                        direction=direction,
                        entry_price=entry_price,
                        exit_price=price,
                        pnl_points=price - entry_price if direction == "CE" else entry_price - price,
                        pnl_pct=round(pnl_pct, 2),
                        holding_bars=bars_held,
                        signal_strength=entry_strength,
                    ))
                    in_trade = False

        return trades

    def _compile_results(self, trades: List[BacktestTrade], period: str) -> BacktestResult:
        if not trades:
            return BacktestResult(
                period=period, total_trades=0, winning_trades=0, losing_trades=0,
                win_rate=0, avg_win_points=0, avg_loss_points=0, total_pnl_points=0,
                max_drawdown_points=0, profit_factor=0, sharpe_ratio=0,
                avg_holding_bars=0, best_trade=0, worst_trade=0,
                max_consecutive_wins=0, max_consecutive_losses=0,
                monthly_breakdown={}, trades=trades,
            )

        wins = [t for t in trades if t.pnl_points > 0]
        losses = [t for t in trades if t.pnl_points <= 0]
        pnls = [t.pnl_points for t in trades]

        # Running P&L for drawdown
        cumulative = np.cumsum(pnls)
        peak = np.maximum.accumulate(cumulative)
        drawdown = peak - cumulative
        max_dd = float(drawdown.max()) if len(drawdown) > 0 else 0

        gross_profit = sum(t.pnl_points for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl_points for t in losses)) if losses else 0.001

        # Sharpe (annualized, assuming ~250 trading days)
        if len(pnls) > 1:
            mean_pnl = np.mean(pnls)
            std_pnl = np.std(pnls)
            sharpe = (mean_pnl / std_pnl) * np.sqrt(250) if std_pnl > 0 else 0
        else:
            sharpe = 0

        # Consecutive streaks
        max_consec_w = max_consec_l = cur_w = cur_l = 0
        for t in trades:
            if t.pnl_points > 0:
                cur_w += 1
                cur_l = 0
                max_consec_w = max(max_consec_w, cur_w)
            else:
                cur_l += 1
                cur_w = 0
                max_consec_l = max(max_consec_l, cur_l)

        # Monthly breakdown
        monthly = {}
        for t in trades:
            key = t.entry_date.strftime("%Y-%m") if hasattr(t.entry_date, 'strftime') else str(t.entry_date)[:7]
            if key not in monthly:
                monthly[key] = {"trades": 0, "wins": 0, "pnl": 0.0}
            monthly[key]["trades"] += 1
            monthly[key]["pnl"] += t.pnl_points
            if t.pnl_points > 0:
                monthly[key]["wins"] += 1

        return BacktestResult(
            period=period,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=round(len(wins) / len(trades) * 100, 1),
            avg_win_points=round(np.mean([t.pnl_points for t in wins]), 1) if wins else 0,
            avg_loss_points=round(np.mean([t.pnl_points for t in losses]), 1) if losses else 0,
            total_pnl_points=round(sum(pnls), 1),
            max_drawdown_points=round(max_dd, 1),
            profit_factor=round(gross_profit / gross_loss, 2),
            sharpe_ratio=round(sharpe, 2),
            avg_holding_bars=round(np.mean([t.holding_bars for t in trades]), 1),
            best_trade=round(max(pnls), 1),
            worst_trade=round(min(pnls), 1),
            max_consecutive_wins=max_consec_w,
            max_consecutive_losses=max_consec_l,
            monthly_breakdown=monthly,
            trades=trades,
        )

    def format_result(self, r: BacktestResult) -> str:
        if r.total_trades == 0:
            return "No trades generated during this period. The strategy was too selective or data insufficient."

        grade = (
            "A+" if r.win_rate >= 65 and r.profit_factor >= 2.0 else
            "A" if r.win_rate >= 60 and r.profit_factor >= 1.5 else
            "B" if r.win_rate >= 50 and r.profit_factor >= 1.2 else
            "C" if r.win_rate >= 45 else "D"
        )

        verdict = (
            "STRONG - strategy has a real edge" if grade in ("A+", "A") else
            "DECENT - usable with risk management" if grade == "B" else
            "WEAK - needs tuning or different market conditions" if grade == "C" else
            "POOR - do not trade this strategy blindly"
        )

        monthly_lines = []
        for month, data in sorted(r.monthly_breakdown.items()):
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            bar = "+" * int(data["pnl"] / 20) if data["pnl"] > 0 else "-" * int(abs(data["pnl"]) / 20)
            monthly_lines.append(
                f"  {month}  {data['trades']:>3} trades  {wr:>5.0f}% WR  {data['pnl']:>+8.1f} pts  {bar}"
            )

        return f"""
================================================================
  {self._index.display_name} STRATEGY BACKTEST REPORT
  Period: {r.period} | Generated: {datetime.now().strftime('%d %b %Y %H:%M')}
================================================================

  STRATEGY GRADE: {grade} -- {verdict}

----------------------------------------------------------------
  CORE METRICS
----------------------------------------------------------------
  Total Trades:        {r.total_trades}
  Win Rate:            {r.win_rate}%  ({r.winning_trades}W / {r.losing_trades}L)
  Profit Factor:       {r.profit_factor}x
  Sharpe Ratio:        {r.sharpe_ratio}

  Total P&L:           {r.total_pnl_points:+.1f} {self._index.display_name} points
  Avg Win:             {r.avg_win_points:+.1f} pts
  Avg Loss:            {r.avg_loss_points:+.1f} pts
  Best Trade:          {r.best_trade:+.1f} pts
  Worst Trade:         {r.worst_trade:+.1f} pts

  Max Drawdown:        {r.max_drawdown_points:.1f} pts
  Avg Holding:         {r.avg_holding_bars:.1f} days
  Max Consec Wins:     {r.max_consecutive_wins}
  Max Consec Losses:   {r.max_consecutive_losses}

  Per Lot ({self._index.lot_size} qty):
  - Total P&L:         Rs.{r.total_pnl_points * self._index.lot_size:,.0f}
  - Max Drawdown:      Rs.{r.max_drawdown_points * self._index.lot_size:,.0f}

----------------------------------------------------------------
  MONTHLY BREAKDOWN
----------------------------------------------------------------
{chr(10).join(monthly_lines) if monthly_lines else "  No monthly data"}

================================================================
  WHAT THIS MEANS FOR YOUR LIVE TRADING
================================================================
  Win Rate {r.win_rate}% means ~{r.winning_trades} of every {r.total_trades} trades win.
  Profit Factor {r.profit_factor}x means for every Rs.1 lost, you make Rs.{r.profit_factor:.2f}.
  {"Your edge is real. Keep strict SL and let winners run." if r.profit_factor > 1.3 else "Edge is marginal. Tighten stop-losses and be more selective."}
  {"Max drawdown is manageable." if r.max_drawdown_points < 500 else "WARNING: Large drawdown. Size positions conservatively."}
================================================================
"""


backtester = StrategyBacktester()
