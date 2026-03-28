"""
FastAPI Web Application

Provides:
- REST API endpoints
- WebSocket endpoint for live chat
- Static file serving for UI
"""

import asyncio
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import logging

import sys
sys.path.append('..')
from config import settings
from bot.engine import TradingBot, create_bot
from bot.market_research import market_research
from bot.index_config import NIFTY, BANKNIFTY, SENSEX
from web.websocket import websocket_endpoint, chat_handler, manager
from bot import profit_tracker


# Global bot instance
bot: TradingBot = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.
    
    Initializes the trading bot on startup and cleans up on shutdown.
    """
    global bot
    
    logger.info("Starting NIFTY Trading Bot Web Server...")
    
    # Create bot instance
    bot = create_bot()
    
    # Connect chat handler to bot
    chat_handler.set_bot(bot)
    
    # Initialize bot (but don't login automatically)
    try:
        await bot.initialize()
        logger.info("Bot initialized")

        # Auto-start analysis loop always — paper mode still needs live signals.
        # auto_trade_enabled=false only prevents real order execution; analysis is always safe.
        await bot.start_analysis_loop()
        if settings.trading.auto_trade_enabled:
            logger.info("Auto-trade enabled — analysis loop started (LIVE mode)")
        else:
            logger.info("Paper trading mode — analysis loop started automatically (no real orders)")
    except Exception as e:
        logger.error(f"Bot initialization error: {e}")
    
    yield  # Server is running
    
    # Shutdown
    logger.info("Shutting down...")
    if bot:
        await bot.shutdown()


# Create FastAPI app
app = FastAPI(
    title="NIFTY Trading Bot",
    description="Automated NIFTY options trading bot with live chat interface",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


# ============== API Endpoints ==============

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main chat UI"""
    index_v2_path = static_path / "index_v2.html"
    if index_v2_path.exists():
        return FileResponse(str(index_v2_path))

    index_path = static_path / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    
    # Fallback if static file doesn't exist
    return HTMLResponse("""
    <html>
        <head><title>NIFTY Trading Bot</title></head>
        <body>
            <h1>NIFTY Trading Bot</h1>
            <p>Static files not found. Please ensure index.html is in the static folder.</p>
        </body>
    </html>
    """)


@app.websocket("/ws")
async def websocket_route(websocket: WebSocket):
    """WebSocket endpoint for live chat"""
    await websocket_endpoint(websocket)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Silence the browser's automatic favicon request — no file needed."""
    return Response(status_code=204)


