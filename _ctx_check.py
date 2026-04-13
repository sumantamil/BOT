import json

jf = 'journals/journal_2026-04-09.json'
with open(jf, encoding='utf-8') as f:
    j = json.load(f)

print('=== NIFTY price/RSI/direction at open (9:15-10:00) ===')
for e in j:
    ts = e.get('timestamp', '')
    if e.get('event_type') != 'ANALYSIS':
        continue
    if not ('09:1' <= ts[11:14] <= '10:0'):
        continue
    d = e['details']
    price = d.get('price', 0)
    if 20000 < price < 27000:
        print(ts[11:16], ' ', e['direction'], ' str=', d.get('strength', 0),
              ' rsi=', round(d.get('rsi', 0), 1), ' price=', round(price))

print()
print('=== SENSEX price/RSI at open ===')
for e in j:
    ts = e.get('timestamp', '')
    if e.get('event_type') != 'ANALYSIS':
        continue
    if not ('09:1' <= ts[11:14] <= '10:0'):
        continue
    d = e['details']
    price = d.get('price', 0)
    if 70000 < price < 85000:
        print(ts[11:16], ' ', e['direction'], ' str=', d.get('strength', 0),
              ' rsi=', round(d.get('rsi', 0), 1), ' price=', round(price))
