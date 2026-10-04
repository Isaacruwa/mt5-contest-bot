import os
import sys
import MetaTrader5 as mt5

login = int(os.environ["MT5_LOGIN"])
server = os.environ["MT5_SERVER"]
pw = os.environ["MT5_PASSWORD"]
path = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# the first character of the password can look like letter O or zero: try both
cands = [pw]
if pw[:1] in ("O", "0"):
    cands.append(("0" if pw[0] == "O" else "O") + pw[1:])

ok = False
for i, p in enumerate(cands, 1):
    print(f"login attempt {i} of {len(cands)}", flush=True)
    if mt5.initialize(path=path, login=login, password=p, server=server, timeout=180000):
        ok = True
        print(f"login OK with password variant {i}", flush=True)
        break
    print("initialize failed:", mt5.last_error(), flush=True)
    mt5.shutdown()

if not ok:
    sys.exit(1)

ai = mt5.account_info()
print("currency:", ai.currency, "| leverage:", ai.leverage, "| balance:", ai.balance,
      "| equity:", ai.equity, "| trade_allowed:", ai.trade_allowed, flush=True)
for sym in ("EURUSD", "XAUUSD", "USDJPY", "GBPUSD"):
    mt5.symbol_select(sym, True)
    info = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)
    print(sym, "| tick:", (tick.bid, tick.ask) if tick else None,
          "| contract:", info.trade_contract_size if info else None,
          "| digits:", info.digits if info else None, flush=True)
rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_M5, 0, 5)
print("M5 candles fetched:", None if rates is None else len(rates), flush=True)
mt5.shutdown()
print("DONE", flush=True)
