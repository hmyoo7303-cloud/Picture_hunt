-- Run once in Supabase: Dashboard > SQL Editor > New query > paste > Run

-- 1) One row per game room. The app writes the whole room as JSON.
create table if not exists public.bingo_rooms (
  code       text primary key,
  state      jsonb not null,
  updated_at timestamptz not null default now()
);

create or replace function public.bingo_touch() returns trigger
language plpgsql as $$ begin new.updated_at = now(); return new; end $$;

drop trigger if exists bingo_touch on public.bingo_rooms;
create trigger bingo_touch before insert or update on public.bingo_rooms
  for each row execute function public.bingo_touch();

-- Lock the table: only the server (service role key) can read or write it.
alter table public.bingo_rooms enable row level security;

-- 2) Public bucket for location pictures and selfies (read-only for the public,
--    uploads go through the server with the service role key). 10 MB per file max.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('bingo', 'bingo', true, 10485760, array['image/jpeg'])
on conflict (id) do nothing;
