"""
Dashboard REST API — FastAPI app that reads from the same PostgreSQL DB
used by the trading bot. Serves the frontend and exposes JSON endpoints.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

app = FastAPI(title="AI Grid Bot Dashboard", docs_url=None, redoc_url=None)

_pool: asyncpg.Pool | None = None


# ── DB lifecycle ───────────────────────────────────────────────────────────────

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _pool = await asyncpg.create_pool(
            url, min_size=1, max_size=5, statement_cache_size=0
        )
    return _pool


@app.on_event("startup")
async def startup():
    try:
        await get_pool()
        logger.info("Dashboard DB pool ready")
    except Exception as exc:
        logger.warning("DB unavailable at startup (will retry on first request): %s", exc)


@app.on_event("shutdown")
async def shutdown():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _row(r) -> dict:
    """Convert asyncpg Record to plain dict, serialising non-JSON types."""
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


async def _fetch(sql: str, *args) -> list[dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [_row(r) for r in rows]
    except Exception:
        return []


async def _fetchrow(sql: str, *args) -> dict | None:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
        return _row(row) if row else None
    except Exception:
        return None


async def _fetchval(sql: str, *args) -> Any:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(sql, *args)
    except Exception:
        return None


# ── API endpoints ──────────────────────────────────────────────────────────────

@app.get("/api/summary")
async def summary():
    """High-level stats card data."""
    grids = await _fetch("SELECT * FROM active_grids WHERE is_active = TRUE")
    super_bots = await _fetch(
        "SELECT * FROM super_consensus_bots WHERE is_active = TRUE"
    ) if await _table_exists("super_consensus_bots") else []

    total_pnl_grid = sum(float(g.get("realized_pnl") or 0) for g in grids)
    total_pnl_super = sum(float(b.get("realized_pnl") or 0) for b in super_bots)

    total_trades_grid = await _fetchval(
        "SELECT COUNT(*) FROM trade_history"
    ) or 0
    total_trades_super = (
        await _fetchval("SELECT COUNT(*) FROM super_consensus_trades") or 0
    ) if await _table_exists("super_consensus_trades") else 0

    auto_active = False
    if await _table_exists("auto_mode_settings"):
        row = await _fetchrow("SELECT is_active FROM auto_mode_settings LIMIT 1")
        auto_active = bool(row.get("is_active")) if row else False

    open_auto_positions = 0
    if await _table_exists("auto_mode_positions"):
        open_auto_positions = await _fetchval(
            "SELECT COUNT(*) FROM auto_mode_positions WHERE is_open = TRUE"
        ) or 0

    return {
        "grid_bots_active": len(grids),
        "super_bots_active": len(super_bots),
        "auto_mode_active": auto_active,
        "open_auto_positions": int(open_auto_positions),
        "total_pnl_grid": round(total_pnl_grid, 4),
        "total_pnl_super": round(total_pnl_super, 4),
        "total_pnl": round(total_pnl_grid + total_pnl_super, 4),
        "total_trades": int(total_trades_grid) + int(total_trades_super),
    }


@app.get("/api/grids")
async def grids():
    """All active grid bots."""
    rows = await _fetch(
        "SELECT * FROM active_grids WHERE is_active = TRUE ORDER BY started_at DESC"
    )
    return rows


@app.get("/api/grids/{symbol}/trades")
async def grid_trades(symbol: str, days: int = 7):
    rows = await _fetch(
        """SELECT * FROM trade_history
           WHERE symbol = $1 AND executed_at >= NOW() - ($2 * INTERVAL '1 day')
           ORDER BY executed_at DESC LIMIT 200""",
        symbol, days,
    )
    return rows


@app.get("/api/super_bots")
async def super_bots():
    if not await _table_exists("super_consensus_bots"):
        return []
    return await _fetch(
        "SELECT * FROM super_consensus_bots WHERE is_active = TRUE ORDER BY started_at DESC"
    )


@app.get("/api/super_bots/{symbol}/trades")
async def super_bot_trades(symbol: str, days: int = 7):
    if not await _table_exists("super_consensus_trades"):
        return []
    return await _fetch(
        """SELECT * FROM super_consensus_trades
           WHERE symbol = $1 AND executed_at >= NOW() - ($2 * INTERVAL '1 day')
           ORDER BY executed_at DESC LIMIT 200""",
        symbol, days,
    )


@app.get("/api/auto_mode")
async def auto_mode():
    if not await _table_exists("auto_mode_settings"):
        return {"is_active": False, "positions": []}
    settings = await _fetchrow("SELECT * FROM auto_mode_settings LIMIT 1") or {}
    positions = []
    if await _table_exists("auto_mode_positions"):
        positions = await _fetch(
            "SELECT * FROM auto_mode_positions WHERE is_open = TRUE ORDER BY opened_at DESC"
        )
    return {"settings": settings, "positions": positions}


@app.get("/api/trades/recent")
async def recent_trades(limit: int = 50):
    """Latest trades across all bots."""
    grid_trades = await _fetch(
        f"SELECT symbol, side, price, qty, pnl, executed_at, 'grid' AS source FROM trade_history ORDER BY executed_at DESC LIMIT {limit}"
    )
    super_trades: list[dict] = []
    if await _table_exists("super_consensus_trades"):
        super_trades = await _fetch(
            f"SELECT symbol, side, entry_price AS price, qty, pnl, executed_at, 'super' AS source FROM super_consensus_trades ORDER BY executed_at DESC LIMIT {limit}"
        )
    combined = sorted(
        grid_trades + super_trades,
        key=lambda x: x.get("executed_at", ""),
        reverse=True,
    )[:limit]
    return combined


@app.get("/api/pnl/chart")
async def pnl_chart(days: int = 30):
    """Cumulative PnL per day for charting."""
    rows = await _fetch(
        """SELECT DATE(executed_at) AS day, SUM(pnl) AS daily_pnl
           FROM trade_history
           WHERE executed_at >= NOW() - ($1 * INTERVAL '1 day')
           GROUP BY day ORDER BY day""",
        days,
    )
    if await _table_exists("super_consensus_trades"):
        super_rows = await _fetch(
            """SELECT DATE(executed_at) AS day, SUM(pnl) AS daily_pnl
               FROM super_consensus_trades
               WHERE executed_at >= NOW() - ($1 * INTERVAL '1 day')
               GROUP BY day ORDER BY day""",
            days,
        )
        # Merge by day
        merged: dict[str, float] = {}
        for r in rows:
            merged[r["day"]] = float(r["daily_pnl"] or 0)
        for r in super_rows:
            merged[r["day"]] = merged.get(r["day"], 0) + float(r["daily_pnl"] or 0)
        rows = [{"day": str(k), "daily_pnl": round(v, 4)} for k, v in sorted(merged.items())]

    # Build cumulative
    cumulative = 0.0
    result = []
    for r in rows:
        cumulative += float(r.get("daily_pnl") or 0)
        result.append({"day": str(r["day"]), "daily_pnl": round(float(r.get("daily_pnl") or 0), 4), "cumulative": round(cumulative, 4)})
    return result


@app.get("/api/logs/super")
async def super_logs(symbol: str | None = None, limit: int = 100):
    if not await _table_exists("super_consensus_logs"):
        return []
    if symbol:
        return await _fetch(
            "SELECT * FROM super_consensus_logs WHERE symbol = $1 ORDER BY timestamp DESC LIMIT $2",
            symbol, limit,
        )
    return await _fetch(
        "SELECT * FROM super_consensus_logs ORDER BY timestamp DESC LIMIT $1", limit
    )


# ── Utility ────────────────────────────────────────────────────────────────────

async def _table_exists(name: str) -> bool:
    val = await _fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=$1)", name
    )
    return bool(val)


# ── Static files & SPA ────────────────────────────────────────────────────────

_static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = _static / "index.html"
    return HTMLResponse(html_file.read_text(encoding="utf-8"))
