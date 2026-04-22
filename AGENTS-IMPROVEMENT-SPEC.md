# AGENTS-IMPROVEMENT-SPEC.md

Audit of the MEXC Market Maker Bot codebase and a concrete improvement plan.

---

## 1. What's Good

### Architecture
- Clean module separation: settings, DB, WS, analysis, execution, and Telegram are each in their own file.
- Singleton DB pool with asyncpg is correctly sized for a single-process worker and respects PgBouncer transaction-mode constraints (`statement_cache_size=0`).
- `validate_env()` fails fast at startup before any I/O is attempted.
- `recover_state()` re-arms the WebSocket and stop-loss watcher on restart — no manual intervention needed after a crash.
- `post_init` hook correctly defers `asyncpg.create_pool()` until the event loop is running.

### Code Quality
- Consistent use of `logging.getLogger(__name__)` throughout; no `print()` calls.
- `upsert_state()` accepts partial dicts — callers only write what they change.
- `_confirm_ob_volume()` falls back to `True` when no book data has arrived yet, preventing a deadlock on startup.
- `detect_order_blocks()` excludes the live (incomplete) candle from the average-body calculation.
- Exponential back-off on WebSocket reconnect, capped at 60 s.
- `.env.example` is thorough and documents every variable with context.
- `supabase/schema.sql` is kept as a reference and is idempotent.

### Deployment
- `nixpacks.toml` pins Python 3.10 and prevents Railway language misdetection.
- `Procfile` and `nixpacks.toml` agree on the start command.
- `.gitignore` covers all expected artifacts.

---

## 2. What's Missing

### Testing
- **No tests at all.** There are no unit tests, integration tests, or fixtures. The analyzer logic (`detect_order_blocks`, `find_entry_signal`, `_confirm_ob_volume`) is pure Python with no external dependencies and is directly testable.

### Observability
- **No trade history.** The `bot_state` table holds only the current trade. There is no `trades` table recording completed trades (entry, exit, P&L, duration). Without this, there is no way to evaluate strategy performance.
- **No metrics.** There is no way to know how many signals were found, how many were rejected by volume confirmation, or how long the bot spent in each state.

### Error Recovery
- **No fill-poll timeout handling.** `_poll_order_fill()` logs a warning on timeout but does not cancel the limit order or reset state. A timed-out fill leaves the bot in `pending_fill` state indefinitely.
- **No TP-order failure recovery.** If `place_limit_sell()` fails after a fill, `exit_order_id` is `None` and the bot has an open position with no take-profit order. The stop-loss watcher still runs, but there is no retry or alert escalation.
- **No WebSocket subscription refresh after reconnect.** `_connect_and_serve()` re-subscribes on reconnect, but if the bot has been running for hours and MEXC rotates the session, the subscription list may be stale (e.g., symbol changed mid-session).

### Configuration
- **No runtime config validation.** `_state.config` is populated from Telegram input but never validated for type or range (e.g., negative capital, zero SMA period, `impulse_multiplier < 1`).
- **Strategy parameters are not persisted to DB.** `_state.config` lives only in memory. If the process restarts mid-search (before a fill), the config is lost and `recover_state()` cannot reconstruct the search parameters.

### Documentation
- **`README.md` is empty** (contains only the repo name). There is no setup guide, architecture overview, or deployment walkthrough.
- **No AGENTS.md** (created by this session).

---

## 3. What's Wrong

### Bugs

#### `_confirm_ob_volume` uses only the visible book snapshot
The depth subscription uses `@5` (top-5 levels). Volume confirmation compares zone volume against the average of those 5 levels. For symbols with thin books, a single large level can skew the average and cause valid OBs to be rejected. The threshold is applied to a non-representative sample.

**Fix:** Subscribe to a deeper snapshot (`@20`) or document the limitation and adjust `ob_volume_threshold` defaults accordingly.

#### `_on_depth` replaces the full book on every incremental update
`update_depth(bids, asks)` replaces `self._bids` and `self._asks` entirely. The MEXC `increase.depth` stream sends *incremental* updates (deltas), not full snapshots. Levels with volume `"0"` mean removal. The current code treats every message as a full snapshot, so the book state is always the last delta, not the accumulated order book.

**Fix:** Maintain a sorted dict of `{price: volume}` for bids and asks. Apply deltas: set volume if > 0, delete if == 0. Expose the top-N levels for analysis.

#### `_monitor_loop` accesses `_state.analyzer._current_price` directly
`_current_price` is a private attribute. The monitor loop reads it without going through a public accessor. If `MarketAnalyzer` is replaced or refactored, this will silently break.

**Fix:** Add a `current_price` property to `MarketAnalyzer`.

