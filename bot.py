"""
MT5 contest test bot  (MetaApi cloud + Telegram, runs on GitHub Actions)

Every ~5 minutes GitHub starts this script. It stays alive for ~4 minutes (RUN_SECONDS),
trades and answers Telegram, saves its state to state.json (the workflow caches it), and exits.

Everything is controlled from Telegram:
  /start        first person to send it becomes the owner
  /connect LOGIN PASSWORD SERVER   (message is deleted right away)
  /backtest     validate the strategy on recent history
  /go           start trading     /stop   close everything and pause
  /status       /config     /set NAME VALUE     /disconnect     /help
Demo/test use only.
"""
import asyncio
import json
import math
import os
import re
import sys
import time
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


# ----------------------------------------------------------------- config
def env(name, default=None, cast=str):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return cast(v.strip())


METAAPI_TOKEN = env("METAAPI_TOKEN")
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
STATE_FILE = env("STATE_FILE", "state.json")
RUN_SECONDS = env("RUN_SECONDS", 235, int)
POLL_SECONDS = env("POLL_SECONDS", 10, int)
STATS_MINUTES = env("STATS_MINUTES", 60, int)
MAGIC = env("MAGIC", 55510, int)
NEWS_BEFORE = 4.0       # minutes before a high-impact event: stop entries, flatten
NEWS_AFTER = 3.0        # minutes after: still blocked

# daily loss %, total loss %, block trading around high-impact news
PROFILES = {
    "contest":      dict(daily=5.0, total=10.0, news=True),
    "high_stakes":  dict(daily=5.0, total=10.0, news=True),
    "hyper_growth": dict(daily=3.0, total=6.0,  news=False),
}
TF_MAP = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

# name -> (default, type). Can be changed from Telegram with /set NAME VALUE
TUNABLES = {
    "PROFILE": ("contest", str),
    "INITIAL_BALANCE": (10000.0, float),
    "DAY_START_REF": (0.0, float),          # >0: day-start balance to use on the very first day
    "SYMBOLS": ("EURUSD,GBPUSD,USDJPY,XAUUSD", str),
    "TIMEFRAME": ("5m", str),
    "FAST_EMA": (50, int),
    "SLOW_EMA": (200, int),
    "RSI_N": (7, int),
    "RSI_BUY": (30.0, float),
    "RSI_SELL": (70.0, float),
    "ATR_N": (14, int),
    "TP_ATR": (0.8, float),
    "SL_ATR": (1.6, float),
    "COOLDOWN": (2, int),
    "MAX_HOLD": (60, int),
    "MAX_POS_SYMBOL": (2, int),
    "MAX_POS_TOTAL": (4, int),
    "BUDGET_USE": (60.0, float),            # % of distance to nearest limit usable as open risk
    "PER_TRADE": (15.0, float),             # % of that budget risked per trade
    "EMERGENCY_BUF": (0.7, float),          # % of initial balance
    "GIVE_BACK": (40.0, float),
    "MIN_DAY_PROFIT": (1.0, float),
    "MAX_MARGIN": (60.0, float),
    "MAX_SPREAD_ATR": (0.25, float),
    "SESSION_START": (7, int),              # UTC
    "SESSION_END": (20, int),
    "DAY_RESET_HOUR": (0, int),             # UTC hour when the firm's trading day resets
    "NEWS_FILTER": ("auto", str),           # auto | on | off
}
for _k, (_d, _t) in TUNABLES.items():
    globals()[_k] = env(_k, _d, _t)


def refresh_derived():
    global P, SYMBOL_LIST, TF_SECONDS, WARMUP, NEWS_ON
    P = PROFILES[PROFILE]
    SYMBOL_LIST = [s.strip() for s in SYMBOLS.split(",") if s.strip()]
    TF_SECONDS = TF_MAP[TIMEFRAME]
    WARMUP = SLOW_EMA + 5
    NEWS_ON = (NEWS_FILTER == "on") or (NEWS_FILTER == "auto" and P["news"])


refresh_derived()


def apply_overrides(cfg):
    for k, v in (cfg or {}).items():
        if k in TUNABLES:
            globals()[k] = TUNABLES[k][1](v)
    refresh_derived()


