"""
Market Monitor — polls the running bot every 5 minutes and prints a status snapshot.
Run alongside the bot:  python market_monitor.py
Stops automatically at 15:35 IST (5 min after market close).
"""
import urllib.request
import json
import time
from datetime import datetime


BASE = "http://127.0.0.1:8000"


def get(path):
    try:
        r = urllib.request.urlopen(BASE + path, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def snapshot():
    now = datetime.now()
    s  = get("/api/status")
    p  = get("/api/pnl")
    t  = get("/api/trades")
    h  = get("/api/health-score")

    print()
    print("=" * 60)
    print(f"  BOT MONITOR   {now.strftime('%H:%M:%S IST  %d-%b-%Y')}")
    print("=" * 60)

    if "error" in s:
        print(f"  !! Bot unreachable: {s['error']}")
        print("  Restart with: python main.py")
        return

    auto = s.get("auto_trade_enabled", False)
    mode = "PAPER (safe)" if not auto else "LIVE *** REAL MONEY ***"
    print(f"  Mode        : {mode}")
    print(f"  Market open : {s.get('market_open')}")
    print(f"  Active index: {s.get('active_index', 'N/A')}")
    print(f"  Open pos    : {s.get('open_positions', 0)}")
    print(f"  Daily PnL   : Rs {p.get('daily_pnl', 0):.2f}")
    print(f"  Health      : {h.get('label', 'N/A')}")

    issues = h.get("issues", [])
    if issues:
        for iss in issues:
            print(f"  !! {iss}")

    trades = t.get("trades", [])
    print(f"  Trades today: {len(trades)}")
    if trades:
        print()
        print("  --- SIGNAL LOG ---")
        for tr in trades[-10:]:
            pnl    = tr.get("pnl", 0) or 0
            sign   = "WIN  " if pnl > 0 else ("LOSS " if pnl < 0 else "OPEN ")
            print(f"  {sign} | {tr.get('strategy',''):<6} {tr.get('direction',''):<3}"
                  f" | entry {tr.get('entry_price','?')} "
                  f"| exit {tr.get('exit_price','open')}"
                  f" | PnL Rs{pnl:.0f}")
    else:
        print("  No signals fired yet today.")

    print("=" * 60)


def main():
    print("Market Monitor started — polling every 5 minutes.")
    print("Auto-stops at 15:35 IST. Press Ctrl+C to stop early.")
    print()

    while True:
        now = datetime.now()
        # Stop after 15:35
        if now.hour > 15 or (now.hour == 15 and now.minute >= 35):
            snapshot()
            print()
            print("Market closed (15:35 IST). Monitor stopped.")
            print("Check signal_log.txt and update with today's results.")
            break

        snapshot()

        # Poll every 5 minutes
        time.sleep(300)


if __name__ == "__main__":
    main()
