import json
from collections import defaultdict

with open('paper_trades.json', encoding='utf-8') as f:
    pt = json.load(f)

trades = [t for t in pt.get('trades', []) if t.get('status') == 'CLOSED']
print(f'Total closed paper trades: {len(trades)}')

by_strategy  = defaultdict(lambda: {'w':0,'l':0,'pnl':0.0})
by_exit      = defaultdict(lambda: {'w':0,'l':0,'pnl':0.0})
by_direction = defaultdict(lambda: {'w':0,'l':0,'pnl':0.0})
by_index     = defaultdict(lambda: {'w':0,'l':0,'pnl':0.0})

for t in trades:
    pnl  = float(t.get('pnl') or 0)
    strat = t.get('strategy','?')
    ex   = t.get('exit_reason','?')
    opt  = t.get('option_type','?')
    idx  = t.get('index_name','?')
    key  = 'w' if pnl > 0 else 'l'
    for d,k in [(by_strategy, strat),(by_exit, ex),(by_direction, opt),(by_index, idx)]:
        d[k][key] += 1
        d[k]['pnl'] += pnl

def show(title, d):
    print()
    print('==', title, '==')
    for k, v in sorted(d.items(), key=lambda x: -(x[1]['w']+x[1]['l'])):
        t2 = v['w'] + v['l']
        wr = round(v['w']/t2*100) if t2 else 0
        print(f'  {k:28s} {t2:3d} trades  WR={wr:3d}%  PnL=Rs{v["pnl"]:+,.0f}')

show('BY STRATEGY', by_strategy)
show('BY EXIT REASON (sorted by count)', by_exit)
show('BY DIRECTION', by_direction)
show('BY INDEX', by_index)
