-- 002_wc_create_match_tables.sql
-- FIFA World Cup 2026 match fixtures + match previews for SavvyPlays.
-- Idempotent: safe to run more than once.
-- RLS: public read, service_role write. Explicit GRANTs for PostgREST exposure.
--
-- Depends on 001_wc_create_tables.sql (wc_teams, public.wc_set_updated_at()).

-- ── helper: updated_at trigger (re-declared so this file is self-sufficient) ─
create or replace function public.wc_set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

-- ── wc_matches ──────────────────────────────────────────────────────────────
-- One row per fixture (group stage 1-72, knockouts 73-104). team_*_id are null
-- for knockout placeholders whose teams are not yet known; kickoff/venue/city
-- are nullable for the same reason (filled via --knockout-update later).
create table if not exists public.wc_matches (
  id                 uuid primary key default gen_random_uuid(),
  match_number       int not null unique check (match_number between 1 and 104),
  stage              text not null check (stage in
                       ('Group Stage','Round of 32','Round of 16','Quarter-finals',
                        'Semi-finals','Third-place','Final')),
  group_letter       char(1) check (group_letter between 'A' and 'L'),
  team_a_id          uuid references public.wc_teams(id) on delete set null,
  team_b_id          uuid references public.wc_teams(id) on delete set null,
  team_a_placeholder text,
  team_b_placeholder text,
  kickoff_utc        timestamptz,
  venue              text,
  city               text,
  status             text not null default 'scheduled' check (status in
                       ('scheduled','preview_published','live','completed')),
  score_a            int,
  score_b            int,
  result             text check (result in
                       ('team_a','team_b','draw','team_a_pens','team_b_pens')),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists idx_wc_matches_stage    on public.wc_matches (stage);
create index if not exists idx_wc_matches_group    on public.wc_matches (group_letter);
create index if not exists idx_wc_matches_kickoff  on public.wc_matches (kickoff_utc);
create index if not exists idx_wc_matches_status   on public.wc_matches (status);
create index if not exists idx_wc_matches_team_a   on public.wc_matches (team_a_id);
create index if not exists idx_wc_matches_team_b   on public.wc_matches (team_b_id);

drop trigger if exists trg_wc_matches_updated_at on public.wc_matches;
create trigger trg_wc_matches_updated_at
  before update on public.wc_matches
  for each row execute function public.wc_set_updated_at();

-- ── wc_match_previews ───────────────────────────────────────────────────────
-- One published preview per match.
create table if not exists public.wc_match_previews (
  id                   uuid primary key default gen_random_uuid(),
  match_id             uuid not null unique
                         references public.wc_matches(id) on delete cascade,
  slug                 text not null unique,
  headline             text not null,
  subheadline          text,
  match_overview       text,
  team_a_analysis      text,
  team_b_analysis      text,
  key_battle           jsonb not null default '{}'::jsonb,
  tactical_angle       text,
  betting_preview      jsonb not null default '{}'::jsonb,
  scoreline_prediction text,
  verdict              text,
  published_at         timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

create index if not exists idx_wc_previews_match on public.wc_match_previews (match_id);
create index if not exists idx_wc_previews_slug  on public.wc_match_previews (slug);

drop trigger if exists trg_wc_previews_updated_at on public.wc_match_previews;
create trigger trg_wc_previews_updated_at
  before update on public.wc_match_previews
  for each row execute function public.wc_set_updated_at();

-- ── RLS: public read, service_role write ────────────────────────────────────
alter table public.wc_matches        enable row level security;
alter table public.wc_match_previews enable row level security;

do $$
declare
  t text;
begin
  foreach t in array array['wc_matches','wc_match_previews']
  loop
    if not exists (
      select 1 from pg_policies where tablename = t and policyname = 'Public read access'
    ) then
      execute format(
        'create policy "Public read access" on public.%I for select using (true)', t);
    end if;

    if not exists (
      select 1 from pg_policies where tablename = t and policyname = 'Service write access'
    ) then
      execute format(
        'create policy "Service write access" on public.%I for all using (auth.role() = ''service_role'')', t);
    end if;
  end loop;
end $$;

-- ── GRANTs: expose to PostgREST (anon/authenticated read, service_role write) ─
grant usage on schema public to anon, authenticated, service_role;
grant select on public.wc_matches        to anon, authenticated;
grant select on public.wc_match_previews  to anon, authenticated;
grant all    on public.wc_matches         to service_role;
grant all    on public.wc_match_previews  to service_role;
