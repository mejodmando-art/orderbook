"""
Support & Resistance level detection — Order Block / Liquidity method.

Algorithm (mirrors the indicator visible in the screenshot):
  1. Fetch SR_LOOKBACK_CANDLES candles on the requested timeframe.
  2. Detect Swing Highs and Swing Lows using SR_PIVOT_LEFT/RIGHT bars.
  3. For each swing high: the candle body top (max of open/close) is the
     resistance level.  For each swing low: the candle body bottom
     (min of open/close) is the support level.
     Using the body instead of the wick gives cleaner order-block levels
     (matches the red/blue horizontal bars in the screenshot).
  4. Score each level by the number of subsequent candles that returned
     to test it (touches within SR_TOUCH_ZONE_PCT).
  5. Keep only UNBROKEN levels — a support is broken when a candle
     closes below it; a resistance when a candle closes above it.
     Broken levels are included as fallback only if not enough unbroken
     ones exist.
  6. Merge levels within SR_MERGE_THRESHOLD % of each other.
  7. Apply SR_MIN_DISTANCE_PCT: discard levels too close to current price.
  8. Sort supports descending (nearest first) and resistances ascending
     (nearest first), then return the top num_levels of each.
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

def _swing_highs(highs: np.ndarray, left: int, right: int) -> list[int]:
    """Return indices of candles that are swing highs."""
    n = len(highs)
    indices = []
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == window.max() and np.sum(window == highs[i]) == 1:
            indices.append(i)
    return indices


def _swing_lows(lows: np.ndarray, left: int, right: int) -> list[int]:
    """Return indices of candles that are swing lows."""
    n = len(lows)
    indices = []
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == window.min() and np.sum(window == lows[i]) == 1:
            indices.append(i)
    return indices


# ── Touch scoring ──────────────────────────────────────────────────────────────

def _count_touches(
    level:     float,
    highs:     np.ndarray,
    lows:      np.ndarray,
    zone_pct:  float,
    side:      str,
    after_idx: int = 0,
) -> int:
    """
    Count candles after after_idx that tested the level within zone_pct %.
    Support: candle low within zone. Resistance: candle high within zone.
    """
    zone = level * zone_pct / 100
    if side == "support":
        return int(np.sum(np.abs(lows[after_idx:] - level) <= zone))
    else:
        return int(np.sum(np.abs(highs[after_idx:] - level) <= zone))


def _is_broken(level: float, closes: np.ndarray, side: str, after_idx: int = 0) -> bool:
    """Return True if price closed through the level after after_idx."""
    if side == "support":
        return bool(np.any(closes[after_idx:] < level))
    else:
        return bool(np.any(closes[after_idx:] > level))


# ── Level merging ──────────────────────────────────────────────────────────────

def _merge_levels(levels: list[float], threshold_pct: float) -> list[float]:
    """Merge levels within threshold_pct % of each other into their average."""
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
    Compute num_levels support and resistance levels using order-block logic.
    Returns None if not enough levels found.
    """
    min_candles = pivot_left + pivot_right + 5
    if len(candles) < min_candles:
        logger.warning(
            "Not enough candles (%d) to compute S/R for %s (need %d)",
            len(candles), symbol, min_candles,
        )
        return None

    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    # 1. Detect swing indices
    sh_indices = _swing_highs(highs, pivot_left, pivot_right)
    sl_indices = _swing_lows(lows,   pivot_left, pivot_right)

    # 2. Extract order-block levels from candle bodies
    #    Resistance = top of body at swing high  (max of open/close)
    #    Support    = bottom of body at swing low (min of open/close)
    raw_resistances = [float(max(opens[i], closes[i])) for i in sh_indices]
    raw_supports    = [float(min(opens[i], closes[i])) for i in sl_indices]

    # 3. Merge nearby levels
    raw_resistances = _merge_levels(raw_resistances, merge_threshold)
    raw_supports    = _merge_levels(raw_supports,    merge_threshold)

    # 4. Apply minimum distance filter
    min_dist        = current_price * min_distance_pct / 100
    raw_supports    = [p for p in raw_supports    if p < current_price - min_dist]
    raw_resistances = [p for p in raw_resistances if p > current_price + min_dist]

    # 5. Score and rank — unbroken levels first, then broken as fallback
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

    # 6. Fallback if not enough levels
    if len(supports) < num_levels or len(resistances) < num_levels:
        fb_sup, fb_res = _fallback_levels(highs, lows, current_price, min_dist)
        if len(supports) < num_levels:
            supports = fb_sup
        if len(resistances) < num_levels:
            resistances = fb_res

    if len(supports) < num_levels or len(resistances) < num_levels:
        logger.warning(
            "Could not find %d supports/%d resistances for %s on %s "
            "(found: sup=%d res=%d)",
            num_levels, num_levels, symbol, timeframe,
            len(supports), len(resistances),
        )
        return None

    # 7. Sort by proximity to current price
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


def _fallback_levels(
    highs:         np.ndarray,
    lows:          np.ndarray,
    current_price: float,
    min_dist:      float,
) -> tuple[list[float], list[float]]:
    """
    Fallback: simple swing lows/highs when order-block detection finds
    too few levels. Sorted nearest-first.
    """
    swing_lows, swing_highs = [], []
    n = len(lows)
    for i in range(1, n - 1):
        if lows[i]  < lows[i - 1]  and lows[i]  < lows[i + 1]:
            swing_lows.append(float(lows[i]))
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(float(highs[i]))

    supports    = sorted(
        [p for p in swing_lows  if p < current_price - min_dist],
        reverse=True,
    )
    resistances = sorted(
        [p for p in swing_highs if p > current_price + min_dist],
        reverse=False,
    )
    return supports, resistances


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
