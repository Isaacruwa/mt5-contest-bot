import os, sys, time, glob
import MetaTrader5 as mt5

path = r"C:\Program Files\MetaTrader 5\terminal64.exe"
login = int(os.environ["MT5_LOGIN"]); server = os.environ["MT5_SERVER"]
pws = [p for p in os.environ["MT5_PASSWORDS"].split("|") if p]
print("python", sys.version.split()[0], "| MetaTrader5 package", mt5.__version__)

ok_pw = None
for i, pw in enumerate(pws):
    print(f"--- attempt {i + 1}/{len(pws)} (password length {len(pw)})", flush=True)
    ok = mt5.initialize(path=path, login=login, password=pw, server=server, timeout=120000)
    print("initialize:", ok, mt5.last_error(), flush=True)
    if ok:
        ok_pw = i + 1
        break
    mt5.shutdown()
    time.sleep(3)

if ok_pw is None:
    for f in sorted(glob.glob(r"C:\Program Files\MetaTrader 5\logs\*.log"))[-1:]:
        print("--- terminal log tail:", f)
        print(open(f, encoding="utf-16", errors="ignore").read()[-3000:])
    sys.exit(1)

print("LOGIN OK with password variant", ok_pw)
ai = mt5.account_info()
print("account:", ai.login, ai.server, ai.currency, "balance", ai.balance, "equity", ai.equity, "leverage", ai.leverage)
ti = mt5.terminal_info()
print("terminal connected:", ti.connected, "| company:", ti.company)
for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
    mt5.symbol_select(sym, True)
    i = mt5.symbol_info(sym); t = mt5.symbol_info_tick(sym)
    print(sym, "spec:", None if i is None else (i.trade_contract_size, i.currency_base, i.currency_profit,
          i.volume_min, i.volume_step, i.digits, i.trade_stops_level, i.filling_mode), "| tick:", t)
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, 5)
    print("   last M5 rates (time, close):", None if rates is None else [(int(r["time"]), float(r["close"])) for r in rates])
t = mt5.symbol_info_tick("EURUSD")
print("EURUSD tick time:", None if t is None else t.time, "| local epoch now:", int(time.time()))
mt5.shutdown()
print("SMOKE TEST DONE")
