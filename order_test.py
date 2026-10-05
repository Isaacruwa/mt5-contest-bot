import asyncio, os, sys, time
import MetaTrader5 as mt5
import run_mt5


async def main():
    pw = os.environ["MT5_PASSWORDS"].split("|")[0]
    ok = mt5.initialize(path=run_mt5.TERMINAL, login=int(os.environ["MT5_LOGIN"]), password=pw,
                        server=os.environ["MT5_SERVER"], timeout=120000)
    print("initialize:", ok, mt5.last_error(), flush=True)
    if not ok:
        sys.exit(1)
    ti, ai = mt5.terminal_info(), mt5.account_info()
    print("terminal trade_allowed:", ti.trade_allowed, "| tradeapi_disabled:", ti.tradeapi_disabled,
          "| account trade_allowed:", ai.trade_allowed, "| trade_expert:", ai.trade_expert, flush=True)
    ad = run_mt5.Mt5Adapter()
    try:
        price = await ad.get_symbol_price("EURUSD")
        print("EURUSD price:", price, flush=True)
        sl, tp = round(price["ask"] - 0.0020, 5), round(price["ask"] + 0.0020, 5)
        r = await ad.create_market_buy_order("EURUSD", 0.01, sl, tp, {"magic": 99999, "comment": "ci-test"})
        print("ORDER OK:", r.retcode, r.order, r.deal, flush=True)
        time.sleep(2)
        mine = [p for p in await ad.get_positions() if p["magic"] == 99999]
        print("open test positions:", mine, flush=True)
        for p in mine:
            await ad.close_position(p["id"])
        time.sleep(2)
        print("after close:", [p for p in await ad.get_positions() if p["magic"] == 99999], flush=True)
        print("ORDER ROUND TRIP DONE")
    except Exception as e:
        print("ORDER TEST FAILED:", e)
        sys.exit(2)
    finally:
        mt5.shutdown()


asyncio.run(main())
