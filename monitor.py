"""Live position monitor — runs until 15:30 market close."""
import sys, asyncio, datetime
sys.path.insert(0, 'c:/Users/sundlnu/Sundhar/nifty-trading-bot')
from dotenv import load_dotenv
load_dotenv('c:/Users/sundlnu/Sundhar/nifty-trading-bot/.env')


async def monitor():
    from browser.dhan import DhanBroker
    from bot.index_config import NIFTY
    kite = DhanBroker()
    kite.set_active_index(NIFTY)

    market_close = datetime.datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    print(f"=== Live Monitor started. Market closes at {market_close.strftime('%H:%M')} ===\n")

    while True:
        now = datetime.datetime.now()
        if now >= market_close:
            print(f"\n[{now.strftime('%H:%M:%S')}] Market closed. Printing final summary...")
            break

        try:
            raw = await kite.get_kite_positions()
            day_pos = raw.get('day', []) if isinstance(raw, dict) else []
            open_pos = [p for p in day_pos if (p.get('quantity', 0) or 0) != 0]
            closed_pos = [p for p in day_pos if (p.get('quantity', 0) or 0) == 0 and (p.get('buy_quantity', 0) or 0) > 0]
            total_pnl = sum(float(p.get('pnl', 0) or 0) for p in day_pos)
        except Exception as e:
            print(f"[{now.strftime('%H:%M:%S')}] API error: {e}")
            await asyncio.sleep(30)
            continue

        ts = now.strftime('%H:%M:%S')
        if open_pos:
            for p in open_pos:
                sym  = p.get('tradingsymbol', '?')
                qty  = p.get('quantity', 0)
                avg  = float(p.get('average_price', 0) or 0)
                ltp  = float(p.get('last_price', 0) or 0)
                unr  = float(p.get('unrealised', 0) or 0)
                pct  = ((ltp - avg) / avg * 100) if avg > 0 else 0
                sl   = avg * 0.85
                t1   = avg * 1.15
                t2   = avg * 1.30
                flag = ''
                if   ltp >= t2: flag = '  *** T2 HIT (+30%) ***'
                elif ltp >= t1: flag = '  *** T1 HIT (+15%) ***'
                elif ltp <= sl: flag = '  *** SL HIT (-15%) ***'
                print(
                    f"[{ts}] OPEN  {sym} x{qty} | "
                    f"Avg:Rs.{avg:.2f}  LTP:Rs.{ltp:.2f}  {pct:+.1f}% | "
                    f"Unreal:Rs.{unr:+.0f} | DayPnL:Rs.{total_pnl:+.0f}{flag}"
                )
        else:
            closed_syms = ', '.join(p.get('tradingsymbol', '?') for p in closed_pos) or 'none'
            print(f"[{ts}] NO OPEN POSITIONS  |  Closed today: {closed_syms}  |  DayPnL:Rs.{total_pnl:+.0f}")

        await asyncio.sleep(30)

    # Final summary
    print()
    try:
        raw = await kite.get_kite_positions()
        day_pos = raw.get('day', []) if isinstance(raw, dict) else []
        print("=" * 50)
        print("  FINAL DAY SUMMARY")
        print("=" * 50)
        for p in day_pos:
            if (p.get('buy_quantity', 0) or 0) > 0:
                sym   = p.get('tradingsymbol', '?')
                buy_q = p.get('buy_quantity', 0)
                sell_q = p.get('sell_quantity', 0)
                avg   = float(p.get('average_price', 0) or 0)
                pnl   = float(p.get('pnl', 0) or 0)
                net_q = p.get('quantity', 0)
                status = 'OPEN' if (net_q or 0) != 0 else 'CLOSED'
                print(f"  [{status}] {sym}: buy={buy_q} sell={sell_q} avg=Rs.{avg:.2f} PnL=Rs.{pnl:+.2f}")
        total = sum(float(p.get('pnl', 0) or 0) for p in day_pos)
        print(f"\n  TOTAL DAY PnL: Rs.{total:+.2f}")
    except Exception as e:
        print(f"Final summary failed: {e}")


asyncio.run(monitor())
