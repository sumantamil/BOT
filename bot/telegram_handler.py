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
        self._stop_confirm_pending = False    # /stop confirmation guard
        self._stop_confirm_task: Optional[asyncio.Task] = None

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

    async def _delete_message(self, message_id: int) -> None:
        """Delete a specific message from the chat (used to hide sensitive tokens)."""
        if not self._chat_id or not self._token or not message_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{self._base_url}/deleteMessage",
                    json={"chat_id": self._chat_id, "message_id": message_id},
                )
        except Exception as exc:
            logger.debug(f"TelegramCommandHandler _delete_message error: {exc}")

    # ------------------------------------------------------------------
    # Command dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, update: dict) -> None:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        sender_chat = str(msg.get("chat", {}).get("id", ""))
        message_id  = msg.get("message_id", 0)

        if not text.startswith("/"):
            return

        # Security: only accept commands from the configured chat
        if sender_chat != self._chat_id:
            logger.warning(f"TelegramCommandHandler: ignoring command from unknown chat {sender_chat}")
            return

        parts = text.split()
        cmd   = parts[0].lower().split("@")[0]  # strip @botname suffix
        args  = parts[1:]

        # Mask token in logs — never log the actual token value
        safe_args = [f"{'*' * min(len(a), 8)}..." if cmd == "/settoken" and a else a for a in args]
        logger.info(f"Telegram command received: {cmd} {safe_args}")

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
            "/pnl":           self._cmd_pnl,
            "/today":         self._cmd_today,
            "/stop":          self._cmd_stop,
            "/settoken":      self._cmd_settoken,
            "/help":          self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler:
            # Pass message_id as extra kwarg only to commands that need it
            if cmd == "/settoken":
                await handler(args, message_id=message_id)
            else:
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

    async def _cmd_pnl(self, _args: list) -> None:
        e = self._engine
        if e.order_manager:
            daily = e.order_manager._daily_pnl
            open_pnl = sum(
                (e._current_prices.get(t.symbol, t.price) - t.price) * t.quantity
                for t in e.order_manager._positions.values()
                if t.status == "OPEN" and t.symbol in e._current_prices
            ) if hasattr(e, '_current_prices') else 0.0
            total = daily + open_pnl
            sign = "📈" if total >= 0 else "📉"
            msg = (
                f"{sign} *Live P&L*\n"
                f"Realized: ₹{daily:+,.2f}\n"
                f"Unrealized: ₹{open_pnl:+,.2f}\n"
                f"Total: ₹{total:+,.2f}"
            )
        else:
            msg = "No broker connected — use /paper\_summary for paper P&L"
        await self._send(msg)

    async def _cmd_today(self, _args: list) -> None:
        e = self._engine
        lines = [f"📅 *Today's Summary ({datetime.now().strftime('%d %b %Y')})*"]
        # Live trades
        if e.order_manager:
            s = e.order_manager.get_summary()
            daily = e.order_manager._daily_pnl
            sign = "📈" if daily >= 0 else "📉"
            lines.append(
                f"\n*Live Trading*\n"
                f"Trades: {s.get('total_trades', 0)} | W: {s.get('wins', 0)} L: {s.get('losses', 0)}\n"
                f"{sign} P&L: ₹{daily:+,.2f}"
            )
        # Paper trades
        ps = e.paper_trader.get_summary()
        if ps.get('total_trades', 0) > 0:
            p_sign = "📈" if ps['total_pnl'] >= 0 else "📉"
            lines.append(
                f"\n*Paper Trading*\n"
                f"Trades: {ps['total_trades']} | WR: {ps['win_rate']}\n"
                f"{p_sign} P&L: ₹{ps['total_pnl']:+,.2f}"
            )
            if ps.get('best_trade'):
                b = ps['best_trade']
                lines.append(f"Best: {b.get('index_name','')} {b.get('option_type','')} ₹{b['pnl']:+.0f}")
            if ps.get('worst_trade'):
                w = ps['worst_trade']
                lines.append(f"Worst: {w.get('index_name','')} {w.get('option_type','')} ₹{w['pnl']:+.0f}")
        await self._send("\n".join(lines))

    async def _cmd_settoken(self, args: list) -> None:
        if not args:
            await self._send(
                "Usage: `/settoken <your_new_dhan_token>`\n\n"
                "Get your token from:\n"
                "web.dhan.co → Profile → API"
            )
            return
        new_token = args[0].strip()
        broker = self._engine.kite
        if not hasattr(broker, 'reinit_client'):
            await self._send("⚠️ Token update is only supported for Dhan broker")
            return
        await self._send("⏳ Verifying new token...")
        ok = await broker.reinit_client(new_token)
        if ok:
            await self._send(
                "✅ *Dhan token updated successfully!*\n"
                "Bot is now trading with the new token.\n"
                ".env file has also been updated."
            )
        else:
            await self._send(
                "❌ *Token update failed!*\n"
                "The token you sent is invalid or expired.\n"
                "Please get a fresh token from web.dhan.co → Profile → API"
            )

    async def _cancel_stop_confirm(self) -> None:
        """Auto-cancel the /stop confirmation after 30 seconds."""
        await asyncio.sleep(30)
        if self._stop_confirm_pending:
            self._stop_confirm_pending = False
            await self._send("\u23f0 /stop confirmation expired. Bot continues running.")

    async def _cmd_stop(self, _args: list) -> None:
        # Check if this is the confirmation step
        if _args and _args[0].lower() == "confirm":
            if not self._stop_confirm_pending:
                await self._send("No pending /stop request. Send /stop first.")
                return
            # Cancel the timeout task
            if self._stop_confirm_task and not self._stop_confirm_task.done():
                self._stop_confirm_task.cancel()
            self._stop_confirm_pending = False
            await self._send("\ud83d\uded1 *Stopping bot* \u2014 closing all positions first...")
            try:
                e = self._engine
                if e.order_manager:
                    results = await e.order_manager.close_all_positions()
                    ok = sum(1 for r in results if r.success)
                    await self._send(f"\u2705 {ok}/{len(results)} positions closed")
                self._running = False
                await e.stop()
            except Exception as exc:
                await self._send(f"\u26a0\ufe0f Stop error: {exc}")
            return

        # First /stop — ask for confirmation
        self._stop_confirm_pending = True
        if self._stop_confirm_task and not self._stop_confirm_task.done():
            self._stop_confirm_task.cancel()
        self._stop_confirm_task = asyncio.create_task(self._cancel_stop_confirm())
        await self._send(
            "\u26a0\ufe0f *Are you sure?*\n"
            "This will close ALL open positions and shut down the bot.\n\n"
            "Reply `/stop confirm` within *30 seconds* to proceed.\n"
            "Do nothing to cancel automatically."
        )

    async def _cmd_help(self, _args: list) -> None:
        await self._send(
            "*📋 Bot Commands*\n\n"
            "/status — Bot state, positions, P&L\n"
            "/pnl — Real-time live P&L\n"
            "/today — Full day summary\n"
            "/pause — Pause auto-trading\n"
            "/resume — Resume auto-trading\n"
            "/positions — Open positions with P&L\n"
            "/close\_all — Emergency close all positions\n"
            "/paper\_summary — Paper trading results\n"
            "/set\_sl 20 — Set paper SL to 20%%\n"
            "/set\_target 50 — Set paper target to 50%%\n"
            "/regime — Current market regime\n"
            "/vix — Current India VIX\n"            "/settoken <token> — Update Dhan token (no restart!)\n"            "/stop — Gracefully stop the bot"
        )
