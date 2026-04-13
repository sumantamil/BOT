import json, os

for date in ['2026-03-16','2026-03-17','2026-04-01','2026-04-02','2026-04-06','2026-04-07','2026-04-09']:
    jf = f'journals/journal_{date}.json'
    if not os.path.exists(jf):
        print(f'{date}: no journal')
        continue
    with open(jf, encoding='utf-8') as f:
        j = json.load(f)
    events = j if isinstance(j, list) else j.get('events', j.get('trades', []))
    trades = [e for e in events if e.get('event_type') == 'TRADE']
    analyses_near_trade = []
    print()
    print(f'=== {date}: {len(trades)} trade events ===')
    for e in trades:
        d   = e.get('details', {})
        ts  = e.get('timestamp','')
        out = e.get('outcome','')
        direction = e.get('direction','')
        action    = d.get('action','')
        strike    = d.get('strike','')
        premium   = d.get('premium', 0)
        qty       = d.get('quantity','')
        print(f'  {ts[11:16]}  {direction} {action}  strike={strike}  premium={premium:.1f}  qty={qty}')
        if out:
            print(f'         outcome: {out[:100]}')
    # Get RSI/price for key analyses
    analyses = [e for e in events if e.get('event_type') == 'ANALYSIS']
    # Show RSI range for the day
    rsi_vals = [e.get('details',{}).get('rsi',0) for e in analyses if e.get('details',{}).get('rsi')]
    if rsi_vals:
        print(f'  RSI range today: min={min(rsi_vals):.1f}  max={max(rsi_vals):.1f}  avg={sum(rsi_vals)/len(rsi_vals):.1f}')
    # Show prices
    prices = [e.get('details',{}).get('price',0) for e in analyses if e.get('details',{}).get('price',0) > 0]
    if prices:
        nifty_prices = [p for p in prices if 20000 < p < 27000]
        if nifty_prices:
            print(f'  NIFTY range: {min(nifty_prices):.0f} - {max(nifty_prices):.0f}')