@app.get("/api/status")
async def get_status():
    """Get current bot status"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    status = bot.get_status()
    
    # Fetch real P&L, positions, and funds from broker API
    daily_pnl = status.daily_pnl
    current_pnl = 0.0
    open_positions = status.open_positions
    funds = {"available_balance": 0.0, "used_margin": 0.0, "available_margin": 0.0}
    
    try:
        # Get positions and P&L
        kite_data = await bot.kite.get_kite_positions()
        # Only count day positions where net quantity != 0 (excludes closed round-trips)
        open_day = [p for p in kite_data.get("day", []) if (p.get("quantity") or 0) != 0]
        if kite_data.get("day"):
            daily_pnl = kite_data["total_pnl"]
            open_positions = len(open_day)
        else:
            # Broker reports no day trades (new day or no trades yet) — reset to 0
            daily_pnl = 0.0
            net_open = [p for p in kite_data.get("net", []) if (p.get("quantity") or 0) != 0]
            open_positions = len(net_open)
        # Current P&L = unrealised on positions still open (qty != 0)
        # Check both day and net lists
        current_pnl = sum(float(p.get("unrealised", 0) or 0) for p in open_day)
        if not open_day:
            current_pnl = sum(
                float(p.get("unrealised", 0) or 0)
                for p in kite_data.get("net", [])
                if (p.get("quantity") or 0) != 0
            )
        
        # Get funds/balance
        funds = await bot.kite.get_fund_limits()
    except Exception as e:
        logger.warning(f"Could not fetch broker data for status: {e}")
    
    return {
        "state": status.state.value,
        "auto_trade_enabled": status.auto_trade_enabled,
        "last_analysis": status.last_analysis.isoformat() if status.last_analysis else None,
        "open_positions": open_positions,
        "daily_pnl": daily_pnl,
        "current_pnl": current_pnl,
        "available_balance": funds["available_balance"],
        "used_margin": funds["used_margin"],
        "available_margin": funds["available_margin"],
        "kite_logged_in": bot.kite.is_logged_in(),
        "broker": settings.broker.capitalize(),
        "connections": manager.get_connection_count(),
        "rsi": round(status.last_signal.rsi, 1) if status.last_signal else None,
        "strength": round(status.last_signal.strength, 0) if status.last_signal else None,
        "trend": status.last_signal.trend if status.last_signal else None,
        # Regime + VIX from engine cache (populated after first analysis cycle)
        "regime": bot._cached_regime.regime.value if getattr(bot, '_cached_regime', None) and bot._cached_regime else None,
        "india_vix": round(bot._cached_vix, 2) if getattr(bot, '_cached_vix', 0) else None,
        # Paper trading stats (only populated in paper mode)
        "paper_trading": bot.paper_trader.get_summary() if not status.auto_trade_enabled else None,
        # Straddle strategy stats
        "straddle": bot.straddle_strategy.get_summary() if hasattr(bot, "straddle_strategy") else None,
    }


@app.get("/api/pnl")
async def get_pnl():
    """Fast P&L endpoint with 5-second broker cache.
    Called every 2 seconds by dashboard — fetches from broker but throttled
    so it never fires more than once every 5 seconds."""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")

    now = datetime.now()

    # Paper trading mode — serve live P&L from in-memory paper_trader (always fresh).
    # No broker call needed; P&L is updated every analysis cycle via check_exits().
    if not settings.trading.auto_trade_enabled:
        realized_pnl   = float(bot.paper_trader._total_pnl)
        unrealized_pnl = sum(float(p.get("pnl", 0.0)) for p in bot.paper_trader._positions)
        return {
            "daily_pnl":   round(realized_pnl + unrealized_pnl, 2),
            "current_pnl": round(unrealized_pnl, 2),
            "last_updated": now.isoformat(),
        }

    cache = getattr(app.state, "pnl_cache", None)
    cache_time = getattr(app.state, "pnl_cache_time", None)

    # Return cached value if fresher than 5 seconds
    if cache and cache_time and (now - cache_time).total_seconds() < 5:
        return cache

    # Refresh from broker
    daily_pnl   = 0.0
    current_pnl = 0.0
    try:
        kite_data = await bot.kite.get_kite_positions()
        if kite_data.get("day"):
            daily_pnl = float(kite_data.get("total_pnl") or 0)
        # current unrealised: only positions still holding (qty != 0)
        open_day = [p for p in kite_data.get("day", []) if (p.get("quantity") or 0) != 0]
        current_pnl = sum(float(p.get("unrealised", 0) or 0) for p in open_day)
        if not open_day:
            current_pnl = sum(
                float(p.get("unrealised", 0) or 0)
                for p in kite_data.get("net", [])
                if (p.get("quantity") or 0) != 0
            )
    except Exception as e:
        logger.warning(f"get_pnl: broker fetch failed: {e}")
        # Fall back to engine cache on broker error
        daily_pnl   = float(getattr(bot, "_cached_daily_pnl",   0.0))
        current_pnl = float(getattr(bot, "_cached_current_pnl", 0.0))

    result = {
        "daily_pnl":    round(daily_pnl,   2),
        "current_pnl":  round(current_pnl, 2),
        "last_updated": now.isoformat(),
    }
    app.state.pnl_cache      = result
    app.state.pnl_cache_time = now
    return result


@app.post("/api/login")
async def trigger_login():
    """Trigger Zerodha login process"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    # Run login in background
    asyncio.create_task(bot.login())
    
    return {"message": "Login initiated. Please complete 2FA in the browser window."}


