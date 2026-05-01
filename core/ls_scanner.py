"""
Liquidity Swings Scanner.

Fetches Top 100 coins by market cap (CoinGecko → CoinPaprika fallback),
then analyses each one for active Swing Low zones using the LS engine.

Returns a ranked list of coins that have at least one active, untouched
Swing Low zone with enough touches — ready to trade.

This module is synchronous (uses urllib) so it can be called from an
asyncio executor without blocking the event loop.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from core.liquidity_swings_engine import (
    build_all_zones,
    SwingZone,
    LS_PIVOT_LEFT,
    LS_PIVOT_RIGHT,
    LS_MIN_TOUCHES,
    LS_SWING_AREA,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

MARKET_CACHE_TTL = 300   # 5 minutes

STABLECOINS = {
    "usdt", "usdc", "busd", "dai", "tusd", "usdp", "usdd",
    "frax", "lusd", "susd", "gusd", "fdusd", "pyusd",
    "usd1", "usde", "usds", "crvusd",
    "xaut", "paxg",
    "wbtc", "steth", "weth", "cbbtc",
}

_market_cache: dict[int, tuple] = {}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class CoinData:
    symbol:     str
    name:       str
    price:      float
    market_cap: float
    volume_24h: float
    change_24h: float
    rank:       int


@dataclass
class LSOpportunity:
    symbol:      str          # e.g. "BTC/USDT"
    name:        str
    rank:        int
    price:       float
    zone:        SwingZone    # the best active Swing Low zone
    tp_price:    float        # nearest Swing High above price
    sl_price:    float        # zone.pivot_price * (1 - sl_buffer)
    touch_count: int
    change_24h:  float


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _get_cached(count: int) -> Optional[list[CoinData]]:
    cached = _market_cache.get(count)
    if cached:
        ts, coins = cached
        if time.monotonic() - ts < MARKET_CACHE_TTL:
            return coins
    return None


def _set_cache(count: int, coins: list[CoinData]) -> None:
    _market_cache[count] = (time.monotonic(), coins)


# ── Market data fetch ──────────────────────────────────────────────────────────

def _fetch_coingecko_raw(count: int, retries: int = 3) -> list:
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={count}&page=1"
        "&sparkline=false&price_change_percentage=24h"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "LSBot/1.0"})
    last_exc: Exception = RuntimeError("No attempts")
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                wait = 10 * attempt
                logger.warning("CoinGecko 429 — sleeping %ds", wait)
                time.sleep(wait)
            else:
                raise
        except Exception as exc:
            last_exc = exc
            time.sleep(3)
    raise last_exc


def _parse_coingecko(raw: list) -> list[CoinData]:
    coins = []
    for i, item in enumerate(raw, start=1):
        sym = (item.get("symbol") or "").lower()
        if sym in STABLECOINS:
            continue
        coins.append(CoinData(
            symbol     = sym,
            name       = item.get("name", ""),
            price      = float(item.get("current_price") or 0),
            market_cap = float(item.get("market_cap") or 0),
            volume_24h = float(item.get("total_volume") or 0),
            change_24h = float(item.get("price_change_percentage_24h") or 0),
            rank       = i,
        ))
    return coins


def _fetch_coinpaprika_fallback(count: int) -> list[CoinData]:
    with urllib.request.urlopen(
        urllib.request.Request(
            "https://api.coinpaprika.com/v1/tickers",
            headers={"User-Agent": "LSBot/1.0"},
        ),
        timeout=30,
    ) as r:
        raw = json.loads(r.read())

    coins, rank = [], 0
    for item in raw:
        sym = (item.get("symbol") or "").lower()
        if sym in STABLECOINS:
            continue
        rank += 1
        if rank > count:
            break
        q = item.get("quotes", {}).get("USD", {})
        coins.append(CoinData(
            symbol     = sym,
            name       = item.get("name", ""),
            price      = float(q.get("price") or 0),
            market_cap = float(q.get("market_cap") or 0),
            volume_24h = float(q.get("volume_24h") or 0),
            change_24h = float(q.get("percent_change_24h") or 0),
            rank       = rank,
        ))
    return coins


def fetch_top_coins(count: int = 100) -> list[CoinData]:
    cached = _get_cached(count)
    if cached:
        return cached
    try:
        raw   = _fetch_coingecko_raw(count)
        coins = _parse_coingecko(raw)
        source = "CoinGecko"
    except Exception as cg_exc:
        logger.warning("CoinGecko failed (%s) — trying CoinPaprika", cg_exc)
        try:
            coins  = _fetch_coinpaprika_fallback(count)
            source = "CoinPaprika"
        except Exception as cp_exc:
            raise RuntimeError(
                f"Both sources failed. CoinGecko: {cg_exc} | CoinPaprika: {cp_exc}"
            ) from cp_exc
    _set_cache(count, coins)
    logger.info("%s: %d coins fetched", source, len(coins))
    return coins


# ── OHLCV fetch from MEXC via ccxt (sync wrapper) ─────────────────────────────

def _fetch_ohlcv_sync(symbol: str, timeframe: str, limit: int, client) -> list:
    """
    Synchronous OHLCV fetch — runs inside asyncio.to_thread so it must not
    use await. We call the ccxt exchange object's synchronous fetch_ohlcv.
    """
    try:
        exchange = client._exchange   # the underlying ccxt exchange object
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as exc:
        logger.debug("OHLCV fetch failed for %s: %s", symbol, exc)
        return []


# ── Zone analysis ──────────────────────────────────────────────────────────────

def analyse_coin(
    candles:       list,
    symbol:        str,
    coin:          CoinData,
    sl_buffer_pct: float = 0.3,
    min_touches:   int   = LS_MIN_TOUCHES,
    pivot_left:    int   = LS_PIVOT_LEFT,
    pivot_right:   int   = LS_PIVOT_RIGHT,
    swing_area:    str   = LS_SWING_AREA,
) -> Optional[LSOpportunity]:
    """
    Analyse a single coin's candles for an active Swing Low opportunity.
    Returns the best opportunity or None.
    """
    if len(candles) < pivot_left + pivot_right + 10:
        return None

    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    high_zones, low_zones = build_all_zones(
        highs, lows, opens, closes,
        pivot_left, pivot_right, swing_area,
    )

    current_price = float(closes[-1])

    # Find active Swing Low zones with enough touches
    active_lows = [
        z for z in low_zones
        if not z.crossed and z.touch_count >= min_touches
        and z.zone_bottom <= current_price <= z.zone_top * 1.02  # price near zone
    ]
    if not active_lows:
        return None

    # Find nearest active Swing High above price for TP
    active_highs = [
        z for z in high_zones
        if not z.crossed and z.pivot_price > current_price
    ]
    if not active_highs:
        return None

    best_low  = max(active_lows,  key=lambda z: z.touch_count)
    best_high = min(active_highs, key=lambda z: z.pivot_price)

    sl_price = best_low.pivot_price * (1 - sl_buffer_pct / 100)

    return LSOpportunity(
        symbol      = symbol,
        name        = coin.name,
        rank        = coin.rank,
        price       = current_price,
        zone        = best_low,
        tp_price    = best_high.pivot_price,
        sl_price    = sl_price,
        touch_count = best_low.touch_count,
        change_24h  = coin.change_24h,
    )


# ── Main scanner ───────────────────────────────────────────────────────────────

async def scan_top_coins(
    client,
    timeframe:     str   = "1h",
    coin_count:    int   = 100,
    min_touches:   int   = LS_MIN_TOUCHES,
    sl_buffer_pct: float = 0.3,
    lookback:      int   = 300,
    max_results:   int   = 20,
) -> list[LSOpportunity]:
    """
    Async entry point. Fetches Top `coin_count` coins, analyses each for
    active LS Swing Low zones, and returns up to `max_results` opportunities
    sorted by touch_count descending.
    """
    import asyncio

    # Fetch market data in thread (sync HTTP)
    coins = await asyncio.to_thread(fetch_top_coins, coin_count)

    opportunities: list[LSOpportunity] = []
    skipped = 0

    for coin in coins:
        symbol = f"{coin.symbol.upper()}/USDT"
        try:
            candles = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=lookback)
        except Exception:
            skipped += 1
            continue

        if not candles:
            skipped += 1
            continue

        opp = analyse_coin(
            candles, symbol, coin,
            sl_buffer_pct = sl_buffer_pct,
            min_touches   = min_touches,
        )
        if opp:
            opportunities.append(opp)

        # Small pause to respect MEXC rate limits
        await asyncio.sleep(0.25)

    logger.info(
        "LS scan complete: %d coins analysed, %d skipped, %d opportunities found",
        len(coins) - skipped, skipped, len(opportunities),
    )

    # Sort: most touches first, then by rank (lower = bigger coin)
    opportunities.sort(key=lambda o: (-o.touch_count, o.rank))
    return opportunities[:max_results]
