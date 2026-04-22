"""
Supabase database manager.

All bot state is stored in a single row (id=1) of the `bot_state` table.
Using a singleton row keeps the design simple: the bot only ever runs one
trade at a time, so there is no need for a multi-row history table here.

Table schema (apply via supabase/schema.sql):

    bot_state
    ├── id          integer  PRIMARY KEY  (always 1)
    ├── symbol      text
    ├── is_active   boolean              (true while a trade is open/pending)
    ├── entry_price float8
    ├── side        text                 ('buy' | 'sell')
    ├── qty         float8
    ├── stop_loss   float8
    └── config      jsonb                (full strategy config dict)

Public API
----------
    get_db()                -> DatabaseManager   singleton accessor
    db.fetch_state()        -> dict | None
    db.upsert_state(data)   -> None
    db.reset_state()        -> None
    db.is_trade_active()    -> bool

All methods are async and safe to call from any asyncio context.
The Supabase client performs HTTP calls internally; we run them in a
thread-pool executor so they never block the event loop.
"""

import asyncio
import logging
from typing import Any

from supabase import create_client, Client

from config.settings import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

# The table name and the single-row primary key
_TABLE = "bot_state"
_ROW_ID = 1

# Default empty state written on first boot / after reset
_EMPTY_STATE: dict[str, Any] = {
    "id": _ROW_ID,
    "symbol": None,
    "is_active": False,
    "entry_price": None,
    "side": None,
    "qty": None,
    "stop_loss": None,
    "config": {},
}


class DatabaseManager:
    """
    Thin async wrapper around the synchronous supabase-py client.

    supabase-py v2 uses httpx under the hood but exposes a synchronous
    interface. We offload every call to asyncio's default thread-pool
    executor so the event loop is never blocked.
    """

    def __init__(self) -> None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set before initialising DatabaseManager"
            )
        self._client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialised (url=%s…)", SUPABASE_URL[:30])

    # ── Internal helper ────────────────────────────────────────────────────────

    async def _run(self, fn, *args, **kwargs):
        """Run a synchronous callable in the thread-pool executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── Public API ─────────────────────────────────────────────────────────────

    async def fetch_state(self) -> dict | None:
        """
        Return the current bot_state row as a dict, or None if the row
        does not exist yet.
        """
        try:
            result = await self._run(
                lambda: (
                    self._client.table(_TABLE)
                    .select("*")
                    .eq("id", _ROW_ID)
                    .maybe_single()
                    .execute()
                )
            )
            data = result.data
            if data:
                logger.debug("fetch_state: %s", data)
            else:
                logger.debug("fetch_state: no row found")
            return data
        except Exception as exc:
            logger.error("fetch_state failed: %s", exc)
            raise

    async def upsert_state(self, data: dict[str, Any]) -> None:
        """
        Insert or update the bot_state row.

        `data` should be a partial dict — only the keys you want to change.
        The row id is always forced to _ROW_ID so callers don't need to
        include it.
        """
        payload = {**data, "id": _ROW_ID}
        try:
            await self._run(
                lambda: (
                    self._client.table(_TABLE)
                    .upsert(payload, on_conflict="id")
                    .execute()
                )
            )
            logger.debug("upsert_state: %s", payload)
        except Exception as exc:
            logger.error("upsert_state failed: %s", exc)
            raise

    async def reset_state(self) -> None:
        """
        Reset the bot_state row to the empty/idle defaults.
        Safe to call after a trade closes or on /emergency_stop.
        """
        try:
            await self._run(
                lambda: (
                    self._client.table(_TABLE)
                    .upsert(_EMPTY_STATE, on_conflict="id")
                    .execute()
                )
            )
            logger.info("bot_state reset to idle")
        except Exception as exc:
            logger.error("reset_state failed: %s", exc)
            raise

    async def is_trade_active(self) -> bool:
        """
        Return True if is_active == true in the database.
        Used by the startup recovery check.
        """
        row = await self.fetch_state()
        return bool(row and row.get("is_active"))

    async def ensure_row_exists(self) -> None:
        """
        Guarantee the singleton row exists.  Called once at startup so
        subsequent upserts never fail on a missing row.
        """
        row = await self.fetch_state()
        if row is None:
            logger.info("bot_state row not found — creating default row")
            await self.upsert_state(_EMPTY_STATE)


# ── Singleton ──────────────────────────────────────────────────────────────────

_db: DatabaseManager | None = None


def get_db() -> DatabaseManager:
    """
    Return the module-level DatabaseManager singleton.
    Raises RuntimeError if init_db() has not been called yet.
    """
    if _db is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _db


def init_db() -> DatabaseManager:
    """
    Create and cache the DatabaseManager singleton.
    Must be called once before any async DB operations, typically in main().
    """
    global _db
    if _db is None:
        _db = DatabaseManager()
    return _db
