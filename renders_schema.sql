-- Not Hollywood: user library storage
-- Run once in the Supabase SQL Editor (dashboard → SQL editor → New query).
-- Idempotent: safe to re-run.

create table if not exists public.renders (
  id text primary key,
  user_id uuid not null,
  prompt text not null,
  title text,
  slug text,
  storage_path text not null,
  thumb_path text,
  duration integer not null default 0,
  resolution text,
  scene_count integer not null default 1,
  scenes jsonb,
  franchise_ref_url text,
  created_at timestamptz not null default now(),
  bytes bigint
);

create index if not exists renders_user_created_idx
  on public.renders (user_id, created_at desc);

-- Row-level security: users see only their own rows. Writes happen via
-- the service_role key from the FastAPI worker, which bypasses RLS.
alter table public.renders enable row level security;

drop policy if exists renders_owner_select on public.renders;
create policy renders_owner_select on public.renders
  for select using (auth.uid() = user_id);

drop policy if exists renders_owner_delete on public.renders;
create policy renders_owner_delete on public.renders
  for delete using (auth.uid() = user_id);