#### `recover_state` does not restore `_state.config`
When the bot restarts with `is_active == True`, `recover_state()` re-arms the SL watcher using values from the DB row (`entry_price`, `stop_loss`, `qty`). But `_state.config` is left empty. Any code path that reads `_state.config` after recovery (e.g., `_on_order_filled`, `_start_strategy`) will get stale or missing values.

**Fix:** Persist the full config dict to `bot_state.config` (it is already a JSONB column) and restore it in `recover_state()`.

#### `sl_ratio` derivation in `_arm_sl_watcher` call
```python
sl_ratio = sl_price / entry_price if entry_price else 0.985
```
`VirtualStopLossWatcher` receives `stop_loss_pct` and computes the trigger as `entry_price * stop_loss_pct`. If `sl_price` was already derived from an OB (e.g., `entry_ob.low - tick`), the ratio will not equal the original OB-based SL exactly due to floating-point rounding. The watcher may trigger at a slightly different price than intended.

**Fix:** Pass `sl_price` directly to the watcher as an absolute price, not as a ratio. Update `VirtualStopLossWatcher` to accept `stop_loss_price: float` instead of `stop_loss_pct`.

#### `upsert_state` dynamic SQL is injection-safe but fragile
The column list is filtered against an `allowed` set, which prevents SQL injection. However, the dynamic `INSERT … ON CONFLICT DO UPDATE SET` construction will silently ignore any key not in `allowed`. If a new column is added to the table but not to `allowed`, writes will silently drop data.

**Fix:** Add a comment warning that `allowed` must be kept in sync with the table schema, or derive it from the schema at startup.

### Code Smells

- `_state` is a module-level singleton in `telegram_bot.py`. This makes the module untestable without mocking global state. Consider passing `BotState` as a parameter to handlers or wrapping it in the `Application.bot_data` dict.
- `time.time()` is used for `filled_at` timestamps. Use `datetime.utcnow().isoformat()` or store as a Unix integer consistently — the DB column is `TIMESTAMPTZ`.
- `_OB_REFRESH_INTERVAL = 300` is a magic number defined inside `_start_strategy`. Move it to `config/settings.py` as a named constant.
- `asyncio.sleep(3)` after `_start_ws()` is a timing assumption. Replace with a condition that waits until the first depth snapshot arrives (e.g., check `_state.analyzer._bids`).

---

## 4. Improvement Spec

Ordered by impact. Each item is self-contained and can be implemented independently.

---

### IMP-01 — Fix incremental order-book handling (Critical)

**Problem:** `update_depth()` treats every WebSocket delta as a full snapshot. The book state is always wrong after the first update.

**Implementation:**
1. In `MarketAnalyzer`, replace `self._bids: list` and `self._asks: list` with `self._bid_book: dict[float, float]` and `self._ask_book: dict[float, float]`.
2. In `update_depth()`, iterate the incoming levels. If `volume > 0`, set `book[price] = volume`. If `volume == 0`, delete `book.pop(price, None)`.
3. Expose `top_bids(n=20)` and `top_asks(n=20)` properties that return sorted lists for analysis.
4. Update `_confirm_ob_volume()` and `find_walls()` to use the new properties.
5. Change the WS subscription from `@5` to `@20` in `_start_ws()` to get a deeper initial snapshot.

---

### IMP-02 — Add trade history table (High)

**Problem:** No record of completed trades. Strategy performance cannot be evaluated.

**Implementation:**
1. Add a `trades` table to `supabase/schema.sql`:
   ```sql
   CREATE TABLE IF NOT EXISTS trades (
       id          SERIAL PRIMARY KEY,
       symbol      TEXT NOT NULL,
       side        TEXT NOT NULL,
       entry_price FLOAT8 NOT NULL,
       exit_price  FLOAT8,
       qty         FLOAT8 NOT NULL,
       pnl_pct     FLOAT8,
       exit_reason TEXT,   -- 'take_profit' | 'stop_loss' | 'emergency_stop'
       opened_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       closed_at   TIMESTAMPTZ
   );
   ```
2. Add `insert_trade()` and `close_trade()` methods to `DatabaseManager`.
3. Call `insert_trade()` in `_on_order_filled()`.
4. Call `close_trade()` in the SL watcher trigger and TP fill handler.
5. Add a `/history` Telegram command that shows the last 5 trades.

---

### IMP-03 — Persist and restore `_state.config` (High)

**Problem:** Config is lost on restart. `recover_state()` cannot reconstruct search parameters.

**Implementation:**
1. In `_start_strategy()`, after building `_state.config`, call `db.upsert_state({"config": _state.config})`.
2. In `recover_state()`, after fetching the DB row, set `_state.config = row.get("config", {})`.
3. Ensure `upsert_state()` serialises the config dict to JSON (already done for the `config` key).

---

