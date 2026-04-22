-- ─────────────────────────────────────────────────────────────────────────────
-- MEXC Market Maker Bot — Supabase schema
--
-- Run this once in the Supabase SQL Editor (Dashboard → SQL Editor → New query).
-- The bot uses a single-row table (id = 1) to store all mutable state.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Create the table
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.bot_state (
    id          integer      primary key default 1,
    symbol      text,
    is_active   boolean      not null default false,
    entry_price float8,
    side        text         check (side in ('buy', 'sell')),
    qty         float8,
    stop_loss   float8,
    config      jsonb        not null default '{}'::jsonb,

    -- Audit columns (optional but useful for debugging)
    updated_at  timestamptz  not null default now()
);

-- Enforce the singleton: only one row with id = 1 is ever allowed.
-- The check constraint is belt-and-suspenders alongside the PK.
alter table public.bot_state
    add constraint bot_state_singleton check (id = 1);

-- Auto-update updated_at on every write
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_bot_state_updated_at on public.bot_state;
create trigger trg_bot_state_updated_at
    before update on public.bot_state
    for each row execute function public.set_updated_at();


-- 2. Seed the singleton row
-- ─────────────────────────────────────────────────────────────────────────────
-- Insert only if the row doesn't exist yet (idempotent).
insert into public.bot_state (id, symbol, is_active, entry_price, side, qty, stop_loss, config)
values (1, null, false, null, null, null, null, '{}'::jsonb)
on conflict (id) do nothing;


-- 3. Row-Level Security
-- ─────────────────────────────────────────────────────────────────────────────
-- Enable RLS so the anon key cannot read/write this table.
-- The bot uses the service-role key (SUPABASE_KEY), which bypasses RLS.
alter table public.bot_state enable row level security;

-- Deny all access to the anon role (default Supabase public role).
-- The service-role key bypasses RLS entirely, so no policy is needed for it.
-- If you want to inspect the table from the Supabase dashboard, use the
-- service-role key or temporarily disable RLS.
revoke all on public.bot_state from anon, authenticated;


-- 4. Verify
-- ─────────────────────────────────────────────────────────────────────────────
-- Run this to confirm the row exists after applying the schema:
--   select * from public.bot_state;
