"""
Support & Resistance level detection.

Matches the LuxAlgo "Support & Resistance Channels" indicator:

  Resistance (red lines)  = swing highs: local price peaks
  Support    (blue lines) = swing lows:  local price troughs

A swing high at index i: high[i] is the highest in a window of
  SR_PIVOT_LEFT bars to the left and SR_PIVOT_RIGHT bars to the right.
A swing low  at index i: low[i]  is the lowest  in the same window.

The level value is the exact high/low of that candle — matching the
horizontal lines drawn by the indicator.

Levels are then:
  1. Merged if within SR_MERGE_THRESHOLD % of each other.
  2. Filtered: must be at least SR_MIN_DISTANCE_PCT % away from price.
  3. Ranked: unbroken levels (price never closed through them) first,
     then by number of times price returned to test the level.
  4. Sorted by proximity: S1 = nearest below, R1 = nearest above.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config.settings import (
    SR_LOOKBACK_CANDLES,
    SR_PIVOT_LEFT,
    SR_PIVOT_RIGHT,
    SR_MERGE_THRESHOLD,
    SR_MIN_DISTANCE_PCT,
    SR_TOUCH_ZONE_PCT,
)
from core.mexc_client import MexcClient

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]


@dataclass
class SRLevels:
    supports:      list[float]   # nearest first (descending), all below current price
    resistances:   list[float]   # nearest first (ascending),  all above current price
    current_price: float
    timeframe:     str
    symbol:        str


# ── Swing detection ────────────────────────────────────────────────────────────

def _find_swing_highs(highs: np.ndarray, left: int, right: int) -> list[float]:
    """Return high values at swing-high candles."""
    n = len(highs)
    levels = []
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == window.max() and np.sum(window == highs[i]) == 1:
            levels.append(float(highs[i]))
    return levels


def _find_swing_lows(lows: np.ndarray, left: int, right: int) -> list[float]:
    """Return low values at swing-low candles."""
    n = len(lows)
    levels = []
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == window.min() and np.sum(window == lows[i]) == 1:
            levels.append(float(lows[i]))
    return levels


# ── Scoring helpers ────────────────────────────────────────────────────────────

def _count_touches(
    level: float,
    highs: np.ndarray,
    lows:  np.ndarray,
    zone_pct: float,
    side: str,
) -> int:
    zone = level * zone_pct / 100
    if side == "support":
        return int(np.sum(np.abs(lows - level) <= zone))
    else:
        return int(np.sum(np.abs(highs - level) <= zone))


def _is_broken(level: float, closes: np.ndarray, side: str) -> bool:
    if side == "support":
        return bool(np.any(closes < level))
    else:
        return bool(np.any(closes > level))


# ── Level merging ──────────────────────────────────────────────────────────────

def _merge_levels(levels: list[float], threshold_pct: float) -> list[float]:
    if not levels:
        return []
    sorted_levels = sorted(levels)
    merged = [sorted_levels[0]]
    for price in sorted_levels[1:]:
        if abs(price - merged[-1]) / merged[-1] <= threshold_pct / 100:
            merged[-1] = (merged[-1] + price) / 2
        else:
            merged.append(price)
    return merged


# ── Main computation ───────────────────────────────────────────────────────────

def compute_sr_levels(
    candles: list,
    current_price: float,
    symbol: str,
    timeframe: str,
    pivot_left:       int   = SR_PIVOT_LEFT,
    pivot_right:      int   = SR_PIVOT_RIGHT,
    merge_threshold:  float = SR_MERGE_THRESHOLD,
    min_distance_pct: float = SR_MIN_DISTANCE_PCT,
    touch_zone_pct:   float = SR_TOUCH_ZONE_PCT,
    num_levels:       int   = 2,
) -> Optional[SRLevels]:
    """
    Compute num_levels support and resistance levels from swing highs/lows.
    Returns None if not enough levels found.
    """
    min_candles = pivot_left + pivot_right + 5
    if len(candles) < min_candles:
        logger.warning(
            "Not enough candles (%d) to compute S/R for %s (need %d)",
            len(candles), symbol, min_candles,
        )
        return None

    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    # 1. Detect swing highs/lows
    raw_resistances = _find_swing_highs(highs, pivot_left, pivot_right)
    raw_supports    = _find_swing_lows(lows,   pivot_left, pivot_right)

    # 2. Merge nearby levels
    raw_resistances = _merge_levels(raw_resistances, merge_threshold)
    raw_supports    = _merge_levels(raw_supports,    merge_threshold)

    # 3. Distance filter — keep only levels outside min_distance_pct of price
    min_dist        = current_price * min_distance_pct / 100
    raw_supports    = [p for p in raw_supports    if p < current_price - min_dist]
    raw_resistances = [p for p in raw_resistances if p > current_price + min_dist]

    # 4. Rank: unbroken levels first, then by touch count
    def _rank(levels: list[float], side: str) -> list[float]:
        unbroken, broken = [], []
        for lvl in levels:
            touches = _count_touches(lvl, highs, lows, touch_zone_pct, side)
            bucket  = broken if _is_broken(lvl, closes, side) else unbroken
            bucket.append((touches, lvl))
        unbroken.sort(key=lambda x: x[0], reverse=True)
        broken.sort(key=lambda x: x[0], reverse=True)
        return [lvl for _, lvl in unbroken + broken]

    supports    = _rank(raw_supports,    "support")
    resistances = _rank(raw_resistances, "resistance")

    # 5. Fallback if not enough levels found
    if len(supports) < num_levels or len(resistances) < num_levels:
        # Relax distance filter to half and retry
        half_dist = min_dist / 2
        if len(supports) < num_levels:
            extra = _rank(
                [p for p in _find_swing_lows(lows, pivot_left, pivot_right)
                 if p < current_price - half_dist and p not in supports],
                "support",
            )
            supports = (supports + extra)[:num_levels]
        if len(resistances) < num_levels:
            extra = _rank(
                [p for p in _find_swing_highs(highs, pivot_left, pivot_right)
                 if p > current_price + half_dist and p not in resistances],
                "resistance",
            )
            resistances = (resistances + extra)[:num_levels]

    if len(supports) < num_levels or len(resistances) < num_levels:
        logger.warning(
            "Could not find %d S/%d R for %s on %s (found sup=%d res=%d)",
            num_levels, num_levels, symbol, timeframe,
            len(supports), len(resistances),
        )
        return None

    # 6. Sort by proximity to current price
    #    supports    → descending (S1 = nearest below price)
    #    resistances → ascending  (R1 = nearest above price)
    supports    = sorted(supports[:num_levels],    reverse=True)
    resistances = sorted(resistances[:num_levels], reverse=False)

    return SRLevels(
        supports      = [round(p, 8) for p in supports],
        resistances   = [round(p, 8) for p in resistances],
        current_price = current_price,
        timeframe     = timeframe,
        symbol        = symbol,
    )


# ── Async fetch wrapper ────────────────────────────────────────────────────────

async def fetch_sr_levels(
    client: MexcClient,
    symbol: str,
    timeframe: str,
    num_levels: int = 2,
) -> Optional[SRLevels]:
    """Fetch candles and compute S/R levels. Returns None on failure."""
    try:
        candles       = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=SR_LOOKBACK_CANDLES)
        current_price = await client.get_current_price(symbol)
    except Exception as exc:
        logger.error("fetch_sr_levels failed for %s/%s: %s", symbol, timeframe, exc)
        return None

    return compute_sr_levels(candles, current_price, symbol, timeframe, num_levels=num_levels)
