"""
FastAPI Web Application

Provides:
- REST API endpoints
- WebSocket endpoint for live chat
- Static file serving for UI
"""

import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

import sys
sys.path.append('..')
from config import settings
from bot.engine import TradingBot, create_bot
from bot.market_research import market_research
from bot.index_config import NIFTY, BANKNIFTY, SENSEX
from web.websocket import websocket_endpoint, chat_handler, manager


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

        # Auto-start analysis + position monitor if configured
        if settings.trading.auto_trade_enabled:
            await bot.start_analysis_loop()
            logger.info("Auto-trade enabled — analysis loop started automatically")
        else:
            logger.info("Auto-trade disabled — click 'Start' in the UI to begin")
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
        if kite_data["day"]:
            daily_pnl = kite_data["total_pnl"]
            open_positions = len(kite_data["day"])
        else:
            # Broker reports no day trades (new day or no trades yet) — reset to 0
            daily_pnl = 0.0
            if kite_data["net"]:
                open_positions = len(kite_data["net"])
        # Current P&L = sum of unrealized P&L on open (net qty != 0) positions
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
        "connections": manager.get_connection_count()
    }


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
    """Get current open positions from Kite API"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
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
    """Get today's trades (open + closed) from Kite API"""
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
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
    
    return {
        "indices": indices_data,
        "best_opportunity": best_recommendation,
        "best_index": best["symbol"]
    }


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
                    if e.get("event_type") == "TRADE":
                        d = e.get("details", {})
                        records.append({
                            "date": day_str,
                            "time": e.get("timestamp", "")[:19].replace("T", " "),
                            "action": d.get("action", ""),
                            "symbol": f"{d.get('strike','')}{e.get('direction','')}",
                            "strike": d.get("strike", 0),
                            "option_type": e.get("direction", ""),
                            "premium": d.get("premium", 0),
                            "quantity": d.get("quantity", 0),
                            "pnl": d.get("pnl", None),
                            "status": d.get("status", "CLOSED"),
                            "source": "journal",
                        })
            except Exception as ex:
                logger.warning(f"Ledger: could not read {fname}: {ex}")

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


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "bot_initialized": bot is not None,
        "connections": manager.get_connection_count()
    }


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
