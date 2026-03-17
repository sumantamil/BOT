"""Live bot monitor — polls every 60s, prints trades/errors, auto-restarts if down."""
import time, urllib.request, json, subprocess, os
from datetime import datetime

URL     = "http://127.0.0.1:8000/api"
LOG     = r"D:\Users\sundlnu\VS Code BOT\BOT\trading_bot.log"
BOT_DIR = r"D:\Users\sundlnu\VS Code BOT\BOT"
BOT_PY  = r".venv\Scripts\python.exe"

def api(ep):
    try:
        r = urllib.request.urlopen(f"{URL}/{ep}", timeout=6)
        return json.loads(r.read())
    except Exception:
        return None

def log_tail(n=80):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except Exception:
        return []

def ts():
    return datetime.now().strftime("%H:%M:%S")

def restart():
    subprocess.run("taskkill /F /IM python.exe /T", shell=True, capture_output=True)
    time.sleep(3)
    subprocess.Popen([os.path.join(BOT_DIR, BOT_PY), "main.py"], cwd=BOT_DIR,
                     creationflags=0x08000000)
    print(f"[{ts()}] BOT RESTARTED", flush=True)
    time.sleep(12)

last_line = len(log_tail())
KEYWORDS = ("ERROR", "order placed", "Trade recorded", "STOP-LOSS",
            "TRAILING STOP", "TARGET reached", "auto-clos", "DH-9")

for i in range(40):
    time.sleep(60)
    st  = api("status")
    pnl = api("pnl")
    now = ts()

    if not st:
        print(f"[{now}] BOT DOWN - restarting", flush=True)
        restart()
        continue

    pos  = st.get("open_positions", 0)
    dp   = pnl.get("daily_pnl",   0) if pnl else st.get("daily_pnl",   0)
    cp   = pnl.get("current_pnl", 0) if pnl else st.get("current_pnl", 0)
    state = st.get("state", "?")
    print(f"[{now}] {state} | pos={pos} | daily=Rs{dp:.0f} | live=Rs{cp:.0f}", flush=True)

    all_lines = log_tail()
    new_lines  = all_lines[last_line:]
    last_line  = len(all_lines)
    for line in new_lines:
        if any(k in line for k in KEYWORDS):
            prefix = "ERR" if "ERROR" in line or "DH-9" in line else "TRD"
            print(f"  [{prefix}] {line.strip()[-130:]}", flush=True)

print("Monitor complete.", flush=True)
