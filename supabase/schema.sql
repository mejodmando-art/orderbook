-- ─────────────────────────────────────────────────────────────────────────────
-- MEXC Market Maker Bot — reference schema
--
-- The bot runs CREATE TABLE IF NOT EXISTS automatically on startup via
-- db_manager.ensure_table(), so you do NOT need to run this manually.
--
-- This file is kept as a reference and for manual inspection / recovery.
-- All statements are idempotent — safe to run at any time.
-- ─────────────────────────────────────────────────────────────────────────────

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

-- Auto-update updated_at on every write
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

-- Seed the singleton row (no-op if it already exists)
INSERT INTO bot_state (id, symbol, is_active, entry_price, side, qty, stop_loss, config)
VALUES (1, NULL, FALSE, NULL, NULL, NULL, NULL, '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- Verify: SELECT * FROM bot_state;