def validate_setting(key, raw):
    key = key.upper()
    if key not in TUNABLES:
        return None, None, "Unknown setting. Send /config to see all names."
    try:
        val = TUNABLES[key][1](raw)
    except Exception:
        return None, None, f"{key} needs a {TUNABLES[key][1].__name__} value."
    if key == "PROFILE" and val not in PROFILES:
        return None, None, "PROFILE must be one of: " + ", ".join(PROFILES)
    if key == "TIMEFRAME" and val not in TF_MAP:
        return None, None, "TIMEFRAME must be one of: " + ", ".join(TF_MAP)
    if key == "NEWS_FILTER" and val not in ("auto", "on", "off"):
        return None, None, "NEWS_FILTER must be auto, on or off."
    if key == "SYMBOLS":
        val = ",".join(s.strip().upper() for s in str(val).replace(" ", ",").split(",") if s.strip())
        if not val:
            return None, None, "Give at least one symbol, e.g. EURUSD,XAUUSD"
    return key, val, None


# ----------------------------------------------------------------- state (saved between runs)
def load_state():
    base = dict(owner=None, account_id=None, tg_offset=0, enabled=False, cfg={}, risk={},
                last_bar={}, since={}, start_equity=None, last_report=0.0,
                news_warned=False, backtest=False)
    try:
        with open(STATE_FILE) as f:
            base.update(json.load(f))
    except Exception:
        pass
    return base


def save_state(S):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(S, f)
    os.replace(tmp, STATE_FILE)


# ----------------------------------------------------------------- helpers
def to_dt(x):
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def in_session(dt):
    h = dt.hour
    if SESSION_START <= SESSION_END:
        return SESSION_START <= h < SESSION_END
    return h >= SESSION_START or h < SESSION_END


# ----------------------------------------------------------------- indicators
def ema(values, n):
    k = 2.0 / (n + 1)
    out, e = [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(h, l, c, n):
    out = [None] * len(c)
    if len(c) < n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                          for i in range(1, len(c))]
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def indicators(candles):
    c = [x["close"] for x in candles]
    h = [x["high"] for x in candles]
    l = [x["low"] for x in candles]
    return dict(f=ema(c, FAST_EMA), s=ema(c, SLOW_EMA), r=rsi(c, RSI_N), a=atr(h, l, c, ATR_N))


def signal_at(A, i):
    """1 = buy, -1 = sell, 0 = nothing. Uses candle i (must be closed)."""
    f, s, r, a = A["f"][i], A["s"][i], A["r"][i], A["a"][i]
    if i < WARMUP or r is None or a is None or a <= 0:
        return 0, a
    if f > s and r < RSI_BUY:
        return 1, a
    if f < s and r > RSI_SELL:
        return -1, a
    return 0, a


# ----------------------------------------------------------------- sizing helpers
def loss_per_lot(spec, price, dist, acct_ccy):
    """Account-currency loss for 1 lot if price moves `dist`. None if currency not supported."""
    cs = spec.get("contractSize", 1) or 1
    base, prof = spec.get("baseCurrency"), spec.get("profitCurrency")
    loss = dist * cs
    if prof == acct_ccy:
        return loss
    if base == acct_ccy:
        return loss / price
    return None


def margin_per_lot(spec, price, leverage, acct_ccy):
    cs = spec.get("contractSize", 1) or 1
    if spec.get("baseCurrency") == acct_ccy:
        return cs / leverage
    return cs * price / leverage