@app.post("/api/start")
async def start_analysis():
    """Start the analysis loop"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    if bot._analysis_task and not bot._analysis_task.done():
        return {"message": "Analysis loop is already running"}
    
    await bot.start_analysis_loop()
    return {"message": "Analysis loop started"}


@app.post("/api/stop")
async def stop_analysis():
    """Stop the analysis loop"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    await bot.stop_analysis_loop()
    return {"message": "Analysis loop stopped"}


@app.post("/api/toggle-auto-trade")
async def toggle_auto_trade():
    """Toggle auto-trading on/off without stopping the analysis loop"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    bot._auto_trade = not bot._auto_trade
    state = "ENABLED" if bot._auto_trade else "DISABLED"
    await bot._broadcast_message(f"🤖 Auto-trading {state}", "system")
    return {"auto_trade_enabled": bot._auto_trade, "message": f"Auto-trading {state}"}


@app.post("/api/analyze")
async def run_analysis():
    """Run immediate trend analysis"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    signal = bot.analyzer.analyze()
    
    if not signal:
        raise HTTPException(status_code=500, detail="Analysis failed")
    
    return {
        "trend": signal.trend.value,
        "strength": signal.strength,
        "current_price": signal.current_price,
        "rsi": signal.rsi,
        "recommendation": signal.recommendation,
        "timestamp": signal.timestamp.isoformat()
    }


@app.post("/api/command")
async def execute_command(command: dict):
    """Execute a trading command"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    cmd = command.get("command", "")
    if not cmd:
        raise HTTPException(status_code=400, detail="No command provided")
    
    result = await bot.process_command(cmd)
    return {"result": result}


@app.get("/api/positions")
async def get_positions():
    """Get current open positions from Kite API (or paper_trader in paper mode)"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")

    # Paper trading mode — serve paper_trader open positions
    if not settings.trading.auto_trade_enabled:
        positions = []
        for pos in bot.paper_trader._positions:
            pnl_pct       = pos.get("pnl_pct", 0.0)
            premium_entry = pos.get("premium_entry", 0.0)
            est_ltp       = round(premium_entry * (1 + pnl_pct / 100), 2)
            positions.append({
                "symbol":        f"{pos['index_name']} {pos['option_type']}",
                "product":       "PAPER",
                "quantity":      1,
                "average_price": round(premium_entry, 2),
                "last_price":    est_ltp,
                "pnl":           round(pos.get("pnl", 0.0), 2),
                "exchange":      "PAPER",
                "strategy":      pos.get("strategy", ""),
                "entry_time":    pos.get("entry_time", ""),
            })
        return {"positions": positions, "source": "paper_trader"}

    # Fetch live positions from Kite API
    try:
        kite_data = await bot.kite.get_kite_positions()
        open_positions = [
            {
                "symbol": p.get("tradingsymbol", ""),
                "product": p.get("product", ""),
                "quantity": p.get("quantity", 0),
                "average_price": p.get("average_price", 0),
                "last_price": p.get("last_price", 0),
                "pnl": p.get("pnl", 0),
                "exchange": p.get("exchange", ""),
            }
            for p in kite_data["net"]
            if p.get("quantity", 0) != 0
        ]
        return {"positions": open_positions, "source": "kite"}
    except Exception as e:
        logger.warning(f"Kite positions fetch failed, using bot-tracked: {e}")
    
    # Fallback to bot-tracked positions
    positions = []
    if bot.order_manager:
        for trade_id, trade in bot.order_manager._positions.items():
            if trade.status == "OPEN":
                positions.append({
                    "symbol": trade.symbol,
                    "quantity": trade.quantity,
                    "average_price": trade.price,
                    "last_price": 0,
                    "pnl": 0,
                })
    return {"positions": positions, "source": "bot"}


