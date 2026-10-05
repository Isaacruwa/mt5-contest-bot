"""
Runs the Telegram bot from bot.py against a LOCAL MetaTrader 5 terminal (Wine, inside GitHub Actions)
instead of MetaApi. Start with:   wine 'C:\\Python311\\python.exe' run_mt5.py
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

import bot

log = logging.getLogger("bot")
TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
TFS = {"1m": mt5.TIMEFRAME_M1, "5m": mt5.TIMEFRAME_M5, "15m": mt5.TIMEFRAME_M15, "1h": mt5.TIMEFRAME_H1}
ENTRY = {0: "DEAL_ENTRY_IN", 1: "DEAL_ENTRY_OUT", 2: "DEAL_ENTRY_INOUT", 3: "DEAL_ENTRY_OUT"}


# ----------------------------------------------------------------- broker server clock
def _nth_sunday(year, month, n):
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def us_dst(utc):
    start = _nth_sunday(utc.year, 3, 2).replace(hour=7)
    end = _nth_sunday(utc.year, 11, 1).replace(hour=6)
    return start <= utc < end


class Mt5Adapter:
    """Same async methods bot.py used on MetaApi, backed by the MetaTrader5 package."""

    def __init__(self):
        self.synced = set()

    # MT5 candle/tick times are broker "server time" (usually EET: UTC+2, UTC+3 in US summer time)
    def offset(self):
        now = time.time()
        for sym in (bot.SYMBOL_LIST or ["EURUSD"]):
            t = mt5.symbol_info_tick(sym)
            if t and t.time:
                diff = t.time - now
                k = round(diff / 3600)
                if 0 <= k <= 4 and abs(diff - k * 3600) < 300:      # a fresh tick: measured offset
                    return k * 3600
        return 3600 * (3 if us_dst(datetime.now(timezone.utc)) else 2)

    def _ensure(self, sym):
        """Subscribe to the symbol and wait (briefly) until its history is up to date."""
        if sym in self.synced:
            return
        mt5.symbol_select(sym, True)
        deadline = time.time() + 25
        while time.time() < deadline:
            r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 2)
            if r is not None and len(r) and abs(time.time() - int(r[-1]["time"])) < 5 * 86400:
                break
            time.sleep(1)
        self.synced.add(sym)

    async def get_account_information(self):
        ai = mt5.account_info()
        if ai is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        return dict(balance=ai.balance, equity=ai.equity, freeMargin=ai.margin_free,
                    leverage=ai.leverage, currency=ai.currency, login=ai.login)

    async def get_positions(self):
        ps = mt5.positions_get() or []
        off = self.offset()
        return [dict(id=p.ticket, symbol=p.symbol, type=p.type, volume=p.volume, openPrice=p.price_open,
                     currentPrice=p.price_current, stopLoss=p.sl, takeProfit=p.tp, profit=p.profit,
                     magic=p.magic, openTime=datetime.fromtimestamp(int(p.time) - off, tz=timezone.utc))
                for p in ps]

    async def get_symbol_specification(self, sym):
        self._ensure(sym)
        i = mt5.symbol_info(sym)
        if i is None:
            raise RuntimeError(f"unknown symbol {sym}: {mt5.last_error()}")
        return dict(contractSize=i.trade_contract_size, baseCurrency=i.currency_base,
                    profitCurrency=i.currency_profit, volumeStep=i.volume_step, minVolume=i.volume_min,
                    maxVolume=i.volume_max, digits=i.digits, stopsLevel=i.trade_stops_level)

    async def get_symbol_price(self, sym):
        t = mt5.symbol_info_tick(sym)
        if t is None or t.bid <= 0 or t.ask <= 0:
            raise RuntimeError(f"no price for {sym}")
        return dict(bid=t.bid, ask=t.ask)

    def _filling(self, sym):
        fm = mt5.symbol_info(sym).filling_mode
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def _send(self, req):
        r = mt5.order_send(req)
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            detail = None if r is None else (r.retcode, r.comment)
            raise RuntimeError(f"order rejected: {detail} {mt5.last_error()}")
        return r

    def _market(self, sym, side, lots, sl, tp, opts):
        t = mt5.symbol_info_tick(sym)
        buy = side == "buy"
        return self._send(dict(
            action=mt5.TRADE_ACTION_DEAL, symbol=sym, volume=float(lots),
            type=mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
            price=t.ask if buy else t.bid, sl=float(sl), tp=float(tp), deviation=20,
            magic=int((opts or {}).get("magic", 0)), comment=str((opts or {}).get("comment", ""))[:30],
            type_time=mt5.ORDER_TIME_GTC, type_filling=self._filling(sym)))

    async def create_market_buy_order(self, sym, lots, sl, tp, opts=None):
        return self._market(sym, "buy", lots, sl, tp, opts)

    async def create_market_sell_order(self, sym, lots, sl, tp, opts=None):
        return self._market(sym, "sell", lots, sl, tp, opts)

    async def close_position(self, pid):
        ps = mt5.positions_get(ticket=int(pid))
        if not ps:
            return
        p = ps[0]
        t = mt5.symbol_info_tick(p.symbol)
        is_buy = p.type == 0
        self._send(dict(
            action=mt5.TRADE_ACTION_DEAL, symbol=p.symbol, volume=p.volume, position=p.ticket,
            type=mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            price=t.bid if is_buy else t.ask, deviation=20, magic=p.magic, comment="close",
            type_time=mt5.ORDER_TIME_GTC, type_filling=self._filling(p.symbol)))

    async def get_deals_by_time_range(self, a, b):
        off = self.offset()
        a_s = datetime.utcfromtimestamp(a.timestamp() + off)
        b_s = datetime.utcfromtimestamp(b.timestamp() + off)
        ds = mt5.history_deals_get(a_s, b_s) or []
        return {"deals": [dict(entryType=ENTRY.get(d.entry, ""), symbol=d.symbol, profit=d.profit,
                               commission=d.commission, swap=d.swap, magic=d.magic) for d in ds]}

    async def get_historical_candles(self, sym, timeframe, start, limit):
        self._ensure(sym)
        off = self.offset()
        if start is None:
            rates = mt5.copy_rates_from_pos(sym, TFS[timeframe], 0, limit)
        else:
            st = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            rates = mt5.copy_rates_from(sym, TFS[timeframe], datetime.utcfromtimestamp(st.timestamp() + off), limit)
        if rates is None:
            raise RuntimeError(f"no candles for {sym}: {mt5.last_error()}")
        return [dict(time=datetime.fromtimestamp(int(r["time"]) - off, tz=timezone.utc),
                     open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                     close=float(r["close"])) for r in rates]

    async def close(self):
        mt5.shutdown()


# ----------------------------------------------------------------- patched Bot methods
async def ensure_connected(self):
    S = self.S
    creds = S.get("mt5")
    if self.conn or not creds or time.time() < self.next_try:
        return
    self.next_try = time.time() + 60
    try:
        ok = await asyncio.to_thread(
            mt5.initialize, path=TERMINAL, login=int(creds["login"]), password=creds["password"],
            server=creds["server"], timeout=120000)
        if not ok:
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        ai = mt5.account_info()
        if ai is None or ai.login != int(creds["login"]):
            raise RuntimeError("MT5 is logged into a different account than the one you gave")
        self.conn = self.account = Mt5Adapter()
        ti = mt5.terminal_info()
        if ti is not None and not ti.trade_allowed and not S.get("algo_warned"):
            S["algo_warned"] = True
            await self.say("⚠️ MT5 reports algorithmic trading is switched off, so orders will be rejected.")
        if not S.get("ready_told"):
            S["ready_told"] = True
            await self.say(f"✅ Logged in. Balance {ai.balance:,.2f} {ai.currency}, leverage 1:{ai.leverage}.\n"
                           f"Initial balance for the loss limits is set to {bot.INITIAL_BALANCE:,.0f} "
                           f"(change with /set INITIAL_BALANCE 10000). Profile: {bot.PROFILE}.\n"
                           f"Next: /backtest, then /go.")
    except Exception as e:
        log.warning("connect failed: %s", e)
        mt5.shutdown()
        if time.time() - S.get("conn_warn_ts", 0) > 3600:
            S["conn_warn_ts"] = time.time()
            await self.say("❌ Couldn't log in to MT5. Check the login, password and server spelling "
                           "(letter O vs zero!), then send /disconnect and /connect again. "
                           f"({str(e)[:160]})")


async def do_connect(self, login, password, server):
    S = self.S
    if S.get("mt5"):
        await self.say("An account is already linked. Send /disconnect first.")
        return
    if not str(login).isdigit():
        await self.say("The login must be only digits, e.g. 113587722")
        return
    S["mt5"] = {"login": str(login), "password": password, "server": server}
    S["account_id"] = "local"            # marker used by the shared command handlers
    S["enabled"] = False
    S["risk"] = {}
    S["start_equity"] = None
    S["ready_told"] = False
    bot.save_state(S)
    self.next_try = 0
    await self.say("🔗 Details saved. Logging in to MT5 now. This can take a minute or two.")


async def do_disconnect(self):
    S = self.S
    if not S.get("mt5"):
        await self.say("No account linked.")
        return
    try:
        if self.conn:
            await self.close_all(await self.my_positions())
    except Exception as e:
        log.warning("close on disconnect failed: %s", e)
    try:
        mt5.shutdown()
    except Exception:
        pass
    S.update(mt5=None, account_id=None, enabled=False, risk={}, start_equity=None, last_bar={}, since={},
             ready_told=False)
    self.conn = self.account = None
    await self.say("🔌 Account unlinked and trading stopped.")


bot.Bot.ensure_connected = ensure_connected
bot.Bot.do_connect = do_connect
bot.Bot.do_disconnect = do_disconnect

if __name__ == "__main__":
    asyncio.run(bot.run_window())
