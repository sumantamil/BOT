"""Quick manual test for GTT and close_position flows."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from dotenv import load_dotenv
load_dotenv(".env")

from browser.zerodha import ZerodhaKite, OrderType, OptionType
from bot.index_config import NIFTY


async def main():
    z = ZerodhaKite()

    order_id = [2000]
    gtt_id = [5000]

    def fake_place(variety, exchange, tradingsymbol, transaction_type,
                   quantity, product, order_type):
        order_id[0] += 1
        print(f"  ORDER  -> {transaction_type} {tradingsymbol} x{quantity} [{exchange}]")
        return order_id[0]

    def fake_gtt(trigger_type, tradingsymbol, exchange, trigger_values,
                 last_price, orders):
        gtt_id[0] += 1
        print(f"  GTT    -> trigger={trigger_values} limit={orders[0]['price']:.1f} sym={tradingsymbol}")
        return {"trigger_id": gtt_id[0]}

    def fake_del_gtt(trigger_id):
        print(f"  CANCEL GTT id={trigger_id}")

    def fake_positions():
        return {
            "net": [{
                "tradingsymbol": "NIFTY2631724000CE",
                "quantity": 65,
                "average_price": 150.0,
                "last_price": 165.0,
                "pnl": 975.0,
                "exchange": "NFO",
                "product": "MIS",
            }],
            "day": [],
        }

    z.kite.place_order = fake_place
    z.kite.place_gtt = fake_gtt
    z.kite.delete_gtt = fake_del_gtt
    z.kite.positions = fake_positions

    z.set_active_index(NIFTY)

    print("=" * 55)
    print("TEST 1: place_gtt directly (SL=15% below Rs 150)")
    gid = await z.place_gtt("NIFTY2631724000CE", "NFO", 150.0, 15.0, 65)
    print("GTT id returned:", gid)
    assert gid == 5001, f"Expected 5001 got {gid}"
    print("PASS")

    print()
    print("TEST 2: cancel_gtt")
    ok = await z.cancel_gtt(gid)
    assert ok is True
    print("PASS")

    print()
    print("TEST 3: close_position (finds open position, places SELL)")
    r = await z.close_position("NIFTY2631724000CE")
    assert r.success, f"close_position failed: {r.message}"
    print("PASS:", r.message)

    print()
    print("TEST 4: BUY CE then SELL CE (round trip)")
    z.set_active_index(NIFTY)
    buy = await z.place_order(OptionType.CE, 24000, OrderType.BUY, 65)
    assert buy.success
    sel = await z.place_order(OptionType.CE, 24000, OrderType.SELL, 65)
    assert sel.success
    print("PASS: BUY then SELL CE round trip")

    print()
    print("TEST 5: BUY PE then SELL PE (round trip)")
    buy = await z.place_order(OptionType.PE, 24000, OrderType.BUY, 65)
    assert buy.success
    sel = await z.place_order(OptionType.PE, 24000, OrderType.SELL, 65)
    assert sel.success
    print("PASS: BUY then SELL PE round trip")

    print()
    print("=" * 55)
    print("All 5 tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
