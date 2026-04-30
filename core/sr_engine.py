"""
Support & Resistance level detection using price clustering.

Algorithm:
  1. Fetch last SR_LOOKBACK_CANDLES candles on the requested timeframe.
  2. Collect all candle highs and lows as candidate price levels.
  3. Run Kernel Density Estimation (KDE) over those prices to find
     density peaks — areas where price has repeatedly touched/reversed.
  4. Split peaks into supports (below current price) and resistances
     (above current price), return the 2 strongest of each.

Returns SRLevels(supports=[s1, s2], resistances=[r1, r2]) where
  s1 < s2 < current_price < r1 < r2
  (s1 = strongest/closest support, r1 = strongest/closest resistance)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config.settings import SR_LOOKBACK_CANDLES, SR_CLUSTER_BANDWIDTH
from core.mexc_client import MexcClient

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]


@dataclass
class SRLevels:
    supports:     list[float]     # [closest, further] — both below current price
    resistances:  list[float]     # [closest, further] — both above current price
    current_price: float
    timeframe:    str
    symbol:       str


def _kde_peaks(prices: np.ndarray, bandwidth: float, n_points: int = 500) -> np.ndarray:
    """
    Gaussian KDE over prices, return x-values of local density maxima.
    bandwidth is expressed as a fraction of the price range.
    """
    from scipy.stats import gaussian_kde
    from scipy.signal import argrelextrema

    price_range = prices.max() - prices.min()
    if price_range == 0:
        return np.array([prices.mean()])

    bw = bandwidth * price_range / prices.std(ddof=1)
    bw = max(bw, 0.01)   # floor to avoid degenerate KDE

    kde = gaussian_kde(prices, bw_method=bw)
    x   = np.linspace(prices.min(), prices.max(), n_points)
    y   = kde(x)

    # Local maxima with a window of n_points // 20
    order = max(3, n_points // 20)
    peak_idx = argrelextrema(y, np.greater, order=order)[0]
    return x[peak_idx]


def compute_sr_levels(
    candles: list,
    current_price: float,
    symbol: str,
    timeframe: str,
    bandwidth: float = SR_CLUSTER_BANDWIDTH,
) -> Optional[SRLevels]:
    """
    Given raw OHLCV candles, compute 2 support and 2 resistance levels.
    Returns None if there are not enough distinct levels.
    """
    if len(candles) < 20:
        logger.warning("Not enough candles (%d) to compute S/R for %s", len(candles), symbol)
        return None

    # Use highs and lows as candidate reversal prices
    highs = np.array([float(c[2]) for c in candles])
    lows  = np.array([float(c[3]) for c in candles])
    prices = np.concatenate([highs, lows])

    peaks = _kde_peaks(prices, bandwidth)

    if len(peaks) == 0:
        logger.warning("KDE found no peaks for %s", symbol)
        return None

    # Split into supports (below price) and resistances (above price)
    supports    = sorted([p for p in peaks if p < current_price], reverse=True)  # closest first
    resistances = sorted([p for p in peaks if p > current_price])                # closest first

    if len(supports) < 2 or len(resistances) < 2:
        # Fall back: use recent swing lows/highs if clustering gives too few levels
        supports, resistances = _fallback_levels(highs, lows, current_price)

    if len(supports) < 2 or len(resistances) < 2:
        logger.warning(
            "Could not find 2 supports and 2 resistances for %s on %s "
            "(supports=%d, resistances=%d)",
            symbol, timeframe, len(supports), len(resistances),
        )
        return None

    return SRLevels(
        supports     = [round(supports[0], 8), round(supports[1], 8)],
        resistances  = [round(resistances[0], 8), round(resistances[1], 8)],
        current_price = current_price,
        timeframe    = timeframe,
        symbol       = symbol,
    )


def _fallback_levels(
    highs: np.ndarray,
    lows: np.ndarray,
    current_price: float,
) -> tuple[list[float], list[float]]:
    """
    Simple fallback: use the 4 most recent swing lows as supports
    and 4 most recent swing highs as resistances.
    A swing low is a candle low lower than its two neighbours.
    """
    swing_lows  = []
    swing_highs = []
    n = len(lows)
    for i in range(1, n - 1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append(lows[i])
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(highs[i])

    supports    = sorted([p for p in swing_lows  if p < current_price], reverse=True)
    resistances = sorted([p for p in swing_highs if p > current_price])
    return supports, resistances


async def fetch_sr_levels(
    client: MexcClient,
    symbol: str,
    timeframe: str,
) -> Optional[SRLevels]:
    """Fetch candles and compute S/R levels. Returns None on failure."""
    try:
        candles = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=SR_LOOKBACK_CANDLES)
        current_price = await client.get_current_price(symbol)
    except Exception as exc:
        logger.error("fetch_sr_levels failed for %s/%s: %s", symbol, timeframe, exc)
        return None

    return compute_sr_levels(candles, current_price, symbol, timeframe)
