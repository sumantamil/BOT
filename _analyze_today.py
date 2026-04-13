import json, os

today = '2026-04-09'

# ── Journal trades ──────────────────────────────────────────────
jf = f'journals/journal_{today}.json'
with open(jf, encoding='utf-8') as f:
    journal = json.load(f)

trades   = [e for e in journal if e.get('event_type') == 'TRADE']
analyses = [e for e in journal if e.get('event_type') == 'ANALYSIS']

print('='*60)
print(f'TODAY ({today}) — TRADE SUMMARY')
print('='*60)
print(f'Total TRADE events in journal : {len(trades)}')
print()

for i, t in enumerate(trades, 1):
    ts   = t['timestamp'][11:16]
    d    = t['details']
    print(f'Trade {i}: {ts}  {t["direction"]} {d.get("action","?")}  strike={d.get("strike")}  premium={d.get("premium",0):.1f}  qty={d.get("quantity")}')
    print(f'  outcome: {t.get("outcome","")}')
    # Find analysis events within 3 min before this trade
    thresh = t['timestamp'][:16]
    nearby = [
        e for e in analyses
        if e['timestamp'][:16] <= thresh and e['timestamp'][:16] >= t['timestamp'][:13]
    ]
    if nearby:
        e = nearby[-1]
        nd = e['details']
        print(f'  last signal before: dir={e["direction"]} str={nd.get("strength")} price={nd.get("price",0):.0f} rsi={nd.get("rsi",0):.1f} @ {e["timestamp"][11:16]}')
    print()

# ── Paper trades ─────────────────────────────────────────────────
print('='*60)
print('PAPER TRADES')
print('='*60)
with open('paper_trades.json', encoding='utf-8') as f:
    pt = json.load(f)

today_pt = [t for t in pt.get('trades', []) if str(t.get('entry_time', '')).startswith(today)]
print(f'Paper trades today: {len(today_pt)}')
for t in today_pt:
    print(json.dumps({k: t.get(k) for k in ['entry_time','index_name','option_type','strategy','entry_price','exit_price','pnl','exit_reason','status']}, default=str))

# Recent paper trades (last 10) regardless of date
print()
print('Last 5 paper trades (any date):')
for t in pt.get('trades', [])[-5:]:
    print(f"  {t.get('entry_time','?')[:16]} {t.get('index_name')} {t.get('option_type')} str={t.get('strategy')} pnl={t.get('pnl')} exit={t.get('exit_reason')} status={t.get('status')}")

# ── Signal log tail ───────────────────────────────────────────────
print()
print('='*60)
print('SIGNAL LOG (last 30 lines)')
print('='*60)
if os.path.exists('signal_log.txt'):
    with open('signal_log.txt', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    today_lines = [l for l in lines if today in l]
    for l in today_lines[-30:]:
        print(l.rstrip())
