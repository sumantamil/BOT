"""
NIFTY Bot Self-Healing Watchdog
================================
• Auto-restarts bot if it crashes or port 8000 goes dead
• Kills port conflict automatically (no manual taskkill needed)
• Scans trading_bot.log every 30s for known error patterns
• Sends Telegram alerts with tracebacks when code bugs are detected
• Throttles restarts (max 3 per rolling 10 minutes) to prevent loops
• Runs until 15:35 IST then exits

Run: pythonw.exe watchdog_monitor.py   (no console window)
 or: python.exe  watchdog_monitor.py   (with console output)
"""

import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_DIR    = r"D:\Users\sundlnu\VS Code BOT\BOT"
BOT_PY     = os.path.join(BOT_DIR, ".venv", "Scripts", "python.exe")
BOT_LOG    = os.path.join(BOT_DIR, "trading_bot.log")
WD_LOG     = os.path.join(BOT_DIR, "watchdog.log")
STATUS_URL = "http://127.0.0.1:8000/api/status"
BOT_PORT   = 8000

# Max 3 restarts in any 10-minute window — prevents crash-loop hammering
MAX_RESTARTS_PER_WINDOW = 3
RESTART_WINDOW_SECONDS  = 600

# How many bytes from the end of the bot log to scan each cycle
LOG_SCAN_BYTES = 8_000

# Error patterns that identify a known self-healable condition vs a code bug
KNOWN_ERRORS = [
    re.compile(r"Port \d+ is already in use"),
    re.compile(r"analysis stale"),
    re.compile(r"API dead"),
    re.compile(r"port dead"),
]

# These patterns indicate a real code bug — send Telegram traceback alert
CODE_BUG_PATTERNS = [
    re.compile(r"AttributeError:"),
    re.compile(r"ImportError:"),
    re.compile(r"NameError:"),
    re.compile(r"TypeError:"),
    re.compile(r"KeyError:"),
    re.compile(r"IndexError:"),
    re.compile(r"SyntaxError:"),
    re.compile(r"RuntimeError:"),
    re.compile(r"Traceback \(most recent call last\)"),
]

# ── Telegram sender (sync, uses stdlib only) ─────────────────────────────────
def _load_telegram_creds() -> tuple:
    """Read ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID from .env"""
    env_path = os.path.join(BOT_DIR, ".env")
    token, chat_id = "", ""
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ALERT_TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                elif line.startswith("ALERT_TELEGRAM_CHAT_ID="):
                    chat_id = line.split("=", 1)[1].strip()
    except Exception:
        pass
    return token, chat_id


_TG_TOKEN, _TG_CHAT = _load_telegram_creds()


def send_telegram(message: str) -> None:
    if not _TG_TOKEN or not _TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": _TG_CHAT, "text": message}).encode()
        req  = urllib.request.Request(url, data=body,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception:
        pass  # Never crash the watchdog over a Telegram failure


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(WD_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Port helpers ──────────────────────────────────────────────────────────────
def is_port_listening() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", BOT_PORT), timeout=3)
        s.close()
        return True
    except Exception:
        return False


def kill_port_owner() -> None:
    """Kill whichever process is holding BOT_PORT so the bot can start clean."""
    log(f"SELF-HEAL: killing process on port {BOT_PORT}...")
    try:
        # netstat -ano gives: Proto  Local  Foreign  State  PID
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if f":{BOT_PORT}" in line and ("LISTENING" in line or "ESTABLISHED" in line):
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True
                    )
                    log(f"SELF-HEAL: killed PID {pid} (was holding port {BOT_PORT})")
    except Exception as e:
        log(f"SELF-HEAL: kill_port_owner failed: {e}")
    time.sleep(2)


# ── Status / API ──────────────────────────────────────────────────────────────
def get_status() -> dict | None:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── Log scanner ───────────────────────────────────────────────────────────────
_last_reported_bug: str = ""   # avoid spamming the same traceback repeatedly

def scan_bot_log() -> str | None:
    """
    Read the last LOG_SCAN_BYTES of trading_bot.log.
    Returns a short alert string if a new code bug is detected, else None.
    """
    global _last_reported_bug
    try:
        size = os.path.getsize(BOT_LOG)
        offset = max(0, size - LOG_SCAN_BYTES)
        with open(BOT_LOG, "rb") as f:
            f.seek(offset)
            tail = f.read().decode("utf-8", errors="replace")

        for pattern in CODE_BUG_PATTERNS:
            m = pattern.search(tail)
            if m:
                # Extract up to 20 lines around the match for context
                lines  = tail.splitlines()
                idx    = next(
                    (i for i, l in enumerate(lines) if pattern.search(l)), 0
                )
                snippet = "\n".join(lines[max(0, idx - 2): idx + 18])

                # Only alert if this is a new bug (different snippet)
                fingerprint = snippet[:120]
                if fingerprint == _last_reported_bug:
                    return None
                _last_reported_bug = fingerprint

                alert = (
                    f"🚨 BOT CODE BUG DETECTED\n"
                    f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
                    f"{snippet[:800]}"  # Telegram limit is 4096 chars
                )
                return alert
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"log-scan error: {e}")
    return None


# ── Restart throttle ──────────────────────────────────────────────────────────
_restart_times: deque = deque()   # timestamps of recent restarts


def _restart_allowed() -> bool:
    now = time.monotonic()
    # Evict old entries outside the rolling window
    while _restart_times and now - _restart_times[0] > RESTART_WINDOW_SECONDS:
        _restart_times.popleft()
    return len(_restart_times) < MAX_RESTARTS_PER_WINDOW