@app.get("/api/trades")
async def get_trades():
    """Get today's trades (open + closed) from Kite API or paper_trader."""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")

    # Paper trading mode — return paper_trader open + closed trades
    if not settings.trading.auto_trade_enabled:
        today_str  = datetime.now().date().isoformat()
        all_trades = []
        # Open paper positions (live unrealised P&L updated by check_exits)
        for pos in bot.paper_trader._positions:
            pnl_pct       = pos.get("pnl_pct", 0.0)
            premium_entry = pos.get("premium_entry", 0.0)
            est_ltp       = round(premium_entry * (1 + pnl_pct / 100), 2)
            all_trades.append({
                "symbol":        f"{pos['index_name']} {pos['option_type']} [{pos['strategy']}]",
                "product":       "PAPER",
                "buy_quantity":  1,
                "sell_quantity": 0,
                "quantity":      1,
                "average_price": round(premium_entry, 2),
                "last_price":    est_ltp,
                "pnl":           round(pos.get("pnl", 0.0), 2),
                "realised":      0.0,
                "unrealised":    round(pos.get("pnl", 0.0), 2),
                "status":        "OPEN",
                "exchange":      "PAPER",
                "exit_reason":   "",
            })
        # Closed paper trades from today
        for trade in bot.paper_trader._trades:
            if trade.get("entry_time", "")[:10] != today_str:
                continue
            premium_entry = trade.get("premium_entry", 0.0)
            pnl_pct       = trade.get("pnl_pct", 0.0)
            est_exit      = round(premium_entry * (1 + pnl_pct / 100), 2)
            all_trades.append({
                "symbol":        f"{trade['index_name']} {trade['option_type']} [{trade['strategy']}]",
                "product":       "PAPER",
                "buy_quantity":  1,
                "sell_quantity": 1,
                "quantity":      0,
                "average_price": round(premium_entry, 2),
                "last_price":    est_exit,
                "pnl":           round(trade.get("pnl", 0.0), 2),
                "realised":      round(trade.get("pnl", 0.0), 2),
                "unrealised":    0.0,
                "status":        "CLOSED",
                "exchange":      "PAPER",
                "exit_reason":   trade.get("exit_reason", ""),
            })
        total_pnl = sum(t["pnl"] for t in all_trades)
        return {"trades": all_trades, "total_pnl": round(total_pnl, 2), "count": len(all_trades)}

    try:
        kite_data = await bot.kite.get_kite_positions()
        day_trades = [
            {
                "symbol": p.get("tradingsymbol", ""),
                "product": p.get("product", ""),
                "buy_quantity": p.get("buy_quantity", 0),
                "sell_quantity": p.get("sell_quantity", 0),
                "quantity": p.get("quantity", 0),  # 0 = fully closed
                "average_price": p.get("average_price", 0),
                "last_price": p.get("last_price", 0),
                "pnl": p.get("pnl", 0),
                "realised": p.get("realised", 0),
                "unrealised": p.get("unrealised", 0),
                "status": "OPEN" if p.get("quantity", 0) != 0 else "CLOSED",
                "exchange": p.get("exchange", ""),
            }
            for p in kite_data["day"]
        ]
        return {
            "trades": day_trades,
            "total_pnl": kite_data["total_pnl"],
            "count": len(day_trades)
        }
    except Exception as e:
        logger.warning(f"Could not fetch trades from Kite: {e}")
        return {"trades": [], "total_pnl": 0.0, "count": 0}


