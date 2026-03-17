import time, urllib.request, json, sys, subprocess
from datetime import datetime

URL   = "http://127.0.0.1:8000/api"
LOG   = r"D:\Users\sundlnu\VS Code BOT\BOT\trading_bot.log"
BOT_DIR = r"D:\Users\sundlnu\VS Code BOT\BOT"
last_trade_count = 0
known_errors = set()
check = 0

def api(endpoint):
    try:
        r = urllib.request.urlopen(f"{URL}/{endpoint}", timeout=5)
        return json.loads(r.read())
    except: return None

def tail(n=60):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except: return []

def restart():
    subprocess.run("taskkill /F /IM python.exe /T", shell=True, capture_output=True)
    time.sleep(2)
    subprocess.Popen([r".venv\Scripts\python.exe", "main.py"],
        cwd=BOT_DIR, creationflags=0x08000000)
    print(f"[{now()}] BOT RESTARTED", flush=True)
    time.sleep(10)

def now(): return datetime.now().strftime("%H:%M:%S")

while True:
    check += 1
    st = api("status")
    pnl = api("pnl")
    ts = now()

    if not st:
        print(f"[{ts}] ⚠ Bot unreachable — restarting", flush=True)
        restart(); continue

    pos  = st.get("open_positions", 0)
    dpnl = pnl.get("daily_pnl",0)   if pnl else st.get("daily_pnl",0)
    cpnl = pnl.get("current_pnl",0) if pnl else st.get("current_pnl",0)
    state = st.get("state","?")

    # Scan last 60 lines for new errors / trades
    lines = tail(60)
    new_errs = [l.strip() for l in lines if "ERROR" in l and l not in known_errors]
    for e in new_errs: known_errors.add(e)
    trades = [l.strip() for l in lines if "order placed" in l or "Trade recorded" in l]

    print(f"[{ts}] {state} | pos={pos} | daily=₹{dpnl:.0f} | live=₹{cpnl:.0f}", flush=True)
    for e in new_errs[-3:]:
        print(f"  ❌ {e[-120:]}", flush=True)
    for t in trades[-2:]:
        print(f"  ✅ {t[-120:]}", flush=True)

    time.sleep(60)
