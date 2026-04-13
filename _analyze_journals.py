import json, os, glob
from collections import Counter

journal_dir = 'journals'
sells = []

for f in sorted(glob.glob(journal_dir + '/journal_*.json')):
    try:
        with open(f) as jf:
            entries = json.load(jf)
        date = os.path.basename(f).replace('journal_','').replace('.json','')
        for e in entries:
            if e.get('event_type') != 'TRADE':
                continue
            d = e.get('details', {})
            if d.get('action') != 'SELL':
                continue
            sym = str(d.get('symbol') or d.get('customSymbol') or '')
            opt = 'CE' if 'CE' in sym else ('PE' if 'PE' in sym else '?')
            pnl = float(d.get('net_pnl') or d.get('gross_pnl') or d.get('pnl') or 0)
            sells.append({
                'date': date,
                'time': e.get('timestamp', '')[11:16],
                'opt': opt,
                'pnl': pnl,
                'reason': str(d.get('exit_reason') or d.get('reason') or ''),
                'source': str(d.get('source') or ''),
                'symbol': sym,
                'index': ('BANKNIFTY' if 'BANK' in sym else ('SENSEX' if 'SENSEX' in sym or 'BSX' in sym else 'NIFTY')),
                'hour': int(e.get('timestamp', '00:00')[11:13]) if len(e.get('timestamp','')) > 13 else -1,
            })
    except Exception as ex:
        print("Error reading", f, ex)

n = len(sells)
if n == 0:
    print("No closed trades found in journals.")
    raise SystemExit

wins   = [t for t in sells if t['pnl'] > 0]
losses = [t for t in sells if t['pnl'] <= 0]

print("=" * 55)
print("TRADE JOURNAL ANALYSIS")
print("=" * 55)
print(f"Total closed : {n}")
print(f"Wins         : {len(wins)}  ({round(len(wins)/n*100)}%)")
print(f"Losses       : {len(losses)}  ({round(len(losses)/n*100)}%)")

total  = sum(t['pnl'] for t in sells)
avg_w  = sum(t['pnl'] for t in wins)  / max(len(wins), 1)
avg_l  = sum(t['pnl'] for t in losses)/ max(len(losses), 1)
rr     = round(abs(avg_w / avg_l), 2) if avg_l != 0 else 'N/A'

print(f"Total PnL    : Rs {round(total):+,}")
print(f"Avg win      : Rs {round(avg_w):+,}")
print(f"Avg loss     : Rs {round(avg_l):+,}")
print(f"R:R achieved : {rr}  (need >0.6 to be profitable at 40% win)")
print()

# Exit reason breakdown
print("EXIT REASON BREAKDOWN:")
reason_counter = Counter(t['reason'][:40] or 'unknown' for t in sells)
for reason, cnt in reason_counter.most_common(10):
    sub = [t for t in sells if (t['reason'][:40] or 'unknown') == reason]
    sub_pnl = sum(t['pnl'] for t in sub)
    sub_wins = sum(1 for t in sub if t['pnl'] > 0)
    print(f"  {cnt:3}x  {reason:<40}  PnL Rs{sub_pnl:+6,.0f}  WR {round(sub_wins/cnt*100)}%")
print()

# Option type breakdown
print("OPTION TYPE BREAKDOWN:")
for otype in ['CE', 'PE']:
    sub = [t for t in sells if t['opt'] == otype]
    if sub:
        sw = sum(1 for t in sub if t['pnl'] > 0)
        print(f"  {otype}: {len(sub)} trades  WR {round(sw/len(sub)*100)}%  PnL Rs{sum(t['pnl'] for t in sub):+,.0f}")
print()

# Index breakdown
print("INDEX BREAKDOWN:")
for idx in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
    sub = [t for t in sells if t['index'] == idx]
    if sub:
        sw = sum(1 for t in sub if t['pnl'] > 0)
        print(f"  {idx:<10}: {len(sub)} trades  WR {round(sw/len(sub)*100)}%  PnL Rs{sum(t['pnl'] for t in sub):+,.0f}")
print()

# Time of day breakdown
print("HOUR-OF-DAY BREAKDOWN:")
hour_data = {}
for t in sells:
    h = t['hour']
    if h not in hour_data:
        hour_data[h] = []
    hour_data[h].append(t)
for h in sorted(hour_data.keys()):
    sub = hour_data[h]
    sw = sum(1 for t in sub if t['pnl'] > 0)
    pnl = sum(t['pnl'] for t in sub)
    print(f"  {h:02d}:xx  {len(sub):2} trades  WR {round(sw/len(sub)*100):3}%  PnL Rs{pnl:+6,.0f}")
print()

# Last 12 trades
print("LAST 12 CLOSED TRADES:")
for t in sells[-12:]:
    sign = 'W' if t['pnl'] > 0 else 'L'
    print(f"  {t['date']} {t['time']} {t['opt']:2} {t['index']:<10} {sign} Rs{t['pnl']:+6.0f}  {t['reason'][:35]}")
