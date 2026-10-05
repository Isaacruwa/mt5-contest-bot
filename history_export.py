import os, sys, time
from datetime import datetime, timezone, timedelta
from collections import Counter
import numpy as np
import MetaTrader5 as mt5

PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

def eet_offset(utc_dt):
    def nth_sun(y, m, n):
        d = datetime(y, m, 1, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    s = nth_sun(utc_dt.year, 3, 2).replace(hour=7); e = nth_sun(utc_dt.year, 11, 1).replace(hour=6)
    return 3 * 3600 if s <= utc_dt < e else 2 * 3600

def to_utc(server_epoch):
    guess = datetime.fromtimestamp(server_epoch - 2 * 3600, tz=timezone.utc)
    return datetime.fromtimestamp(server_epoch - eet_offset(guess), tz=timezone.utc)

def analyse(tag, login, pws, server):
    ok = False
    for pw in pws:
        if mt5.initialize(path=PATH, login=int(login), password=pw, server=server, timeout=90000):
            ok = True; break
        mt5.shutdown(); time.sleep(2)
    print(f"[{tag}] login ok: {ok}", flush=True)
    if not ok:
        print(f"[{tag}] last error: {mt5.last_error()}"); return
    ai = mt5.account_info()
    print(f"[{tag}] {ai.currency} balance {ai.balance:.2f} equity {ai.equity:.2f} leverage 1:{ai.leverage}")
    end = datetime.now(timezone.utc) + timedelta(days=2)
    deals = mt5.history_deals_get(datetime(2024, 1, 1), end) or []
    orders = mt5.history_orders_get(datetime(2024, 1, 1), end) or []
    print(f"[{tag}] deals: {len(deals)}, orders: {len(orders)}")
    sl_tp = {}
    for o in orders:
        if o.position_id and o.position_id not in sl_tp:
            sl_tp[o.position_id] = (o.sl > 0, o.tp > 0)
    pos = {}
    for d in deals:
        if d.type not in (0, 1):
            continue
        p = pos.setdefault(d.position_id, dict(ins=[], outs=[], sym=d.symbol, magic=d.magic))
        (p["ins"] if d.entry == 0 else p["outs"]).append(d)
    rows = []
    for pid, p in pos.items():
        if not p["ins"] or not p["outs"]:
            continue
        i0, o1 = p["ins"][0], p["outs"][-1]
        info = mt5.symbol_info(p["sym"])
        pt = info.point if info else 0.00001
        digits = info.digits if info else 5
        move = (o1.price - i0.price) * (1 if i0.type == 0 else -1)
        unit = move / (pt * 10) if p["sym"] != "XAUUSD" else move       # pips for FX, dollars for gold
        prof = sum(d.profit + d.commission + d.swap for d in p["ins"] + p["outs"])
        rows.append(dict(open=to_utc(i0.time), close=to_utc(o1.time), sym=p["sym"], dir="BUY" if i0.type == 0 else "SELL",
                         vol=sum(d.volume for d in p["ins"]), unit=unit, profit=prof, magic=p["magic"],
                         hold=o1.time - i0.time, sl=sl_tp.get(pid, (False, False))[0], tp=sl_tp.get(pid, (False, False))[1]))
    rows.sort(key=lambda r: r["open"])
    man = [r for r in rows if r["magic"] == 0]
    print(f"[{tag}] closed positions: {len(rows)} | manual (magic 0): {len(man)} | bot/test: {len(rows) - len(man)}")
    if not man:
        mt5.shutdown(); return
    pr = np.array([r["profit"] for r in man]); hold = np.array([r["hold"] for r in man])
    wins = pr > 0
    print(f"[{tag}] MANUAL: net {pr.sum():+.2f} | win rate {wins.mean()*100:.0f}% | avg win {pr[wins].mean() if wins.any() else 0:+.2f} | avg loss {pr[~wins].mean() if (~wins).any() else 0:+.2f}")
    print(f"[{tag}] hold time: median {np.median(hold):.0f}s | <1m {np.mean(hold < 60)*100:.0f}% | 1-5m {np.mean((hold>=60)&(hold<300))*100:.0f}% | 5-15m {np.mean((hold>=300)&(hold<900))*100:.0f}% | 15-60m {np.mean((hold>=900)&(hold<3600))*100:.0f}% | >1h {np.mean(hold>=3600)*100:.0f}%")
    print(f"[{tag}] symbols: {dict(Counter(r['sym'] for r in man))} | buys {sum(r['dir']=='BUY' for r in man)} sells {sum(r['dir']=='SELL' for r in man)}")
    print(f"[{tag}] volume lots: min {min(r['vol'] for r in man)} median {np.median([r['vol'] for r in man])} max {max(r['vol'] for r in man)} | had SL: {sum(r['sl'] for r in man)}/{len(man)} | had TP: {sum(r['tp'] for r in man)}/{len(man)}")
    days = Counter(r["open"].date() for r in man)
    print(f"[{tag}] trading days: {len(days)} | busiest day: {max(days.values())} trades | UTC hours: {dict(sorted(Counter(r['open'].hour for r in man).items()))}")
    print(f"[{tag}] first trade {man[0]['open']:%Y-%m-%d %H:%M} last {man[-1]['close']:%Y-%m-%d %H:%M}")
    print(f"[{tag}] --- trades (UTC open | sym dir lots | hold | move (pips; $ for gold) | profit | SL/TP) ---")
    for r in man[-70:]:
        print(f"{r['open']:%m-%d %H:%M} | {r['sym']:7s} {r['dir']:4s} {r['vol']:<5} | {int(r['hold']):>6}s | {r['unit']:+8.2f} | {r['profit']:+9.2f} | {'S' if r['sl'] else '-'}{'T' if r['tp'] else '-'}")
    mt5.shutdown()

accts = [("A", os.environ.get("MT5_LOGIN"), os.environ.get("MT5_PASSWORDS", ""), os.environ.get("MT5_SERVER")),
         ("B", os.environ.get("MT5_ALT_LOGIN"), os.environ.get("MT5_ALT_PASSWORDS", ""), os.environ.get("MT5_ALT_SERVER"))]
for tag, login, pws, server in accts:
    if login:
        analyse(tag, login, [p for p in pws.split("|") if p], server)