def restart_bot(reason: str) -> bool:
    """Kill all python.exe, start main.py fresh. Returns True if attempted."""
    if not _restart_allowed():
        msg = (
            f"⛔ WATCHDOG THROTTLED\n"
            f"Reason: {reason}\n"
            f"Already restarted {len(_restart_times)}× in last "
            f"{RESTART_WINDOW_SECONDS//60} min — manual intervention needed."
        )
        log(msg)
        send_telegram(msg)
        return False

    log(f"RESTART [{reason}] — killing Python processes...")
    send_telegram(f"🔄 Bot restarting\nReason: {reason}")

    # Kill the port owner specifically before killing all python.exe
    if is_port_listening():
        kill_port_owner()

    # Kill any remaining python.exe processes belonging to this bot
    subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/T"],
                   capture_output=True)
    time.sleep(3)

    _restart_times.append(time.monotonic())

    proc = subprocess.Popen(
        [BOT_PY, "main.py"],
        cwd=BOT_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    time.sleep(15)  # give bot time to bind port

    if is_port_listening():
        log(f"RESTART complete (PID {proc.pid}) — bot is UP")
        send_telegram(f"✅ Bot restarted OK (reason: {reason})")
    else:
        log(f"RESTART WARNING — port {BOT_PORT} still not listening after 15s")
        send_telegram(f"⚠️ Bot restart may have failed — port {BOT_PORT} not responding")
    return True


# ── Market hours guard ────────────────────────────────────────────────────────
def market_is_open() -> bool:
    now = datetime.datetime.now()
    if now.weekday() > 4:   # Saturday / Sunday
        return False
    return now < now.replace(hour=15, minute=35, second=0, microsecond=0)


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────
restart_count = 0
check_n       = 0
consecutive_failures = 0

startup_msg = (
    f"🛡️ NIFTY Bot Watchdog STARTED\n"
    f"Dir: {BOT_DIR}\n"
    f"Until: 15:35 IST | Max restarts: {MAX_RESTARTS_PER_WINDOW}/{RESTART_WINDOW_SECONDS//60}min"
)
log(startup_msg)
send_telegram(startup_msg)

while market_is_open():
    check_n += 1
    ts = datetime.datetime.now().strftime("%H:%M:%S")

    # ── 1. Scan bot log for code bugs ─────────────────────────────────────
    bug_alert = scan_bot_log()
    if bug_alert:
        log(f"[{ts}] CODE BUG detected — alerting via Telegram")
        send_telegram(bug_alert)
        # Don't restart for code bugs — a restart won't fix broken code,
        # and would just hide the error. Human fix needed.

    # ── 2. Port alive check ───────────────────────────────────────────────
    if not is_port_listening():
        log(f"[{ts}] ALERT: port {BOT_PORT} not reachable — attempting self-heal")
        ok = restart_bot("port dead")
        if ok:
            restart_count += 1
        consecutive_failures += 1
        if consecutive_failures >= 3:
            send_telegram(
                f"🆘 BOT UNRESPONSIVE after {consecutive_failures} heal attempts!\n"
                f"Please check manually. Watchdog will keep trying."
            )
        continue

    # ── 3. API health check ───────────────────────────────────────────────
    status = get_status()
    if status is None:
        log(f"[{ts}] ALERT: /api/status unreachable — restarting")
        ok = restart_bot("API dead")
        if ok:
            restart_count += 1
        consecutive_failures += 1
        continue

    consecutive_failures = 0  # reset on successful contact

    # ── 4. Stale analysis check (>5 min freeze = analysis loop hung) ─────
    last = status.get("last_analysis")
    if last:
        try:
            last_dt  = datetime.datetime.fromisoformat(last)
            age_min  = (datetime.datetime.now() - last_dt).total_seconds() / 60
            if age_min > 5:
                log(f"[{ts}] ALERT: analysis stale {age_min:.1f} min — restarting")
                ok = restart_bot(f"analysis frozen {age_min:.1f}min")
                if ok:
                    restart_count += 1
                continue
        except Exception:
            pass

    # ── 5. Heartbeat log ──────────────────────────────────────────────────
    broker    = status.get("broker", "?")
    pnl       = status.get("daily_pnl", 0)
    pos       = status.get("open_positions", 0)
    bal       = status.get("available_balance", 0)
    auto_trd  = status.get("auto_trade_enabled", False)
    connected = status.get("kite_logged_in", False)

    log(
        f"[{ts}] OK | {broker} connected={connected} | auto={auto_trd} "
        f"| pos={pos} | pnl=Rs{pnl:.0f} | bal=Rs{bal:.0f} | restarts={restart_count}"
    )

    # ── 6. Send Telegram summary every 30 minutes ────────────────────────
    now_min = datetime.datetime.now().minute
    if check_n % 30 == 0:
        summary = (
            f"📊 Bot status {ts}\n"
            f"Connected: {connected} | Auto-trade: {auto_trd}\n"
            f"Positions: {pos} | Daily P&L: ₹{pnl:.0f}\n"
            f"Balance: ₹{bal:.0f} | Restarts today: {restart_count}"
        )
        send_telegram(summary)

    time.sleep(60)

# ── End of trading day ────────────────────────────────────────────────────────
eod_msg = (
    f"🏁 MARKET CLOSED — Watchdog finished\n"
    f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
    f"Total restarts today: {restart_count}"
)
log(eod_msg)
send_telegram(eod_msg)