# ----------------------------------------------------------------- risk
class Risk:
    def __init__(self, d=None):
        d = d or {}
        self.key = d.get("key")                      # trading-day id (string)
        self.day_start = d.get("day_start", 0.0)
        self.peak = d.get("peak", 0.0)
        self.day_locked = d.get("day_locked", False)
        self.total_locked = d.get("total_locked", False)
        self.emergency = False

    def dump(self):
        return dict(key=self.key, day_start=self.day_start, peak=self.peak,
                    day_locked=self.day_locked, total_locked=self.total_locked)

    def locked(self):
        return self.day_locked or self.total_locked

    def daily_floor(self):
        return self.day_start * (1 - P["daily"] / 100)

    def total_floor(self):
        return INITIAL_BALANCE * (1 - P["total"] / 100)

    def floor(self):
        return max(self.daily_floor(), self.total_floor())

    def budget(self, eq):
        return max(0.0, (eq - self.floor()) * BUDGET_USE / 100)

    def update(self, bal, eq, now):
        msgs = []
        key = str((now - timedelta(hours=DAY_RESET_HOUR)).date())
        if key != self.key:
            first = self.key is None
            self.key = key
            self.day_start = DAY_START_REF if (first and DAY_START_REF > 0) else max(bal, eq)
            self.peak = eq
            self.day_locked = False
            msgs.append(f"🌅 New trading day. Reference {self.day_start:,.2f}, "
                        f"daily floor {self.daily_floor():,.2f}")
        self.peak = max(self.peak, eq)
        buf = INITIAL_BALANCE * EMERGENCY_BUF / 100
        if not self.total_locked and eq <= self.total_floor() + buf:
            self.total_locked = True
            self.emergency = True
            msgs.append("🚨 Near the TOTAL loss limit: closing everything and stopping.")
        if not self.day_locked and eq <= self.daily_floor() + buf:
            self.day_locked = True
            self.emergency = True
            msgs.append("🚨 Near the DAILY loss limit: closing everything, stopped for today.")
        pp = self.peak - self.day_start
        if (not self.day_locked and pp > self.day_start * MIN_DAY_PROFIT / 100
                and (self.peak - eq) > pp * GIVE_BACK / 100):
            self.day_locked = True
            msgs.append("🔒 Gave back too much of today's peak profit: stopped for today.")
        return msgs


