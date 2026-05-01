# Grid Bot — MEXC Spot

Telegram-controlled grid trading bot for MEXC spot market.

## Quick Start

```bash
cp .env.example .env
# Fill in MEXC_API_KEY, MEXC_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL

pip install -r requirements.txt
python main.py
```

## Telegram

Send `/menu` to open the interactive control panel.

| Command | Description |
|---|---|
| `/menu` | Interactive control panel |
| `/list` | Active grids |
| `/status BTCUSDT` | Profit report for a grid |
| `/stop BTCUSDT` | Stop grid and market-sell holdings |
| `/upgrade` | Rebuild all grids at current price |

## How It Works

**Grid Bot**
- User sets: symbol, investment (USDT), grids per side, upper % and lower % range, risk level.
- Places `n` limit buy orders below price and `n` limit sell orders above, evenly spaced within the range.
- When a buy fills → places a limit sell one spacing above. When a sell fills → places a limit buy one spacing below.
- If price breaks out beyond the user-defined range, waits for the 1-minute candle to close then rebuilds the grid around the new price.
- Balance drift (external purchases) is detected every 60 s and the user is prompted to sync.

**Price Action Bot**
- Spot only: buy signals only (no short/sell entries).
- Detects Equal Highs/Lows, Liquidity Sweep, and confirmation candle (Engulfing/Hammer).
- Enters with a market buy, places a limit sell at the TP level immediately after.

## Risk Levels

| Level | Effect |
|---|---|
| low | Wider grid spacing, fewer orders |
| medium | Balanced spacing and order count |
| high | Tighter spacing, more orders |

## Deployment

Set environment variables, then run `python main.py`. The `Procfile` runs it as a worker process (Railway/Heroku compatible).
