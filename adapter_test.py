import asyncio, os, sys, time
import MetaTrader5 as mt5
import run_mt5, bot


async def main():
    pw = os.environ["MT5_PASSWORDS"].split("|")[0]
    ok = mt5.initialize(path=run_mt5.TERMINAL, login=int(os.environ["MT5_LOGIN"]), password=pw,
                        server=os.environ["MT5_SERVER"], timeout=120000)
    print("initialize:", ok, mt5.last_error(), flush=True)
    if not ok:
        sys.exit(1)
    ad = run_mt5.Mt5Adapter()
    print("server offset (hours):", ad.offset() / 3600)
    print("account:", {k: v for k, v in (await ad.get_account_information()).items() if k != "login"})
    print("positions:", await ad.get_positions())
    for sym in bot.SYMBOL_LIST:
        spec = await ad.get_symbol_specification(sym)
        try:
            price = await ad.get_symbol_price(sym)
        except Exception as e:
            price = f"ERR {e}"
        c = await ad.get_historical_candles(sym, "5m", None, 1000)
        print(sym, spec, price, "| candles:", len(c), "last open (UTC):", c[-1]["time"] if c else None, flush=True)

    deadline = time.time() + 150
    data = {}
    for sym in bot.SYMBOL_LIST:
        data[sym] = await bot.fetch_history(ad, sym, 15000, deadline)
        cs = data[sym]
        print(sym, "history:", len(cs), cs[0]["time"] if cs else None, "->", cs[-1]["time"] if cs else None, flush=True)

    allT = []
    for sym, cs in data.items():
        if len(cs) < bot.WARMUP + 50:
            print(sym, "not enough history")
            continue
        tr = bot.simulate(cs, 0.12)
        allT += tr
        print(bot.fmt(sym, bot.stats([r for _, r in tr])))
    if allT:
        allT.sort()
        cut = allT[0][0] + (allT[-1][0] - allT[0][0]) * 0.7
        print(bot.fmt("ALL", bot.stats([r for _, r in allT])))
        print(bot.fmt("in-sample", bot.stats([r for t, r in allT if t <= cut])))
        oos = bot.stats([r for t, r in allT if t > cut])
        print(bot.fmt("out-of-sample", oos))
        print("verdict:", bot.verdict(oos))
    mt5.shutdown()
    print("ADAPTER TEST DONE")


asyncio.run(main())