# ----------------------------------------------------------------- news (unofficial free feed)
class News:
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(self):
        self.events = []
        self.loaded = False
        self.last = 0.0

    async def refresh(self, http):
        if time.time() - self.last < 3600:
            return
        self.last = time.time()
        try:
            async with http.get(self.URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
            self.events = [(to_dt(e["date"]), e.get("country", "")) for e in data
                           if e.get("impact") == "High"]
            self.loaded = True
        except Exception as ex:
            log.warning("news feed failed: %s", ex)

    def check(self, ccys, now):
        """(blocked, flatten) for a symbol with these currencies."""
        blocked = flatten = False
        for t, ccy in self.events:
            if ccy not in ccys:
                continue
            mins = (t - now).total_seconds() / 60
            if -NEWS_AFTER <= mins <= NEWS_BEFORE:
                blocked = True
                if mins >= 2.5:      # still outside the 2-minute no-execution window
                    flatten = True
        return blocked, flatten


HELP = ("Commands:\n"
        "/connect LOGIN PASSWORD SERVER  link your MT5 demo account\n"
        "/backtest  test the strategy on recent history\n"
        "/go  start trading   /stop  close everything + pause\n"
        "/status  account + stats   /config  see settings\n"
        "/set NAME VALUE  change a setting   /disconnect  unlink account")


# ----------------------------------------------------------------- the bot
class Bot:
    def __init__(self, S):
        self.S = S
        self.risk = Risk(S.get("risk"))
        self.news = News()
        self.specs = {}
        self.ccy = "USD"
        self.lev = 30
        self.next_try = 0.0
        self.warned_conn = False
        self.http = self.api = self.account = self.conn = None

    # -- telegram
    async def tg(self, method, **params):
        async with self.http.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params) as r:
            return await r.json()

    async def say(self, text):
        log.info(text.replace("\n", " | "))
        if TG_TOKEN and self.S.get("owner"):
            try:
                await self.tg("sendMessage", chat_id=self.S["owner"], text=text)
            except Exception as e:
                log.warning("telegram send failed: %s", e)

    async def poll_telegram(self, timeout):
        timeout = max(1, int(timeout))
        if not TG_TOKEN:
            await asyncio.sleep(timeout)
            return
        try:
            async with self.http.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                                     params={"timeout": timeout, "offset": self.S["tg_offset"]},
                                     timeout=aiohttp.ClientTimeout(total=timeout + 15)) as r:
                data = await r.json()
        except Exception as e:
            log.warning("telegram poll failed: %s", e)
            await asyncio.sleep(2)
            return
        for u in data.get("result", []):
            self.S["tg_offset"] = u["update_id"] + 1
            m = u.get("message")
            if m:
                try:
                    await self.handle_message(m)
                except Exception as e:
                    log.exception("command failed")
                    await self.say(f"⚠️ That command failed: {e}")
            save_state(self.S)

    async def handle_message(self, m):
        chat = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        if not text:
            return
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        S = self.S
        if S.get("owner") is None:
            if cmd == "/start":
                S["owner"] = chat
                await self.say("👋 You're now the owner. Nobody else can control this bot.\n\n"
                               "Step 1: send  /connect LOGIN PASSWORD SERVER  (I delete that message at once)\n"
                               "Step 2: send /backtest to check the strategy\n"
                               "Step 3: send /go to start trading on the demo\n\n" + HELP)
            return
        if chat != S["owner"]:
            return

        # forgiving input: "LOGIN PASSWORD SERVER" sent without /connect works too
        if not text.startswith("/") and re.match(r"^\d{4,}\s+\S+\s+\S+", text):
            cmd, rest = "/connect", text

        if cmd == "/connect":
            try:                                   # remove the message with the password first
                await self.tg("deleteMessage", chat_id=chat, message_id=m["message_id"])
            except Exception:
                pass
            parts = rest.split(None, 2)
            if len(parts) < 3:
                await self.say("Almost! Now send ONE message with your login, password and server "
                               "separated by spaces, like this:\n123456 MyPassword MetaQuotes-Demo")
                return
            await self.do_connect(*parts)
        elif cmd == "/disconnect":
            await self.do_disconnect()
        elif cmd == "/backtest":
            if not S.get("account_id"):
                await self.say("Connect an account first: /connect LOGIN PASSWORD SERVER")
            else:
                S["backtest"] = True
                await self.say("🧪 Backtest queued. Results in a minute or two.")
        elif cmd in ("/go", "/resume"):
            if not S.get("account_id"):
                await self.say("Connect an account first: /connect LOGIN PASSWORD SERVER")
            else:
                S["enabled"] = True
                await self.say("▶️ Trading ON. Daily/total loss protection always applies.")
        elif cmd == "/stop":
            S["enabled"] = False
            if self.conn:
                await self.close_all(await self.my_positions())
            await self.say("⏹ Trading OFF. Open positions closed. /go to start again.")
        elif cmd in ("/status", "/stats"):
            await self.say(await self.report())
        elif cmd == "/config":
            await self.say("Settings (change with /set NAME VALUE):\n" +
                           "\n".join(f"{k} = {globals()[k]}" for k in TUNABLES))
        elif cmd == "/set":
            parts = rest.split(None, 1)
            if len(parts) < 2:
                await self.say("Format: /set NAME VALUE   (e.g. /set TP_ATR 0.7)")
                return
            key, val, err = validate_setting(parts[0], parts[1])
            if err:
                await self.say(err)
                return
            S["cfg"][key] = val
            apply_overrides(S["cfg"])
            S["last_bar"] = {}
            await self.say(f"✅ {key} = {val}")
        else:
            await self.say(HELP)

    # -- MetaApi
    async def do_connect(self, login, password, server):
        from metaapi_cloud_sdk import MetaApi
        if self.S.get("account_id"):
            await self.say("An account is already linked. Send /disconnect first.")
            return
        api = MetaApi(METAAPI_TOKEN)
        last = None
        for typ in ("cloud-g2", "cloud-g1", "cloud"):
            try:
                acc = await api.metatrader_account_api.create_account({
                    "name": "contest-test-bot", "type": typ, "login": str(login), "password": password,
                    "server": server, "platform": "mt5", "magic": MAGIC, "application": "MetaApi"})
                self.S["account_id"] = acc.id
                self.S["enabled"] = False
                self.S["risk"] = {}
                self.S["start_equity"] = None
                save_state(self.S)
                await self.say("🔗 Account created. Logging in now (first time can take a few minutes). "
                               "I'll message you when it's ready.")
                return
            except Exception as e:
                last = e
                if "type" in str(e).lower() or typ in str(e):
                    continue
                break
        await self.say(f"❌ Could not add the account: {last}\n"
                       "Check the login/password/server spelling. If it says the server is unknown, tell Claude.")

    async def do_disconnect(self):
        from metaapi_cloud_sdk import MetaApi
        S = self.S
        if not S.get("account_id"):
            await self.say("No account linked.")
            return
        try:
            if self.conn:
                await self.close_all(await self.my_positions())
            api = self.api or MetaApi(METAAPI_TOKEN)
            acc = await api.metatrader_account_api.get_account(S["account_id"])
            await acc.remove()
        except Exception as e:
            log.warning("remove failed: %s", e)
        S.update(account_id=None, enabled=False, risk={}, start_equity=None, last_bar={}, since={})
        self.conn = self.account = None
        await self.say("🔌 Account unlinked and trading stopped.")

    async def ensure_connected(self):
        S = self.S
        if self.conn or not S.get("account_id") or not METAAPI_TOKEN or time.time() < self.next_try:
            return
        self.next_try = time.time() + 60
        try:
            from metaapi_cloud_sdk import MetaApi
            self.api = MetaApi(METAAPI_TOKEN)
            self.account = await self.api.metatrader_account_api.get_account(S["account_id"])
            if self.account.state != "DEPLOYED":
                await self.account.deploy()
            await asyncio.wait_for(self.account.wait_connected(), 120)
            conn = self.account.get_rpc_connection()
            await conn.connect()
            await asyncio.wait_for(conn.wait_synchronized(), 60)
            self.conn = conn
            if not S.get("ready_told"):
                S["ready_told"] = True
                info = await conn.get_account_information()
                await self.say(f"✅ Logged in. Balance {info['balance']:,.2f} {info.get('currency', '')}, "
                               f"leverage 1:{info.get('leverage')}.\n"
                               f"Initial balance for the loss limits is set to {INITIAL_BALANCE:,.0f} "
                               f"(change with /set INITIAL_BALANCE 10000). Profile: {PROFILE}.\n"
                               f"Next: /backtest, then /go.")
        except Exception as e:
            log.warning("connect failed: %s", e)
            msg = str(e)
            if "top up" in msg.lower():
                self.next_try = time.time() + 600          # retry every 10 minutes
                if time.time() - S.get("conn_warn_ts", 0) > 6 * 3600:
                    S["conn_warn_ts"] = time.time()
                    await self.say("💳 MetaApi needs credit before it can start your MT5 account "
                                   "(its message: top up your account). Add credit or start a trial on the "
                                   "MetaApi billing page. I retry by myself every 10 minutes and will message "
                                   "you when you're logged in.")
            elif time.time() - S.get("conn_warn_ts", 0) > 3600:
                S["conn_warn_ts"] = time.time()
                await self.say("⏳ Still connecting to the MT5 account. If this lasts more than ~10 minutes, "
                               "the login/password/server is probably wrong: /disconnect and /connect again. "
                               f"({msg[:150]})")

    # -- account helpers
    async def my_positions(self):
        return [p for p in await self.conn.get_positions() if p.get("magic") == MAGIC]

    async def get_spec(self, sym):
        if sym not in self.specs:
            try:
                self.specs[sym] = await self.conn.get_symbol_specification(sym)
            except Exception as e:
                log.warning("no spec for %s: %s", sym, e)
                return None
        return self.specs[sym]

    async def close_all(self, positions):
        for p in positions:
            try:
                await self.conn.close_position(p["id"])
            except Exception as e:
                log.warning("close failed %s: %s", p.get("id"), e)

    async def open_risk(self, positions):
        total = 0.0
        for p in positions:
            spec = await self.get_spec(p["symbol"])
            sl, price = p.get("stopLoss"), p.get("currentPrice")
            if not spec or not sl or not price:
                continue
            lpl = loss_per_lot(spec, price, abs(price - sl), self.ccy)
            if lpl:
                total += lpl * p["volume"]
        return total

    def calc_lots(self, spec, entry, sl_dist, eq, open_risk, free_margin):
        budget = self.risk.budget(eq)
        trade_risk = min(budget - open_risk, budget * PER_TRADE / 100)
        if trade_risk <= 0:
            return 0.0
        lpl = loss_per_lot(spec, entry, sl_dist, self.ccy)
        if not lpl:
            return 0.0
        lots = trade_risk / lpl
        mpl = margin_per_lot(spec, entry, self.lev, self.ccy)
        if mpl > 0:
            lots = min(lots, free_margin * MAX_MARGIN / 100 / mpl)
        step = spec.get("volumeStep", 0.01) or 0.01
        mn = spec.get("minVolume", 0.01) or 0.01
        mx = spec.get("maxVolume", 100) or 100
        lots = math.floor(lots / step + 1e-9) * step
        if lots < mn:
            return 0.0                       # never force the minimum lot above the risk budget
        return round(min(lots, mx), 2)

    # -- one trading cycle
    async def cycle(self):
        S = self.S
        if not self.conn:
            return
        now = datetime.now(timezone.utc)
        info = await self.conn.get_account_information()
        bal, eq = info["balance"], info["equity"]
        self.ccy = info.get("currency", "USD")
        self.lev = info.get("leverage") or 30
        free_margin = info.get("freeMargin", eq)
        if S.get("start_equity") is None:
            S["start_equity"] = eq
        positions = await self.my_positions()

        for m in self.risk.update(bal, eq, now):
            await self.say(m)
        S["risk"] = self.risk.dump()
        if self.risk.emergency:
            self.risk.emergency = False
            await self.close_all(positions)
            return
        if not S.get("enabled") or self.risk.locked():
            return
        await self.news.refresh(self.http)
        if NEWS_ON and not self.news.loaded:
            if not S.get("news_warned"):
                S["news_warned"] = True
                await self.say("⚠️ News calendar not loaded, so new entries are blocked. "
                               "Send /set NEWS_FILTER off to override.")
            return

        open_risk = await self.open_risk(positions)
        for sym in SYMBOL_LIST:
            spec = await self.get_spec(sym)
            if not spec:
                continue
            if NEWS_ON:
                ccys = {spec.get("baseCurrency"), spec.get("profitCurrency")}
                blocked, flatten = self.news.check(ccys, now)
                if flatten:
                    mine = [p for p in positions if p["symbol"] == sym]
                    if mine:
                        await self.say(f"📰 News soon: flattening {sym}.")
                        await self.close_all(mine)
                if blocked:
                    continue
            if not in_session(now):
                continue
            if sum(1 for p in positions if p["symbol"] == sym) >= MAX_POS_SYMBOL:
                continue
            if len(positions) >= MAX_POS_TOTAL:
                continue

            bar_id = int(now.timestamp() // TF_SECONDS)
            if S["last_bar"].get(sym) == bar_id:
                continue
            bar_start = datetime.fromtimestamp(bar_id * TF_SECONDS, tz=timezone.utc)
            candles = await self.account.get_historical_candles(sym, TIMEFRAME, None, 1000)
            closed = [c for c in candles if to_dt(c["time"]) < bar_start]
            if not closed or to_dt(closed[-1]["time"]) != bar_start - timedelta(seconds=TF_SECONDS):
                continue                      # last closed candle not available yet, retry next poll
            S["last_bar"][sym] = bar_id
            S["since"][sym] = S["since"].get(sym, 99) + 1

            d, a = signal_at(indicators(closed), len(closed) - 1)
            if d == 0 or S["since"][sym] < COOLDOWN:
                continue
            price = await self.conn.get_symbol_price(sym)
            ask, bid = price["ask"], price["bid"]
            spread = ask - bid
            if spread > a * MAX_SPREAD_ATR:
                continue
            digits = spec.get("digits", 5)
            min_stop = (spec.get("stopsLevel", 0) or 0) * (10 ** -digits)
            sl_dist = max(a * SL_ATR, min_stop + spread)
            tp_dist = max(a * TP_ATR, min_stop + spread)
            entry = ask if d > 0 else bid
            lots = self.calc_lots(spec, entry, sl_dist, eq, open_risk, free_margin)
            if lots <= 0:
                continue
            sl = round(entry - d * sl_dist, digits)
            tp = round(entry + d * tp_dist, digits)
            opts = {"comment": "PBS", "magic": MAGIC}
            try:
                if d > 0:
                    await self.conn.create_market_buy_order(sym, lots, sl, tp, opts)
                else:
                    await self.conn.create_market_sell_order(sym, lots, sl, tp, opts)
                S["since"][sym] = 0
                await self.say(f"{'🟢 BUY' if d > 0 else '🔴 SELL'} {sym} {lots} lots  SL {sl}  TP {tp}")
            except Exception as e:
                await self.say(f"Order failed on {sym}: {e}")
            return                            # one new trade per cycle keeps risk accounting exact

    async def cycle_safe(self):
        try:
            await self.cycle()
        except Exception as e:
            log.exception("cycle error")
            self.S["errors"] = self.S.get("errors", 0) + 1
            if self.S["errors"] in (3, 30):
                await self.say(f"⚠️ Bot errors: {str(e)[:200]}")
        else:
            self.S["errors"] = 0

    # -- reporting
    async def report(self):
        S = self.S
        if not self.conn:
            return ("Account not connected yet. " +
                    ("Linking in progress, try again in a minute." if S.get("account_id")
                     else "Send /connect LOGIN PASSWORD SERVER"))
        info = await self.conn.get_account_information()
        positions = await self.my_positions()
        bal, eq = info["balance"], info["equity"]
        r = self.risk
        now = datetime.now(timezone.utc)
        day0 = now - timedelta(hours=24)
        if r.key:
            day0 = (datetime.fromisoformat(r.key).replace(tzinfo=timezone.utc)
                    + timedelta(hours=DAY_RESET_HOUR))
        closed = wins = 0
        try:
            res = await self.conn.get_deals_by_time_range(day0, now)
            for d in res.get("deals", []):
                if d.get("entryType") == "DEAL_ENTRY_OUT" and d.get("symbol") and d.get("magic") == MAGIC:
                    net = d.get("profit", 0) + d.get("commission", 0) + d.get("swap", 0)
                    closed += 1
                    wins += 1 if net > 0 else 0
        except Exception as e:
            log.warning("deals fetch failed: %s", e)
        floating = sum(p.get("profit", 0) for p in positions)
        day_pl = eq - r.day_start if r.day_start else 0.0
        se = S.get("start_equity") or eq
        status = ("STANDBY (send /go)" if not S.get("enabled") else "LOCKED (total)" if r.total_locked
                  else "LOCKED (today)" if r.day_locked else "RUNNING")
        wr = f"{wins / closed * 100:.0f}%" if closed else "n/a"
        return (f"📊 {PROFILE} | {status}\n"
                f"Balance {bal:,.2f}  Equity {eq:,.2f}\n"
                f"Today {day_pl:+,.2f} ({day_pl / r.day_start * 100 if r.day_start else 0:+.2f}%)  "
                f"Closed {closed}, win rate {wr}\n"
                f"Open {len(positions)}, floating {floating:+,.2f}\n"
                f"Since first connect {(eq - se) / se * 100:+.2f}%\n"
                f"Daily floor {r.daily_floor():,.2f} (room {eq - r.daily_floor():,.2f})\n"
                f"Total floor {r.total_floor():,.2f} (room {eq - r.total_floor():,.2f})")

    async def maybe_hourly(self):
        S = self.S
        if self.conn and S.get("enabled") and time.time() - S.get("last_report", 0) >= STATS_MINUTES * 60:
            S["last_report"] = time.time()
            try:
                await self.say(await self.report())
            except Exception as e:
                log.warning("report failed: %s", e)

    # -- backtest / validation
    async def run_backtest(self):
        bars = env("BT_BARS", 15000, int)
        deadline = time.time() + 150
        data = {}
        for sym in SYMBOL_LIST:
            data[sym] = await fetch_history(self.account, sym, bars, deadline)
            log.info("%s: %d candles", sym, len(data[sym]))
        lines = [f"🧪 Backtest {TIMEFRAME}, EMA {FAST_EMA}/{SLOW_EMA}, RSI{RSI_N} {RSI_BUY}/{RSI_SELL}, "
                 f"TP {TP_ATR}xATR SL {SL_ATR}xATR"]
        base_cost = 0.12
        allT = []
        for sym, cs in data.items():
            if len(cs) < WARMUP + 50:
                lines.append(f"{sym}: not enough history ({len(cs)} candles)")
                continue
            tr = simulate(cs, base_cost)
            allT += tr
            lines.append(fmt(sym, stats([r for _, r in tr])))
        if allT:
            allT.sort()
            cut = allT[0][0] + (allT[-1][0] - allT[0][0]) * 0.7
            ins = [r for t, r in allT if t <= cut]
            oos = [r for t, r in allT if t > cut]
            lines += ["", fmt(f"ALL (cost {base_cost} ATR)", stats([r for _, r in allT])),
                      fmt("first 70% (in-sample)", stats(ins)),
                      fmt("last 30% (out-of-sample)", stats(oos)),
                      "Cost sensitivity (all symbols):"]
            for sf in (0.0, 0.12, 0.25, 0.4):
                R = []
                for cs in data.values():
                    if len(cs) >= WARMUP + 50:
                        R += [r for _, r in simulate(cs, sf)]
                lines.append("  " + fmt(f"spread {sf:.2f} ATR", stats(R)))
            lines += ["", "Verdict (out-of-sample): " + verdict(stats(oos))]
        await self.say("\n".join(lines))


# ----------------------------------------------------------------- backtest helpers
def simulate(candles, spread_frac):
    """One trade at a time per symbol. Entry at next open, SL checked first inside a bar
    (pessimistic), friction = spread_frac x ATR. Returns [(entry_time, R)]."""
    A = indicators(candles)
    n = len(candles)
    out, i, last_entry = [], WARMUP, -999
    while i < n - 1:
        d, a = signal_at(A, i)
        t = to_dt(candles[i]["time"])
        if d == 0 or not in_session(t) or i - last_entry < COOLDOWN:
            i += 1
            continue
        entry = candles[i + 1]["open"]
        sl_d, tp_d = a * SL_ATR, a * TP_ATR
        sl, tp = entry - d * sl_d, entry + d * tp_d
        r, j = None, i + 1
        for j in range(i + 1, min(n, i + 1 + MAX_HOLD)):
            hi, lo = candles[j]["high"], candles[j]["low"]
            if d > 0:
                if lo <= sl:
                    r = -1.0
                    break
                if hi >= tp:
                    r = tp_d / sl_d
                    break
            else:
                if hi >= sl:
                    r = -1.0
                    break
                if lo <= tp:
                    r = tp_d / sl_d
                    break
        if r is None:
            j = min(n - 1, i + MAX_HOLD)
            r = d * (candles[j]["close"] - entry) / sl_d
        r -= (a * spread_frac) / sl_d
        out.append((t, r))
        last_entry = i
        i = j + 1
    return out


def stats(R):
    n = len(R)
    if n == 0:
        return None
    wins = [r for r in R if r > 0]
    losses = [-r for r in R if r <= 0]
    mean = sum(R) / n
    sd = (sum((r - mean) ** 2 for r in R) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    cum = peak = dd = 0.0
    for r in R:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    pf = (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else float("inf")
    return dict(n=n, wr=len(wins) / n * 100, pf=pf, exp=mean, t=t, dd=dd, total=cum)


def fmt(name, s):
    if not s:
        return f"{name}: no trades"
    return (f"{name}: {s['n']} trades, win {s['wr']:.0f}%, PF {s['pf']:.2f}, "
            f"exp {s['exp']:+.3f}R, t={s['t']:.1f}, maxDD {s['dd']:.1f}R")


def verdict(s):
    if not s or s["n"] < 100:
        return "TOO FEW TRADES to conclude anything"
    if s["exp"] <= 0:
        return "NEGATIVE expectancy: do not run this live"
    if s["t"] < 2:
        return "positive but NOT distinguishable from luck (t<2)"
    return "positive edge so far (t>=2). Still forward-test on demo before trusting it"


async def fetch_history(account, sym, bars, deadline):
    got, start = {}, None
    while len(got) < bars and time.time() < deadline:
        batch = await account.get_historical_candles(sym, TIMEFRAME, start, 1000)
        if not batch:
            break
        before = len(got)
        for c in batch:
            got[to_dt(c["time"])] = c
        if len(got) == before:
            break
        start = min(got)
        await asyncio.sleep(0.3)
    return [got[k] for k in sorted(got)]


# ----------------------------------------------------------------- main
async def run_window():
    S = load_state()
    apply_overrides(S.get("cfg"))
    bot = Bot(S)
    bot.http = aiohttp.ClientSession()
    end = time.time() + RUN_SECONDS
    try:
        if not TG_TOKEN:
            log.error("TELEGRAM_BOT_TOKEN is not set")
            return
        while time.time() < end:
            await bot.ensure_connected()
            if S.get("backtest") and bot.account and bot.conn:
                S["backtest"] = False
                try:
                    await bot.run_backtest()
                except Exception as e:
                    log.exception("backtest failed")
                    await bot.say(f"Backtest failed: {str(e)[:200]}")
            await bot.cycle_safe()
            await bot.maybe_hourly()
            save_state(S)
            remaining = end - time.time()
            if remaining <= 1:
                break
            await bot.poll_telegram(min(POLL_SECONDS, remaining))
    finally:
        save_state(S)
        try:
            if bot.conn:
                await bot.conn.close()
        except Exception:
            pass
        await bot.http.close()


if __name__ == "__main__":
    asyncio.run(run_window())