### IMP-04 — Fix fill-poll timeout: cancel order and reset state (High)

**Problem:** A timed-out fill leaves the bot in `pending_fill` indefinitely.

**Implementation:**
1. In `_poll_order_fill()` (or its caller), after the timeout warning, call `trade_exec.cancel_order(symbol, order_id)`.
2. Call `db.reset_state()` and set `_state.running = False`.
3. Send a Telegram alert: `"⚠️ Limit order timed out and was cancelled. Bot is idle."`

---

### IMP-05 — Fix `VirtualStopLossWatcher` to use absolute SL price (Medium)

**Problem:** The ratio-based SL derivation introduces floating-point drift from the OB-based SL price.

**Implementation:**
1. Add `stop_loss_price: float` parameter to `VirtualStopLossWatcher.__init__()`.
2. Replace the trigger condition `price <= self.entry_price * self.stop_loss_pct` with `price <= self.stop_loss_price`.
3. Update `_arm_sl_watcher()` to pass `sl_price` directly.
4. Remove the `sl_ratio` derivation in `telegram_bot.py`.

---

### IMP-06 — Add unit tests for `MarketAnalyzer` (Medium)

**Problem:** The core signal logic has no tests. Regressions are invisible.

**Implementation:**
1. Create `tests/test_analyzer.py` using `pytest`.
2. Add `pytest` to `requirements.txt` (dev dependency — acceptable for a single-process app).
3. Write tests for:
   - `detect_order_blocks()` with synthetic candle lists (bullish OB, bearish OB, no OB).
   - `find_entry_signal()` with mocked price, SMA, and book data.
   - `_confirm_ob_volume()` with edge cases (empty book, single level, zone with no volume).
   - `update_depth()` incremental delta application (after IMP-01).
4. Add a `pytest` run to the devcontainer `postCreateCommand` or document it in `AGENTS.md`.

---

### IMP-07 — Add `current_price` property to `MarketAnalyzer` (Low)

**Problem:** `_monitor_loop` accesses the private `_current_price` attribute directly.

**Implementation:**
1. Add `@property def current_price(self) -> float: return self._current_price` to `MarketAnalyzer`.
2. Update `_monitor_loop` to use `_state.analyzer.current_price`.

---

### IMP-08 — Move magic numbers to `config/settings.py` (Low)

**Problem:** `_OB_REFRESH_INTERVAL = 300` and `asyncio.sleep(3)` are buried in `_start_strategy()`.

**Implementation:**
1. Add to `config/settings.py`:
   ```python
   OB_REFRESH_INTERVAL: int = int(os.getenv("OB_REFRESH_INTERVAL", "300"))
   WS_WARMUP_DELAY: int = int(os.getenv("WS_WARMUP_DELAY", "3"))
   ```
2. Replace the literals in `telegram_bot.py` with the imported constants.
3. Document both in `.env.example` under `# Optional`.

---

### IMP-09 — Write `README.md` (Low)

**Problem:** `README.md` contains only the repo name. New contributors have no onboarding path.

**Implementation:**
Replace `README.md` with sections covering:
- What the bot does (one paragraph).
- Prerequisites (MEXC account, Telegram bot, Supabase project).
- Local setup (copy `.env.example`, install deps, run).
- Railway deployment (link to `nixpacks.toml`, required env vars).
- Architecture overview (link to module map in `AGENTS.md`).
- Known limitations (no offline mode, single trade at a time).

---

### IMP-10 — Validate Telegram setup input (Low)

**Problem:** Invalid values (negative capital, zero SMA period) are accepted and cause downstream errors.

**Implementation:**
1. In the `SETUP_CAPITAL` handler, reject non-numeric or `<= 0` values with an inline error message.
2. In `_cb_strat_*` handlers, assert that `impulse_multiplier >= 1.0` and `ob_volume_threshold > 0`.
3. No schema change required — validation is purely in the handler layer.

---

## 5. Priority Order

| # | Item | Effort | Impact |
|---|---|---|---|
| 1 | IMP-01 Incremental order book | Medium | Critical — current book state is wrong |
| 2 | IMP-03 Persist/restore config | Small | High — recovery is broken without it |
| 3 | IMP-04 Fill-poll timeout recovery | Small | High — prevents stuck state |
| 4 | IMP-05 Absolute SL price | Small | Medium — correctness fix |
| 5 | IMP-02 Trade history table | Medium | High — enables performance review |
| 6 | IMP-06 Unit tests | Medium | Medium — prevents regressions |
| 7 | IMP-07 `current_price` property | Trivial | Low — encapsulation |
| 8 | IMP-08 Magic numbers to settings | Trivial | Low — maintainability |
| 9 | IMP-09 README | Small | Low — onboarding |
| 10 | IMP-10 Input validation | Small | Low — UX robustness |
