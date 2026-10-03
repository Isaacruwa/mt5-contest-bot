# MT5 contest test bot

A Telegram-controlled MT5 demo-account test bot. It runs on GitHub Actions (every ~5 minutes).
Everything is set up and controlled from Telegram, demo/test use only.

Telegram commands: /start, /connect LOGIN PASSWORD SERVER, /backtest, /go, /stop, /status,
/config, /set NAME VALUE, /disconnect.

Required GitHub secrets: METAAPI_TOKEN, TELEGRAM_BOT_TOKEN.
