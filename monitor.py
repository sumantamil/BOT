"""
Watchdog Monitor — monitors the trading bot and auto-restarts on crash/stall.

Runs alongside main.py. Checks every 60 seconds:
  • Bot process alive? (HTTP /api/status)
  • Last analysis not stale (> 6 min without analysis → loop dead)
  • Auto-restarts if down, with rate limit of 3 restarts/hour

Usage:
  .venv\\Scripts\\python.exe monitor.py
"""
import sys, os, time, json, subprocess, logging, urllib.request, urllib.error
from datetime import datetime, time as _time


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("watchdog.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")

BOT_URL   = "http://127.0.0.1:8000"
BOT_CMD   = [sys.executable, "main.py"]
CHECK_SEC = 60          # health-check interval seconds
STALE_SEC = 360         # analysis stale threshold (6 min)
MAX_RESTARTS_PER_HOUR = 3

_restart_times: list = []
_bot_proc = None


def _is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return _time(9, 0) <= t <= _time(15, 35)


def _fetch_status():
    """Return parsed /api/status dict or None on error."""
    try:
        with urllib.request.urlopen(f"{BOT_URL}/api/status", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _fetch_start() -> bool:
    """POST /api/start to restart the analysis loop inside a running bot."""
    try:
        req = urllib.request.Request(
            f"{BOT_URL}/api/start", method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _can_restart() -> bool:
    global _restart_times
    now = time.time()
    _restart_times = [t for t in _restart_times if t > now - 3600]
    return len(_restart_times) < MAX_RESTARTS_PER_HOUR


def _kill_bot():
    """Kill any python process holding port 8000."""
    try:
        subprocess.run(
            ["powershell", "-Command",
             "Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force"],
            timeout=10, check=False, capture_output=True,
        )
        time.sleep(3)
    except Exception as e:
        log.warning("Kill attempt failed: %s", e)


def _start_bot():
    global _restart_times
    log.info("Starting bot: %s", " ".join(BOT_CMD))
    proc = subprocess.Popen(
        BOT_CMD,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _restart_times.append(time.time())
    return proc


def main():
    global _bot_proc
    log.info("Watchdog started — monitoring bot at %s", BOT_URL)
    log.info("Market hours: 09:00-15:35 IST (Mon-Fri)")
    consecutive_failures = 0

    while True:
        time.sleep(CHECK_SEC)
        in_market = _is_market_hours()
        status = _fetch_status()

        if status is None:
            consecutive_failures += 1
            log.warning("Bot unreachable (%d consecutive failures)", consecutive_failures)
            if consecutive_failures >= 2 and in_market:
                if _can_restart():
                    log.error("Bot DOWN — restarting (restarts this hour: %d/%d)",
                              len(_restart_times), MAX_RESTARTS_PER_HOUR)
                    _kill_bot()
                    _bot_proc = _start_bot()
                    time.sleep(15)
                else:
                    log.critical(
                        "Bot DOWN but restart limit reached (%d/hour). Manual action needed.",
                        MAX_RESTARTS_PER_HOUR,
                    )
            continue

        consecutive_failures = 0
        state         = status.get("state", "")
        last_analysis = status.get("last_analysis")
        open_pos      = status.get("open_positions", 0)
        daily_pnl     = status.get("daily_pnl", 0)
        auto_trade    = status.get("auto_trade_enabled", False)

        log.info(
            "OK | state=%s | positions=%d | daily_pnl=RS%.2f | auto=%s | last=%s",
            state, open_pos, daily_pnl, auto_trade, last_analysis or "never",
        )

        # Check: analysis loop stale?
        if state == "RUNNING" and last_analysis and in_market:
            try:
                last_dt = datetime.fromisoformat(last_analysis)
                age_sec = (datetime.now() - last_dt).total_seconds()
                if age_sec > STALE_SEC:
                    log.warning("Analysis stale (%.0f s > %d s) — nudging /api/start",
                                age_sec, STALE_SEC)
                    ok = _fetch_start()
                    log.info("/api/start nudge: %s", "OK" if ok else "FAILED")
            except (ValueError, TypeError):
                pass

        # Check: bot in bad state during market hours?
        if state not in ("RUNNING", "ANALYZING") and in_market:
            log.warning("Bot state is '%s' during market hours — restarting", state)
            if _can_restart():
                _kill_bot()
                _bot_proc = _start_bot()
                time.sleep(15)


if __name__ == "__main__":
    main()