@app.get("/api/compare-indices")
async def compare_indices():
    """Compare NIFTY, BANKNIFTY, and SENSEX to find best trading opportunity"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")

    # Cache for 60 seconds — this endpoint fetches yfinance 3× per call;
    # with multiple browser tabs polling every 60s, cache prevents a data-fetch storm.
    from datetime import datetime as _dt
    cache      = getattr(app.state, "compare_cache",      None)
    cache_time = getattr(app.state, "compare_cache_time", None)
    if cache and cache_time and (_dt.now() - cache_time).total_seconds() < 60:
        return cache

    from bot.trend_analyzer import TrendAnalyzer
    
    indices_data = []
    
    for index_config in [NIFTY, BANKNIFTY, SENSEX]:
        try:
            analyzer = TrendAnalyzer(index_config=index_config)
            signal = analyzer.analyze()
            
            if signal:
                strength = getattr(signal, 'strength', 0)
                indices_data.append({
                    "name": index_config.display_name,
                    "symbol": index_config.name,
                    "current_price": signal.current_price,
                    "trend": signal.trend.value,
                    "strength": strength,
                    "recommendation": signal.recommendation,
                    "rsi": getattr(signal, 'rsi', 0),
                    "macd": getattr(signal, 'macd', 0),
                    "sma_20": getattr(signal, 'sma_20', 0),
                    "sma_50": getattr(signal, 'sma_50', 0),
                })
            else:
                indices_data.append({
                    "name": index_config.display_name,
                    "symbol": index_config.name,
                    "current_price": 0,
                    "trend": "UNKNOWN",
                    "strength": 0,
                    "recommendation": "Data unavailable",
                    "rsi": 0,
                    "macd": 0,
                    "sma_20": 0,
                    "sma_50": 0,
                })
        except Exception as e:
            logger.error(f"Error analyzing {index_config.display_name}: {e}")
            indices_data.append({
                "name": index_config.display_name,
                "symbol": index_config.name,
                "current_price": 0,
                "trend": "ERROR",
                "strength": 0,
                "recommendation": f"Error: {str(e)[:50]}",
                "rsi": 0,
                "macd": 0,
                "sma_20": 0,
                "sma_50": 0,
            })
    
    # Find best opportunity
    bullish_signals = [idx for idx in indices_data if idx["trend"] == "BULLISH"]
    if bullish_signals:
        best = max(bullish_signals, key=lambda x: x["strength"])
        best_recommendation = f"BULLISH: {best['name']} ({best['strength']:.1f}%)"
    else:
        best = max(indices_data, key=lambda x: x["strength"])
        best_recommendation = f"Best: {best['name']} ({best['strength']:.1f}% - {best['trend']})"

    result = {
        "indices": indices_data,
        "best_opportunity": best_recommendation,
        "best_index": best["symbol"]
    }
    # Save to cache
    app.state.compare_cache      = result
    app.state.compare_cache_time = _dt.now()
    return result


@app.get("/api/ledger")
async def get_ledger(days: int = 30):
    """
    Trading ledger: all TRADE events from journal files + today's live Kite positions.
    Returns per-trade rows plus summary stats (total P&L, win rate, trade count).
    """
    import os, json as _json
    from datetime import date, timedelta

    records = []

    # ── 1. Historical trades from journal files ────────────────────────────
    journal_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "journals")
    cutoff = date.today() - timedelta(days=days)

    if os.path.isdir(journal_dir):
        for fname in sorted(os.listdir(journal_dir)):
            if not fname.startswith("journal_") or not fname.endswith(".json"):
                continue
            day_str = fname[8:18]   # "2026-03-13"
            try:
                day_date = date.fromisoformat(day_str)
            except ValueError:
                continue
            if day_date < cutoff:
                continue
            try:
                with open(os.path.join(journal_dir, fname)) as f:
                    entries = _json.load(f)
                for e in entries:
                    if e.get("event_type") != "TRADE":
                        continue
                    d = e.get("details", {})
                    action = d.get("action", "")
                    # Only show SELL (close) rows in ledger — avoids double-counting
                    if action not in ("SELL", "BUY"):
                        continue
                    # Symbol: prefer explicit 'symbol' field over direction
                    sym = d.get("symbol", "") or d.get("customSymbol", "")
                    if not sym:
                        direction = e.get("direction", "")
                        sym = direction if direction not in ("CLOSE", "N/A", "") else "—"
                    # Price: prefer 'price' over legacy 'premium'
                    price = float(d.get("price") or d.get("premium") or d.get("tradedPrice") or 0)
                    # P&L: prefer net_pnl → gross_pnl → pnl, only on SELL rows
                    pnl_val = None
                    if action == "SELL":
                        pnl_val = d.get("net_pnl") or d.get("gross_pnl") or d.get("pnl")
                        if pnl_val is not None:
                            pnl_val = float(pnl_val)
                    # Option type from symbol
                    opt = "CE" if "CE" in sym or "CALL" in sym else ("PE" if "PE" in sym or "PUT" in sym else "")
                    records.append({
                        "date": day_str,
                        "time": e.get("timestamp", "")[:19].replace("T", " "),
                        "action": action,
                        "symbol": sym,
                        "strike": float(d.get("strike") or d.get("drvStrikePrice") or 0),
                        "option_type": opt,
                        "premium": price,
                        "quantity": int(d.get("quantity") or d.get("tradedQuantity") or 0),
                        "pnl": pnl_val,
                        "status": "CLOSED" if action == "SELL" else "OPEN",
                        "source": "journal",
                    })
            except Exception as ex:
                logger.warning(f"Ledger: could not read {fname}: {ex}")

    # ── 1b. Dhan ledger_report for net settled P&L per day (fills gaps) ──
    from config import settings as _cfg
    if bot and str(_cfg.broker).lower() == "dhan":
        try:
            from dhanhq import dhanhq as _dhan
            _dc = _dhan(_cfg.dhan.client_id, _cfg.dhan.access_token)
            from_dt = cutoff.isoformat()
            to_dt   = date.today().isoformat()
            lr = _dc.ledger_report(from_dt, to_dt)
            for row in (lr.get("data") or []):
                narration = row.get("narration", "")
                vdate     = row.get("voucherdate", "")   # "Mar 16, 2026"
                debit     = float(row.get("debit") or 0)
                credit    = float(row.get("credit") or 0)
                if narration != "Trades Executed":
                    continue
                # Convert "Mar 16, 2026" → "2026-03-16"
                try:
                    from datetime import datetime as _dt
                    day_str = _dt.strptime(vdate, "%b %d, %Y").date().isoformat()
                except Exception:
                    continue
                # Net P&L = credit - debit for that day's settled trades
                net = round(credit - debit, 2)
                # Only add if no journal already covers that day with P&L data
                covered = any(r["date"] == day_str and r.get("pnl") is not None for r in records)
                if not covered:
                    records.append({
                        "date": day_str,
                        "time": day_str + " 15:30:00",
                        "action": "SETTLE",
                        "symbol": f"Dhan F&O Settlement ({row.get('vouchernumber','')})",
                        "strike": 0,
                        "option_type": "",
                        "premium": 0,
                        "quantity": 0,
                        "pnl": net,
                        "status": "CLOSED",
                        "source": "dhan_ledger",
                    })
        except Exception as ex:
            logger.warning(f"Ledger: Dhan ledger_report failed: {ex}")

    # ── 2. Today's live positions from Kite ───────────────────────────────
    today_str = date.today().isoformat()
    if bot:
        try:
            kite_data = await bot.kite.get_kite_positions()
            for p in kite_data.get("day", []):
                pnl_val = p.get("pnl", 0) or 0
                records.append({
                    "date": today_str,
                    "time": today_str + " (live)",
                    "action": "BUY" if (p.get("buy_quantity", 0) or 0) > 0 else "SELL",
                    "symbol": p.get("tradingsymbol", ""),
                    "strike": 0,
                    "option_type": "CE" if "CE" in p.get("tradingsymbol", "") else ("PE" if "PE" in p.get("tradingsymbol", "") else ""),
                    "premium": p.get("average_price", 0),
                    "quantity": abs(p.get("buy_quantity", 0) or 0),
                    "pnl": pnl_val,
                    "status": "OPEN" if (p.get("quantity", 0) or 0) != 0 else "CLOSED",
                    "source": "kite",
                })
        except Exception as ex:
            logger.warning(f"Ledger: could not fetch Kite positions: {ex}")

    # ── 3. Summary stats ──────────────────────────────────────────────────
    closed = [r for r in records if r["status"] == "CLOSED" and r.get("pnl") is not None]
    wins   = [r for r in closed if (r["pnl"] or 0) > 0]
    losses = [r for r in closed if (r["pnl"] or 0) < 0]
    total_pnl   = sum(r["pnl"] or 0 for r in records if r.get("pnl") is not None)
    win_rate    = round(len(wins) / len(closed) * 100, 1) if closed else 0
    avg_win     = round(sum(r["pnl"] for r in wins) / len(wins), 2) if wins else 0
    avg_loss    = round(sum(r["pnl"] for r in losses) / len(losses), 2) if losses else 0

    return {
        "records": sorted(records, key=lambda x: x["time"], reverse=True),
        "summary": {
            "total_trades": len(records),
            "closed_trades": len(closed),
            "open_trades": len([r for r in records if r["status"] == "OPEN"]),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": win_rate,
            "total_pnl": round(total_pnl, 2),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }
    }


@app.get("/api/symbols")
async def list_symbols(
    query: str = Query(default="", description="Filter by symbol or name"),
    exchange: str = Query(default="ALL", description="NSE, BSE, or ALL"),
    limit: int = Query(default=50, ge=5, le=500, description="Max results")
):
    """List symbols for auto-suggest"""
    return {
        "symbols": market_research.list_symbols(query=query, exchange=exchange, limit=limit)
    }


@app.get("/api/health-score")
async def get_health_score():
    """
    Bot health score (0–100) with grade and issue list.

    Checks:
    - Win rate (deducted if < 50%)
    - IV history days (deducted if < 5)
    - Consecutive losses (deducted if ≥ 4)
    - Open positions with critical theta (deducted if any theta < -80 ₹/day)
    - Today's slippage (deducted if avg > ₹5)
    - EOD win rate (deducted if < 55% after 20 trades)
    """
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")

    from bot.iv_monitor import iv_monitor
    from bot.engine import _eod_tracker

    score = 100
    issues: list[str] = []
    details: dict = {}

    # ── 1. Win rate ──────────────────────────────────────────────────────
    try:
        stats = bot.order_manager._daily_stats if bot.order_manager else None
        total_trades = stats.total_trades if stats else 0
        wins = stats.winning_trades if stats else 0
        if total_trades >= 5:
            wr = wins / total_trades
            details["win_rate_pct"] = round(wr * 100, 1)
            if wr < 0.50:
                score -= 20
                issues.append(f"Win rate {wr*100:.1f}% (below 50%)")
        else:
            details["win_rate_pct"] = None
    except Exception:
        details["win_rate_pct"] = None

    # ── 2. IV history ────────────────────────────────────────────────────
    try:
        iv_days = max(
            (len(v) for v in iv_monitor._iv_history.values()),
            default=0
        )
        details["iv_history_days"] = iv_days
        if iv_days < 5:
            score -= 10
            issues.append(f"IV history only {iv_days} day(s) — need 5+ to activate filter")
    except Exception:
        details["iv_history_days"] = 0

    # ── 3. Consecutive losses ────────────────────────────────────────────
    try:
        consec = bot.order_manager._consecutive_losses if bot.order_manager else 0
        details["consecutive_losses"] = consec
        if consec >= 4:
            score -= 15
            issues.append(f"{consec} consecutive losses — consider pausing")
    except Exception:
        details["consecutive_losses"] = 0

    # ── 4. Critical theta on open positions ──────────────────────────────
    try:
        critical_theta_positions = []
        if bot.order_manager:
            for t in bot.order_manager._positions.values():
                if t.status == "OPEN" and t.theta_daily_at_entry < -80:
                    critical_theta_positions.append(t.symbol)
        details["critical_theta_positions"] = critical_theta_positions
        if critical_theta_positions:
            score -= 10
            issues.append(
                f"Critical theta on: {', '.join(critical_theta_positions)}"
            )
    except Exception:
        details["critical_theta_positions"] = []

    # ── 5. Today's slippage ──────────────────────────────────────────────
    try:
        slippages = []
        if bot.order_manager:
            for t in bot.order_manager._trade_history + list(bot.order_manager._positions.values()):
                if t.slippage != 0.0:
                    slippages.append(abs(t.slippage))
        avg_slip = round(sum(slippages) / len(slippages), 2) if slippages else 0.0
        details["avg_slippage_today"] = avg_slip
        if avg_slip > 5:
            score -= 15
            issues.append(f"High avg slippage today: ₹{avg_slip:.1f}")
    except Exception:
        details["avg_slippage_today"] = 0.0

    # ── 6. EOD win rate ──────────────────────────────────────────────────
    try:
        eod_wr = _eod_tracker.win_rate()
        eod_n = _eod_tracker.count()
        details["eod_win_rate_pct"] = round(eod_wr * 100, 1) if eod_wr >= 0 else None
        details["eod_trades"] = eod_n
        if eod_wr >= 0 and eod_wr < 0.55:
            score -= 10
            issues.append(
                f"EOD win rate {eod_wr*100:.1f}% over {eod_n} trades — below 55% target"
            )
    except Exception:
        details["eod_win_rate_pct"] = None
        details["eod_trades"] = 0

    score = max(0, score)
    if score >= 85:
        grade = "A"
        colour = "🟢"
    elif score >= 70:
        grade = "B"
        colour = "🟡"
    elif score >= 50:
        grade = "C"
        colour = "🟠"
    else:
        grade = "F"
        colour = "🔴"

    return {
        "score": score,
        "grade": grade,
        "colour": colour,
        "label": f"{colour} Bot Health: {score}/100 (Grade {grade})",
        "issues": issues,
        "details": details,
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "bot_initialized": bot is not None,
        "connections": manager.get_connection_count()
    }


# ─────────────────────────── Profit Tracker ──────────────────────────────────

@app.get("/api/profit-tracker")
async def get_profit_tracker():
    """Return all daily rows + cumulative totals + current config."""
    try:
        return profit_tracker.get_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/profit-tracker/config")
async def update_profit_tracker_config(body: dict):
    """
    Update investor config (names, capital amounts, operator fee %).
    Body: { operator_fee_pct, brokerage_per_trade,
            investors: [{name, capital}, ...] }
    """
    try:
        saved = profit_tracker.update_config(body)
        return {"ok": True, "config": saved}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/profit-tracker/record")
async def manual_record_profit(body: dict):
    """
    Manually record (or re-record) a day's P&L.
    Body: { gross_pnl, total_trades, wins, losses, notes?, date? (YYYY-MM-DD) }
    """
    from datetime import date as _date
    try:
        trade_date = None
        if body.get("date"):
            trade_date = _date.fromisoformat(body["date"])
        row = profit_tracker.record_daily_pnl(
            gross_pnl=float(body.get("gross_pnl", 0)),
            total_trades=int(body.get("total_trades", 0)),
            wins=int(body.get("wins", 0)),
            losses=int(body.get("losses", 0)),
            notes=str(body.get("notes", "Manual entry")),
            trade_date=trade_date,
        )
        return {"ok": True, "row": row}
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/profit-tracker/sync")
async def sync_profit_tracker_to_sheets():
    """Force an immediate Google Sheets sync."""
    try:
        ok = profit_tracker.sync_to_google_sheets()
        if ok:
            return {"ok": True, "message": "Google Sheets synced successfully"}
        else:
            return {"ok": False, "message": "Google Sheets not configured or sync failed — check GSHEET_SPREADSHEET_ID and GSHEET_CREDENTIALS_PATH in .env"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== Factory Function ==============

def create_app() -> FastAPI:
    """Factory function to create the app"""
    return app


# ============== Main Entry ==============

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app:app",
        host=settings.web.host,
        port=settings.web.port,
        reload=True
    )
