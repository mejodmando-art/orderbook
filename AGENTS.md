# AGENTS.md — MEXC Market Maker Bot

Agent guidance for working in this repository.

---

## Project Overview

A Telegram-controlled passive market-maker bot for MEXC exchange.  
It detects Order Block (OB) signals from live WebSocket order-book data, executes trades via the MEXC REST API, and persists state in a Supabase PostgreSQL database.  
Deployed as a Railway `worker` dyno (no inbound HTTP).

### Module Map

| Path | Responsibility |
|---|---|
| `main.py` | Entry point. Validates env, wires startup hook, starts long-polling. |
| `config/settings.py` | Env-var loading, precision cache, `validate_env()`. |
| `bot/telegram_bot.py` | All Telegram handlers, inline-button dashboard, `build_application()`, `recover_state()`. |
| `core/analyzer.py` | Order Block detection, impulse-move logic, order-book volume confirmation. |
| `core/mexc_ws.py` | Async WebSocket client for MEXC live order-book feed. |
| `core/trade_exec.py` | MEXC REST trade execution, `VirtualStopLossWatcher`. |
| `utils/db_manager.py` | asyncpg singleton pool, `bot_state` table CRUD. |
| `supabase/schema.sql` | Reference DDL (auto-applied at startup; kept for manual inspection). |

---

## Environment Variables

All required. Set in Railway Variables tab or a local `.env` file (see `.env.example`).

| Variable | Purpose |
|---|---|
| `MEXC_API_KEY` | MEXC API key |
| `MEXC_SECRET_KEY` | MEXC API secret |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs |
| `DATABASE_URL` | Supabase pooler connection string (port 6543) |
| `LOG_LEVEL` | Optional. Default `INFO`. Use `DEBUG` for verbose output. |

Never commit real credentials. `.env` is gitignored.

---

## Local Development

```bash
# 1. Copy env template
cp .env.example .env
# Fill in real values

# 2. Install dependencies (Python 3.10)
pip install -r requirements.txt

# 3. Run
python main.py
```

The bot requires a live Supabase database and valid MEXC/Telegram credentials to start.  
There is no mock/offline mode.

---

## Architecture Constraints

- **Single-process, single-event-loop.** All async work runs in the event loop managed by `python-telegram-bot`. Do not create a second event loop.
- **Singleton DB pool.** `init_db()` must be called exactly once (in `_on_startup`). Use `get_db()` everywhere else.
- **Singleton bot state row.** `bot_state` always has exactly one row (`id=1`). Never insert additional rows.
- **PgBouncer transaction mode.** `statement_cache_size=0` is required on the asyncpg pool. Do not enable prepared statements.
- **No inbound HTTP.** The Railway dyno is a `worker`. Do not add a web server or health-check endpoint without updating `Procfile`.

---

## Code Conventions

- **Python 3.10.** Match `runtime.txt`. Use `match`/`case` only if already present in the file being edited.
- **Async throughout.** All I/O (DB, HTTP, WebSocket) must be `async`/`await`. No blocking calls in the event loop.
- **Logging over print.** Use `logging.getLogger(__name__)` in every module. Never use `print()`.
- **Partial upserts.** `db.upsert_state(data)` accepts a partial dict — only pass keys you intend to change.
- **Precision via cache.** Always call `get_precision(symbol)` from `config.settings`; never hardcode decimal places.
- **Auth guard.** Every Telegram handler must check `update.effective_user.id in ALLOWED_USER_IDS` before acting.

### Order-book state (IMP-01)

`MarketAnalyzer` maintains the live book as two dicts (`_bid_book`, `_ask_book`), not lists. `update_depth(bids, asks)` applies incremental deltas from the MEXC `increase.depth` stream:
- volume > 0 → insert/update the level
- volume == 0 → remove the level

Use `analyzer.top_bids(n)` / `analyzer.top_asks(n)` to read the current book. Do not access `_bid_book` / `_ask_book` directly. Use `analyzer.current_price` (property) instead of `analyzer._current_price`.

The WS depth subscription uses `@20` (not `@5`) for a deeper initial snapshot.

### Stop-loss watcher (IMP-03 / IMP-05)

`VirtualStopLossWatcher` accepts either:
- `stop_loss_price` (float) — absolute price, preferred
- `stop_loss_pct` (float, default 0.985) — ratio fallback

Always pass `stop_loss_price` when the absolute value is known (e.g. from the OB signal or the `stop_loss` DB column). The ratio fallback exists only for legacy recovery paths where no absolute price is stored.

`_arm_sl_watcher()` in `telegram_bot.py` mirrors this: pass `sl_price=` for the absolute value, `sl_pct=` only as a fallback.

### Fill-poll timeout (IMP-04)

`trade_exec.poll_order_fill()` accepts an `on_timeout` coroutine callback. Always supply one via `_make_fill_timeout_cb(symbol, order_id, application)`. The callback cancels the MEXC order, resets DB state, resets in-memory state, and sends a Telegram alert. Never call `poll_order_fill` without `on_timeout`.

---

## Deployment

- Platform: **Railway** (worker dyno)
- Build: `nixpacks.toml` forces Python 3.10 and runs `pip install -r requirements.txt`
- Start command: `python main.py` (also in `Procfile`)
- No Docker image is used; do not add a `Dockerfile` unless migrating away from Nixpacks.

---

## Testing

There are no automated tests. When adding features:
- Manually verify Telegram command flow end-to-end.
- Check Railway logs (`LOG_LEVEL=DEBUG`) for WebSocket and DB activity.
- Confirm `bot_state` row in Supabase after state changes.

---

## What Agents Should NOT Do

- Do not add a second `bot_state` table or change the singleton-row pattern without updating all CRUD methods.
- Do not switch from `asyncpg` to another DB driver without auditing `statement_cache_size` and JSONB handling.
- Do not commit `.env` or any file containing real credentials.
- Do not change `DATABASE_URL` format without updating the `replace()` normalisation in `db_manager.connect()`.
- Do not add synchronous blocking I/O (e.g., `requests`, `time.sleep`) inside async handlers.
- Do not replace `_bid_book`/`_ask_book` with plain lists — the incremental delta logic depends on dict semantics.
- Do not call `poll_order_fill()` without an `on_timeout` callback — omitting it leaves the bot stuck in `pending_fill` on timeout.
- Do not pass `stop_loss_pct` to `_arm_sl_watcher` when the absolute `sl_price` is available; the ratio introduces floating-point drift.
