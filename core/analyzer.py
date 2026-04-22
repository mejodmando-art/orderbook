"""
Market analysis: order-book wall detection and SMA-20 trend filter.

Wall detection rule:
    A price level is a "wall" when its volume exceeds the rolling average
    volume of all visible levels by a factor of `wall_multiplier` (default 3×).

SMA-20 filter:
    Only take long entries when the current price is above the 20-period
    Simple Moving Average of recent trade prices.  This avoids buying into
    a downtrend.

Data flow:
    - Depth (order-book) snapshots arrive via the WS depth channel.
    - Trade prices arrive via the WS trade channel.
    - Both are fed into this module through update_depth() / update_price().
    - The bot calls find_entry_signal() to get a trade decision.
"""

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class WallLevel:
    price: float
    volume: float
    side: str  # "bid" | "ask"


@dataclass
class EntrySignal:
    """Returned by find_entry_signal() when conditions are met."""
    wall_price: float          # price of the detected support wall
    entry_price: float         # suggested limit-buy price (just above wall)
    wall_volume: float
    sma: float
    current_price: float


class MarketAnalyzer:
    """
    Stateful analyzer that consumes live order-book and trade data.

    Thread-safety: designed for single-threaded asyncio use only.
    """

    def __init__(
        self,
        wall_multiplier: float = 3.0,
        sma_period: int = 20,
        price_precision: int = 8,
    ) -> None:
        self.wall_multiplier = wall_multiplier
        self.sma_period = sma_period
        self.price_precision = price_precision

        # Rolling window of recent trade prices for SMA
        self._prices: deque[float] = deque(maxlen=sma_period)

        # Latest order-book snapshot: list of [price_str, qty_str]
        self._bids: list[list[str]] = []
        self._asks: list[list[str]] = []

        self._current_price: float = 0.0

    # ── Feed methods ───────────────────────────────────────────────────────────

    def update_price(self, price: float) -> None:
        """Append a new trade price to the SMA window."""
        self._current_price = price
        self._prices.append(price)

    def update_depth(self, bids: list[list[str]], asks: list[list[str]]) -> None:
        """Replace the current order-book snapshot."""
        self._bids = bids
        self._asks = asks

    # ── Analysis ───────────────────────────────────────────────────────────────

    def sma(self) -> float | None:
        """Return the current SMA-20, or None if not enough data yet."""
        if len(self._prices) < self.sma_period:
            return None
        return statistics.mean(self._prices)

    def find_walls(self, side: str = "bid") -> list[WallLevel]:
        """
        Scan one side of the book for volume walls.

        A level qualifies as a wall when:
            level_volume > mean(all_volumes) * wall_multiplier

        Returns levels sorted by volume descending.
        """
        levels = self._bids if side == "bid" else self._asks
        if not levels:
            return []

        parsed: list[tuple[float, float]] = []
        for row in levels:
            try:
                price = float(row[0])
                qty = float(row[1])
                parsed.append((price, qty))
            except (ValueError, IndexError):
                continue

        if not parsed:
            return []

        volumes = [qty for _, qty in parsed]
        avg_vol = statistics.mean(volumes)
        threshold = avg_vol * self.wall_multiplier

        walls = [
            WallLevel(price=p, volume=v, side=side)
            for p, v in parsed
            if v >= threshold
        ]
        walls.sort(key=lambda w: w.volume, reverse=True)
        logger.debug("Found %d %s walls (threshold=%.4f)", len(walls), side, threshold)
        return walls

    def find_entry_signal(self, tick_size: float = 0.01) -> EntrySignal | None:
        """
        Return an EntrySignal when all conditions are met, else None.

        Conditions:
        1. SMA-20 is available (enough price history).
        2. Current price is above SMA-20 (uptrend filter).
        3. At least one bid wall exists below the current price.

        Entry price is placed one tick_size above the wall to sit just
        in front of it, benefiting from the wall's support.
        """
        current = self._current_price
        if current <= 0:
            logger.debug("No current price yet")
            return None

        sma = self.sma()
        if sma is None:
            logger.debug("SMA not ready (%d/%d prices)", len(self._prices), self.sma_period)
            return None

        if current <= sma:
            logger.debug("Price %.8f below SMA %.8f — no entry", current, sma)
            return None

        bid_walls = self.find_walls("bid")
        if not bid_walls:
            logger.debug("No bid walls detected")
            return None

        # Pick the strongest wall that is below the current price
        valid_walls = [w for w in bid_walls if w.price < current]
        if not valid_walls:
            logger.debug("All walls are at or above current price")
            return None

        best_wall = valid_walls[0]
        entry_price = round(best_wall.price + tick_size, self.price_precision)

        logger.info(
            "Entry signal: wall=%.8f entry=%.8f vol=%.4f sma=%.8f price=%.8f",
            best_wall.price, entry_price, best_wall.volume, sma, current,
        )
        return EntrySignal(
            wall_price=best_wall.price,
            entry_price=entry_price,
            wall_volume=best_wall.volume,
            sma=sma,
            current_price=current,
        )

    def reset(self) -> None:
        """Clear all accumulated data (called between trades)."""
        self._prices.clear()
        self._bids = []
        self._asks = []
        self._current_price = 0.0
        logger.debug("Analyzer state reset")
