import json

with open('paper_trades.json', encoding='utf-8') as f:
    pt = json.load(f)

trades = [t for t in pt.get('trades', []) if t.get('status') == 'CLOSED']

print('=== ALL ORB TRADES ===')
for t in trades:
    if t.get('strategy') == 'ORB':
        ep  = float(t.get('entry_price') or 0)
        xp  = float(t.get('exit_price') or 0)
        pnl = float(t.get('pnl') or 0)
        pct = ((xp-ep)/ep*100) if ep > 0 else 0
        et  = str(t.get('entry_time',''))[:16]
        idx = t.get('index_name','')
        opt = t.get('option_type','')
        ex  = t.get('exit_reason','')
        print(f'  {et}  {idx} {opt}  entry={ep:.1f} exit={xp:.1f}  pct={pct:+.1f}%  exit_reason={ex}  pnl=Rs{pnl:+.0f}')

print()
print('=== TIME_STOP LOSERS ===')
for t in trades:
    ex  = t.get('exit_reason', '')
    pnl = float(t.get('pnl') or 0)
    if ex.startswith('TIME_STOP') and pnl < 0:
        ep  = float(t.get('entry_price') or 0)
        xp  = float(t.get('exit_price') or 0)
        pct = ((xp-ep)/ep*100) if ep > 0 else 0
        et  = str(t.get('entry_time',''))[:16]
        print(f'  {et}  {t.get("index_name","")} {t.get("option_type","")} {t.get("strategy","")}  pct={pct:+.1f}%  pnl=Rs{pnl:+.0f}  exit={ex}')

print()
print('=== LIVE PROFIT TRACKING CSV ===')
import csv
rows = []
with open('profit_tracking.csv', encoding='utf-8') as f:
    for row in csv.DictReader(f):
        rows.append(row)
for r in rows:
    print(f'  {r["date"]}  PnL=Rs{float(r["gross_pnl"]):+,.0f}  trades={r.get("total_trades","?")}  wins={r.get("winning_trades","?")}  losses={r.get("losing_trades","?")}')
