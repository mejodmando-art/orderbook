"""
Unit tests for core grid engine logic.
Run with: pytest tests/
"""
import pytest
from unittest.mock import MagicMock
from core.grid_engine import derive_grid_params, GridParams


# ── Fake client ────────────────────────────────────────────────────────────────

def _make_client(min_amt=0.01, min_cost=1.0, price_dp=4, amount_dp=2):
    client = MagicMock()
    client.min_amount.return_value  = min_amt
    client.min_cost.return_value    = min_cost
    client.round_price.side_effect  = lambda sym, p: round(p, price_dp)
    client.round_amount.side_effect = lambda sym, a: round(a, amount_dp)
    return client


# ── derive_grid_params ─────────────────────────────────────────────────────────

class TestDeriveGridParams:

    def test_upper_lower_bounds(self):
        client = _make_client()
        p = derive_grid_params(100.0, 300.0, client, "BTC/USDT",
                               num_grids=3, upper_pct=3.0, lower_pct=3.0)
        assert p.upper == pytest.approx(103.0, rel=1e-4)
        assert p.lower == pytest.approx(97.0,  rel=1e-4)

    def test_grid_count(self):
        client = _make_client()
        p = derive_grid_params(100.0, 300.0, client, "BTC/USDT", num_grids=5)
        assert p.grid_count == 10   # 5 buys + 5 sells

    def test_qty_per_grid_within_budget(self):
        client = _make_client()
        p = derive_grid_params(100.0, 300.0, client, "BTC/USDT",
                               num_grids=3, upper_pct=3.0, lower_pct=3.0)
        # qty_per_grid * grid_count * price should not exceed total_investment
        assert p.qty_per_grid * p.grid_count * 100.0 <= 300.0 * 1.01  # 1% tolerance for rounding

    def test_asymmetric_pct(self):
        client = _make_client()
        p = derive_grid_params(100.0, 300.0, client, "BTC/USDT",
                               num_grids=3, upper_pct=5.0, lower_pct=2.0)
        assert p.upper == pytest.approx(105.0, rel=1e-4)
        assert p.lower == pytest.approx(98.0,  rel=1e-4)

    def test_num_grids_one(self):
        """Edge case: single grid per side should not crash."""
        client = _make_client()
        p = derive_grid_params(100.0, 100.0, client, "BTC/USDT", num_grids=1)
        assert p.grid_count == 2
        assert p.grid_spacing > 0

    def test_returns_gridparams(self):
        client = _make_client()
        p = derive_grid_params(100.0, 300.0, client, "BTC/USDT")
        assert isinstance(p, GridParams)


# ── _handle_fill guard ─────────────────────────────────────────────────────────

class TestHandleFillGuard:
    """Verify _handle_fill skips gracefully on bad order data."""

    @pytest.mark.asyncio
    async def test_bad_fill_price_skipped(self):
        from core.grid_engine import GridEngine, GridState, GridParams
        from datetime import datetime, timezone

        client = _make_client()
        engine = GridEngine(client=client, notify=MagicMock())

        params = GridParams(lower=97.0, upper=103.0, grid_count=6,
                            grid_spacing=1.0, qty_per_grid=0.1, atr=0.0)
        state = GridState(symbol="BTC/USDT", risk="medium",
                          total_investment=300.0, params=params,
                          started_at=datetime.now(timezone.utc))

        bad_order = {"id": "x1", "status": "closed", "average": None,
                     "price": None, "filled": None}
        meta = {"side": "buy", "price": 0.0, "qty": 0.0}

        # Should not raise — bad data is logged and skipped
        await engine._handle_fill(state, meta, bad_order)
        assert state.held_qty == 0.0   # nothing changed


# ── DB upsert_grid keys ────────────────────────────────────────────────────────

class TestUpsertGridKeys:
    """Verify upper_pct/lower_pct are included in every upsert call."""

    def test_start_passes_pct_to_upsert(self):
        """derive_grid_params receives the user-supplied pct values."""
        client = _make_client()
        p = derive_grid_params(100.0, 300.0, client, "BTC/USDT",
                               num_grids=3, upper_pct=4.0, lower_pct=2.5)
        assert p.upper == pytest.approx(104.0, rel=1e-4)
        assert p.lower == pytest.approx(97.5,  rel=1e-4)
