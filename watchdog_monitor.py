"""
NIFTY Bot Watchdog — auto-restarts bot if it crashes, runs until 15:35 IST
Run: pythonw.exe watchdog_monitor.py   (no window)
 or: python.exe  watchdog_monitor.py   (with window)
"""
import subprocess, time, datetime, os, sys, urllib.request, json

BOT_DIR  = r"D:\Users\sundlnu\VS Code BOT\BOT"
BOT_PY   = os.path.join(BOT_DIR, ".venv", "Scripts", "python.exe")
LOG_FILE = os.path.join(BOT_DIR, "watchdog.log")
STATUS_URL = "http://127.0.0.1:8000/api/status"


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_status():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def is_port_listening():
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 8000), timeout=3)
        s.close()
        return True
    except Exception:
        return False


def restart_bot(reason):
    log(f"RESTART [{reason}] — killing Python processes...")
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/T"],
                       capture_output=True)
    time.sleep(3)
    subprocess.Popen(
        [BOT_PY, "main.py"],
        cwd=BOT_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    )
    time.sleep(12)
    log("RESTART complete — bot launched")


def market_is_open():
    now = datetime.datetime.now()
    close = now.replace(hour=15, minute=35, second=0, microsecond=0)
    return now < close


restart_count = 0
check_n = 0

log("=== NIFTY BOT WATCHDOG STARTED ===")
log(f"    Bot dir : {BOT_DIR}")
log(f"    Until   : 15:35 IST")

while market_is_open():
    check_n += 1
    ts = datetime.datetime.now().strftime("%H:%M:%S")

    # 1. Port connectivity
    if not is_port_listening():
        log(f"[{ts}] ALERT: port 8000 not reachable — restarting")
        restart_bot("port dead")
        restart_count += 1
        continue

    # 2. API health
    status = get_status()
    if status is None:
        log(f"[{ts}] ALERT: /api/status unreachable — restarting")
        restart_bot("API dead")
        restart_count += 1
        continue

    # 3. Stale analysis (>5 min)
    last = status.get("last_analysis")
    if last:
        try:
            last_dt = datetime.datetime.fromisoformat(last)
            age_min = (datetime.datetime.now() - last_dt).total_seconds() / 60
            if age_min > 5:
                log(f"[{ts}] ALERT: analysis stale {age_min:.1f}min — restarting")
                restart_bot("stale")
                restart_count += 1
                continue
        except Exception:
            pass

    # 4. Heartbeat
    broker   = status.get("broker", "?")
    pnl      = status.get("daily_pnl", 0)
    pos      = status.get("open_positions", 0)
    bal      = status.get("available_balance", 0)
    autotrd  = status.get("auto_trade_enabled", False)
    connected = status.get("kite_logged_in", False)

    msg = (f"[{ts}] OK | {broker} connected={connected} | auto={autotrd} "
           f"| pos={pos} | pnl=Rs{pnl} | bal=Rs{bal:.2f} | restarts={restart_count}")
    log(msg)

    time.sleep(60)

log(f"=== MARKET CLOSED. Watchdog done at {datetime.datetime.now():%H:%M:%S} ===")
