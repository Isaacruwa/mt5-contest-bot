import os, sys
import MetaTrader5 as mt5

login = int(os.environ["MT5_LOGIN"])
server = os.environ["MT5_SERVER"]
path = r"C:\Program Files\MetaTrader 5\terminal64.exe"
print("MetaTrader5 package:", mt5.__version__, flush=True)

worked = False
for name in ("MT5_PASSWORD", "MT5_PASSWORD_ALT"):
    pw = os.environ.get(name)
    if not pw:
        continue
    ok = mt5.initialize(path=path, login=login, password=pw, server=server, timeout=120000)
    print(name, "initialize ->", ok, mt5.last_error(), flush=True)
    if ok:
        ai = mt5.account_info()
        print("ACCOUNT currency/balance/leverage/server/trade_allowed:",
              ai.currency, ai.balance, ai.leverage, ai.server, ai.trade_allowed, flush=True)
        for s in ("EURUSD", "XAUUSD", "USDJPY"):
            mt5.symbol_select(s, True)
            print(s, "tick:", mt5.symbol_info_tick(s), flush=True)
        rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_M5, 0, 5)
        print("M5 candles:", None if rates is None else len(rates), flush=True)
        print("WORKING_PASSWORD_VARIABLE:", name, flush=True)
        worked = True
        mt5.shutdown()
        break
    mt5.shutdown()
print("PROBE_RESULT:", "SUCCESS" if worked else "FAILED", flush=True)
