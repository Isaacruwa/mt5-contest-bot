import os, sys, time, json, csv, gzip
import numpy as np
import MetaTrader5 as mt5

SYMS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "XAUUSD"]
TFS = {"M5": (mt5.TIMEFRAME_M5, 150000), "M1": (mt5.TIMEFRAME_M1, 150000)}
PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

pw = os.environ["MT5_PASSWORDS"].split("|")[0]
ok = mt5.initialize(path=PATH, login=int(os.environ["MT5_LOGIN"]), password=pw,
                    server=os.environ["MT5_SERVER"], timeout=120000)
print("initialize:", ok, mt5.last_error(), flush=True)
if not ok:
    sys.exit(1)
print("terminal maxbars:", mt5.terminal_info().maxbars, flush=True)

os.makedirs("data", exist_ok=True)
meta = {"symbols": {}, "exported_at": int(time.time())}

def fresh(sym):
    r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 2)
    return r is not None and len(r) and abs(time.time() - int(r[-1]["time"])) < 5 * 86400

for sym in SYMS:
    mt5.symbol_select(sym, True)
    t0 = time.time()
    while time.time() - t0 < 30 and not fresh(sym):
        time.sleep(1)
    info = mt5.symbol_info(sym)
    if info is None:
        print(sym, "no symbol info"); continue
    tick = mt5.symbol_info_tick(sym)
    meta["symbols"][sym] = dict(point=info.point, digits=info.digits, contract=info.trade_contract_size,
                                base=info.currency_base, profit=info.currency_profit,
                                tick_size=info.trade_tick_size, tick_value=info.trade_tick_value,
                                vol_min=info.volume_min, vol_step=info.volume_step,
                                last_tick_time=(tick.time if tick else 0))
    for name, (tf, maxn) in TFS.items():
        # let the terminal download history: ask repeatedly until the count stops growing
        last = -1
        for _ in range(8):
            r = mt5.copy_rates_from_pos(sym, tf, 0, 50000)
            n = 0 if r is None else len(r)
            if n == last:
                break
            last = n
            time.sleep(3)
        chunks, pos = [], 0
        while pos < maxn:
            n = min(50000, maxn - pos)
            r = mt5.copy_rates_from_pos(sym, tf, pos, n)
            if r is None or len(r) == 0:
                break
            chunks.append(r)
            if len(r) < n:
                break
            pos += n
        if not chunks:
            print(sym, name, "no data"); continue
        arr = np.concatenate(chunks[::-1])
        arr = arr[np.argsort(arr["time"], kind="stable")]
        _, idx = np.unique(arr["time"], return_index=True)
        arr = arr[idx]
        fn = f"data/{sym}_{name}.csv.gz"
        with gzip.open(fn, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread"])
            for x in arr:
                w.writerow([int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]),
                            float(x["close"]), int(x["tick_volume"]), int(x["spread"])])
        print(f"{sym} {name}: {len(arr)} bars, {time.strftime('%Y-%m-%d', time.gmtime(int(arr['time'][0])))} -> "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(int(arr['time'][-1])))}, "
              f"median spread {float(np.median(arr['spread']))} pts", flush=True)

with open("data/meta.json", "w") as f:
    json.dump(meta, f)
mt5.shutdown()
print("EXPORT DONE")
