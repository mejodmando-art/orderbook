"""
Async PostgreSQL database manager using asyncpg.

Connects directly to Supabase via the connection-pooler string in DATABASE_URL.
The pooler endpoint (port 6543) is recommended for the Supabase free tier because
it multiplexes connections and avoids hitting the 60-connection limit.

All bot state lives in a single row (id=1) of the `bot_state` table.
The table is created automatically on first startup via `ensure_table()`.

Public API
----------
    init_db()               -> DatabaseManager   (call once in main())
    get_db()                -> DatabaseManager   (singleton accessor)

    db.fetch_state()        -> dict | None
    db.upsert_state(data)   -> None
    db.reset_state()        -> None
    db.is_trade_active()    -> bool
    db.ensure_table()       -> None              (called by init_db)
    db.close()              -> None              (called on shutdown)
"""

import asyncio
import json
import logging
from typing import Any

import asyncpg

from config.settings import DATABASE_URL

logger = logging.getLogger(__name__)

_TABLE = "bot_state"
_ROW_ID = 1

# Written to the row on first boot and after every reset
_EMPTY_STATE: dict[str, Any] = {
    "symbol": None,
    "is_active": False,
    "entry_price": None,
    "side": None,
    "qty": None,
    "stop_loss": None,
    "config": {},
}

# DDL executed once at startup — idempotent
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bot_state (
    id          INTEGER     PRIMARY KEY,
    symbol      TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT FALSE,
    entry_price FLOAT8,
    side        TEXT        CHECK (side IN ('buy', 'sell')),
    qty         FLOAT8,
    stop_loss   FLOAT8,
    config      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Trigger that keeps updated_at current on every write
_CREATE_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_bot_state_updated_at'
    ) THEN
        CREATE TRIGGER trg_bot_state_updated_at
            BEFORE UPDATE ON bot_state
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END;
$$;
"""


class DatabaseManager:
    """
    Async PostgreSQL client backed by an asyncpg connection pool.

    The pool is sized at min=1 / max=3 — sufficient for a single-process
    bot and well within Supabase free-tier limits when using the pooler.

    asyncpg returns JSONB columns as strings; we decode them explicitly.
    """

    def __init__(self) -> None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL must be set before initialising DatabaseManager"
            )
        self._pool: asyncpg.Pool | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the connection pool. Called once from init_db()."""
        # asyncpg does not understand the 'postgresql+asyncpg://' SQLAlchemy
        # prefix — strip it if present and normalise to plain 'postgresql://'.
        dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

        self._pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=3,
            # Supabase pooler (PgBouncer) uses transaction-mode pooling;
            # prepared statements are not supported in that mode.
            statement_cache_size=0,
            command_timeout=10,
        )
        logger.info("asyncpg pool connected (min=1 max=3)")

    async def close(self) -> None:
        """Gracefully close the connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("asyncpg pool closed")

    async def ensure_table(self) -> None:
        """
        Create the bot_state table and updated_at trigger if they don't
        exist, then seed the singleton row.  Safe to call on every startup.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(_CREATE_TABLE_SQL)
            await conn.execute(_CREATE_TRIGGER_SQL)
            # Seed the singleton row only if it doesn't exist yet
            await conn.execute(
                """
                INSERT INTO bot_state (id, symbol, is_active, entry_price,
                                       side, qty, stop_loss, config)
                VALUES ($1, NULL, FALSE, NULL, NULL, NULL, NULL, '{}'::jsonb)
                ON CONFLICT (id) DO NOTHING
                """,
                _ROW_ID,
            )
        logger.info("bot_state table ready")

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: asyncpg.Record | None) -> dict | None:
        """Convert an asyncpg Record to a plain dict, decoding JSONB."""
        if row is None:
            return None
        d = dict(row)
        # asyncpg returns JSONB as a str when the column type is unknown;
        # decode it if needed.
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                d["config"] = {}
        return d

    # ── Public API ─────────────────────────────────────────────────────────────

    async def fetch_state(self) -> dict | None:
        """Return the bot_state row as a dict, or None if missing."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM bot_state WHERE id = $1", _ROW_ID
            )
        result = self._row_to_dict(row)
        logger.debug("fetch_state: %s", result)
        return result

    async def upsert_state(self, data: dict[str, Any]) -> None:
        """
        Insert or update the singleton row.

        `data` is a partial dict — only the keys you want to change.
        Unrecognised keys are silently ignored to avoid SQL errors.
        """
        # Map Python dict keys to the columns we manage
        allowed = {"symbol", "is_active", "entry_price", "side", "qty",
                   "stop_loss", "config"}
        payload = {k: v for k, v in data.items() if k in allowed}

        if not payload:
            logger.warning("upsert_state called with no recognised keys")
            return

        # Encode config dict to JSON string for asyncpg
        if "config" in payload and isinstance(payload["config"], dict):
            payload["config"] = json.dumps(payload["config"])

        # Build dynamic SET clause
        cols = list(payload.keys())
        set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
        values = [payload[c] for c in cols]

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO bot_state (id, {', '.join(cols)})
                VALUES ($1, {', '.join(f'${i+2}' for i in range(len(cols)))})
                ON CONFLICT (id) DO UPDATE SET {set_clause}
                """,
                _ROW_ID, *values,
            )
        logger.debug("upsert_state: %s", payload)

    async def reset_state(self) -> None:
        """Reset the row to idle defaults. Called after trade close or /emergency_stop."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE bot_state
                SET symbol      = NULL,
                    is_active   = FALSE,
                    entry_price = NULL,
                    side        = NULL,
                    qty         = NULL,
                    stop_loss   = NULL,
                    config      = '{}'::jsonb
                WHERE id = $1
                """,
                _ROW_ID,
            )
        logger.info("bot_state reset to idle")

    async def is_trade_active(self) -> bool:
        """Return True if is_active == true in the database."""
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT is_active FROM bot_state WHERE id = $1", _ROW_ID
            )
        return bool(val)

    async def ensure_row_exists(self) -> None:
        """
        Guarantee the singleton row exists.
        Delegates to ensure_table() which already handles this idempotently.
        Kept for API compatibility with callers in telegram_bot.py.
        """
        await self.ensure_table()


# ── Singleton ──────────────────────────────────────────────────────────────────

_db: DatabaseManager | None = None


def get_db() -> DatabaseManager:
    """Return the singleton. Raises if init_db() has not been called."""
    if _db is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _db


async def init_db() -> DatabaseManager:
    """
    Create the DatabaseManager, open the connection pool, and ensure the
    table exists.  Must be awaited once before any DB operations.

    Changed to async (was sync) because asyncpg.create_pool() is a coroutine.
    main.py calls this inside the post_init hook so the event loop is running.
    """
    global _db
    if _db is None:
        _db = DatabaseManager()
        await _db.connect()
        await _db.ensure_table()
    return _db
