"""
Telegram Command Handler
========================
Polls the Telegram Bot API for incoming commands and lets you control the bot
from your phone. Uses long-polling (getUpdates) — no webhook needed.

Supported commands:
  /status         Bot state, positions, P&L
  /pause          Pause auto-trading
  /resume         Resume auto-trading
  /positions      Open positions with live P&L
  /close_all      Emergency close all positions
  /paper_summary  Today's paper trading results
  /set_sl 20      Change stop-loss % for paper trades
  /set_target 50  Change target % for paper trades
  /regime         Current market regime
  /vix            Current India VIX

Usage:
  handler = TelegramCommandHandler(engine)
  asyncio.create_task(handler.start_polling())   # runs in background

The poller stops automatically when the bot stops. It will silently no-op if
ALERT_TELEGRAM_ENABLED=false or token/chat_id are not configured.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import httpx
from loguru import logger

from config import settings

if TYPE_CHECKING:
    from bot.engine import TradingEngine


class TelegramCommandHandler:
    """
    Polls the Telegram Bot API and dispatches commands to the trading engine.
    Runs as a background asyncio task.
    """

    _POLL_TIMEOUT  = 30    # long-poll timeout in seconds
    _RETRY_SLEEP   = 5     # sleep between retries on error
    _MAX_ERRORS    = 10    # give up after N consecutive errors

    def __init__(self, engine: "TradingEngine") -> None:
        self._engine     = engine
        self._token      = settings.alert.telegram_bot_token
        self._chat_id    = settings.alert.telegram_chat_id
        self._enabled    = bool(settings.alert.telegram_enabled and self._token and self._chat_id)
        self._running    = False
        self._offset     = 0        # last processed update_id + 1
        self._error_count = 0
        self._base_url   = f"https://api.telegram.org/bot{self._token}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_polling(self) -> None:
        """Start the long-poll loop (run as asyncio.create_task)."""
        if not self._enabled:
            logger.info("TelegramCommandHandler: disabled (token/chat_id not configured)")
            return

        self._running = True
        logger.info("TelegramCommandHandler: polling started")
        await self._send("🤖 *Bot online* — send /status for current state")

        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._dispatch(update)
                self._error_count = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._error_count += 1
                logger.warning(f"TelegramCommandHandler poll error ({self._error_count}): {exc}")
                if self._error_count >= self._MAX_ERRORS:
                    logger.error("TelegramCommandHandler: too many errors, stopping poll loop")
                    break
                await asyncio.sleep(self._RETRY_SLEEP)

        logger.info("TelegramCommandHandler: polling stopped")

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Telegram HTTP helpers
    # ------------------------------------------------------------------

    async def _get_updates(self) -> list:
        async with httpx.AsyncClient(timeout=self._POLL_TIMEOUT + 5) as client:
            resp = await client.get(
                f"{self._base_url}/getUpdates",
                params={
                    "offset":          self._offset,
                    "timeout":         self._POLL_TIMEOUT,
                    "allowed_updates": ["message"],
                },
            )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates failed: {data}")
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def _send(self, text: str, chat_id: Optional[str] = None) -> None:
        """Send a Markdown message to the configured chat."""
        cid = chat_id or self._chat_id
        if not cid or not self._token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{self._base_url}/sendMessage",
                    json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
                )
        except Exception as exc:
            logger.debug(f"TelegramCommandHandler _send error: {exc}")

    # ------------------------------------------------------------------
    # Command dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, update: dict) -> None:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        sender_chat = str(msg.get("chat", {}).get("id", ""))

        if not text.startswith("/"):
            return

        # Security: only accept commands from the configured chat
        if sender_chat != self._chat_id:
            logger.warning(f"TelegramCommandHandler: ignoring command from unknown chat {sender_chat}")
            return

        parts = text.split()
        cmd   = parts[0].lower().split("@")[0]  # strip @botname suffix
        args  = parts[1:]

        logger.info(f"Telegram command received: {cmd} {args}")

        handlers = {
            "/status":        self._cmd_status,
            "/pause":         self._cmd_pause,
            "/resume":        self._cmd_resume,
            "/positions":     self._cmd_positions,
            "/close_all":     self._cmd_close_all,
            "/paper_summary": self._cmd_paper_summary,
            "/set_sl":        self._cmd_set_sl,
            "/set_target":    self._cmd_set_target,
            "/regime":        self._cmd_regime,
            "/vix":           self._cmd_vix,
            "/help":          self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler:
            await handler(args)
        else:
            await self._send(f"Unknown command: `{cmd}`\nSend /help for the command list.")

    # ------------------------------------------------------------------
    # Individual command handlers
    # ------------------------------------------------------------------

    async def _cmd_status(self, _args: list) -> None:
        e = self._engine
        state = e.state.value if hasattr(e.state, "value") else str(e.state)
        mode  = "🔴 LIVE" if e._auto_trade else "📋 PAPER"
        positions = len(getattr(e.order_manager, "_positions", [])) if e.order_manager else 0
        paper_sum = e.paper_trader.get_summary()
        pnl  = paper_sum["total_pnl"]
        wr   = paper_sum["win_rate"]
        trades = paper_sum["total_trades"]
        msg = (
            f"🤖 *Bot Status*\n"
            f"State: `{state}` | Mode: {mode}\n"
            f"Open positions: {positions}\n"
            f"📋 Paper trades today: {trades} | WR: {wr} | P&L: ₹{pnl:+.0f}"
        )
        await self._send(msg)

    async def _cmd_pause(self, _args: list) -> None:
        e = self._engine
        e._auto_trade_paused = True
        logger.info("Telegram: bot paused by Telegram command")
        await self._send("⏸ Auto-trading *paused* via Telegram")

    async def _cmd_resume(self, _args: list) -> None:
        e = self._engine
        e._auto_trade_paused = False
        logger.info("Telegram: bot resumed by Telegram command")
        await self._send("▶️ Auto-trading *resumed* via Telegram")

    async def _cmd_positions(self, _args: list) -> None:
        e = self._engine
        if not e.order_manager:
            await self._send("No broker connected — paper mode only")
            return
        try:
            positions = await e.order_manager.get_positions()
            if not positions:
                await self._send("No open positions")
                return
            lines = ["📊 *Open Positions*"]
            for p in positions[:10]:
                sym = p.get("tradingSymbol") or p.get("symbol", "—")
                qty = p.get("netQty") or p.get("quantity", 0)
                pnl = p.get("unrealisedProfit") or p.get("pnl") or 0
                lines.append(f"• `{sym}` qty={qty} P&L=₹{float(pnl):+.0f}")
            await self._send("\n".join(lines))
        except Exception as exc:
            await self._send(f"Could not fetch positions: {exc}")

    async def _cmd_close_all(self, _args: list) -> None:
        e = self._engine
        if not e.order_manager:
            await self._send("⚠️ No broker connected. Cannot close positions.")
            return
        try:
            await self._send("🚨 *Emergency close all positions* initiated…")
            await e.order_manager.close_all_positions()
            await self._send("✅ All positions closed")
        except Exception as exc:
            await self._send(f"❌ Close all failed: {exc}")

    async def _cmd_paper_summary(self, _args: list) -> None:
        s = self._engine.paper_trader.get_summary()
        straddle_s = self._engine.straddle_strategy.get_summary() if hasattr(self._engine, "straddle_strategy") else {}
        lines = [
            "📋 *Paper Trading Summary*",
            f"Total trades: {s['total_trades']}  |  Open: {s['open_positions']}",
            f"Wins: {s['wins']}  |  Losses: {s['losses']}  |  WR: {s['win_rate']}",
            f"Cumulative P&L: ₹{s['total_pnl']:+.0f}",
        ]
        if straddle_s.get("total"):
            lines.append(
                f"\nStraddle: {straddle_s['total']} trades | WR: {straddle_s['win_rate']} | "
                f"P&L: ₹{straddle_s['total_pnl']:+.0f}"
            )
        best  = s.get("best_trade")
        worst = s.get("worst_trade")
        if best:
            lines.append(f"Best: {best.get('index_name')} {best.get('option_type')} ₹{best['pnl']:+.0f}")
        if worst:
            lines.append(f"Worst: {worst.get('index_name')} {worst.get('option_type')} ₹{worst['pnl']:+.0f}")
        await self._send("\n".join(lines))

    async def _cmd_set_sl(self, args: list) -> None:
        if not args:
            await self._send("Usage: /set_sl 20 (sets paper SL to 20%)")
            return
        try:
            pct = float(args[0])
            if not (5 <= pct <= 50):
                raise ValueError("must be 5–50")
            self._engine.paper_trader._sl_pct = pct
            await self._send(f"✅ Paper SL set to {pct}%")
        except ValueError as exc:
            await self._send(f"Invalid value: {exc}")

    async def _cmd_set_target(self, args: list) -> None:
        if not args:
            await self._send("Usage: /set_target 50 (sets paper target to 50%)")
            return
        try:
            pct = float(args[0])
            if not (10 <= pct <= 200):
                raise ValueError("must be 10–200")
            self._engine.paper_trader._target_pct = pct
            await self._send(f"✅ Paper target set to {pct}%")
        except ValueError as exc:
            await self._send(f"Invalid value: {exc}")

    async def _cmd_regime(self, _args: list) -> None:
        e = self._engine
        # Check if there's a cached regime for NIFTY
        nifty_regime = e._index_regime_cache.get("NIFTY") or e._index_regime_cache.get("^NSEI")
        if nifty_regime:
            regime_str = getattr(nifty_regime, "name", str(nifty_regime))
            adx = getattr(nifty_regime, "adx", 0)
            msg = f"📊 *Market Regime* (NIFTY): `{regime_str}`\nADX: {adx:.1f}"
        else:
            msg = "Regime not yet available — check back after first analysis cycle"
        await self._send(msg)

    async def _cmd_vix(self, _args: list) -> None:
        try:
            import yfinance as yf
            info = yf.Ticker("^INDIAVIX").fast_info
            vix  = float(info.get("lastPrice") or 0)
            if vix <= 0:
                df  = yf.Ticker("^INDIAVIX").history(period="1d", interval="1d")
                vix = float(df["Close"].iloc[-1]) if not df.empty else 0
            if vix > 0:
                emoji = "🟢" if vix < 15 else ("🟡" if vix < 20 else "🔴")
                await self._send(f"{emoji} *India VIX*: `{vix:.1f}`\n(< 15 cheap | 15–20 normal | > 20 expensive)")
            else:
                await self._send("Could not fetch India VIX at this time")
        except Exception as exc:
            await self._send(f"VIX fetch error: {exc}")

    async def _cmd_help(self, _args: list) -> None:
        await self._send(
            "*📋 Bot Commands*\n\n"
            "/status — Bot state, positions, P&L\n"
            "/pause — Pause auto-trading\n"
            "/resume — Resume auto-trading\n"
            "/positions — Open positions with P&L\n"
            "/close\\_all — Emergency close all positions\n"
            "/paper\\_summary — Paper trading results\n"
            "/set\\_sl 20 — Set paper SL to 20%%\n"
            "/set\\_target 50 — Set paper target to 50%%\n"
            "/regime — Current market regime\n"
            "/vix — Current India VIX"
        )
