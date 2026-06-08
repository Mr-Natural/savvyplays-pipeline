-- 001_wc_create_tables.sql
-- FIFA World Cup 2026 hub tables for SavvyPlays.
-- Idempotent: safe to run more than once.
-- RLS: public read, service_role write.

-- ── helper: updated_at trigger ──────────────────────────────────────────
create or replace function public.wc_set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

-- ── wc_teams ────────────────────────────────────────────────────────────
create table if not exists public.wc_teams (
  id              uuid primary key default gen_random_uuid(),
  name            text not null,
  slug            text not null unique,
  group_letter    char(1) not null check (group_letter between 'A' and 'L'),
  fifa_ranking    int,
  confederation   text check (confederation in
                    ('UEFA','CONMEBOL','AFC','CAF','CONCACAF','OFC')),
  flag_emoji      text,
  nickname        text,
  manager         text,
  best_wc_finish  text,
  wc_appearances  int,
  qualifying_path text,
  recent_form     jsonb not null default '[]'::jsonb,
  warmup_matches  jsonb not null default '[]'::jsonb,
  overview        text,
  strengths       text,
  weaknesses      text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists idx_wc_teams_group on public.wc_teams (group_letter);
create index if not exists idx_wc_teams_slug  on public.wc_teams (slug);

drop trigger if exists trg_wc_teams_updated_at on public.wc_teams;
create trigger trg_wc_teams_updated_at
  before update on public.wc_teams
  for each row execute function public.wc_set_updated_at();

-- ── wc_players ──────────────────────────────────────────────────────────
create table if not exists public.wc_players (
  id                 uuid primary key default gen_random_uuid(),
  team_id            uuid not null references public.wc_teams(id) on delete cascade,
  name               text not null,
  position           text check (position in ('GK','DEF','MID','FWD')),
  club               text,
  age                int,
  caps               int,
  goals              int,
  description        text,
  is_star_player     boolean not null default false,
  is_player_to_watch boolean not null default false,
  sort_order         int not null default 0
);

create index if not exists idx_wc_players_team on public.wc_players (team_id);
-- one canonical (team, name) row so the populate script can upsert cleanly
create unique index if not exists uq_wc_players_team_name
  on public.wc_players (team_id, name);

-- ── wc_predictions ──────────────────────────────────────────────────────
create table if not exists public.wc_predictions (
  id                     uuid primary key default gen_random_uuid(),
  team_id                uuid not null unique
                           references public.wc_teams(id) on delete cascade,
  predicted_group_pos    int check (predicted_group_pos between 1 and 4),
  predicted_exit_round   text check (predicted_exit_round in
                           ('Group Stage','Round of 32','Round of 16',
                            'Quarter-finals','Semi-finals','Runner-up','Winners')),
  top_scorer_name        text,
  top_scorer_goals       numeric,
  group_winner_odds      text,
  tournament_winner_odds text,
  dark_horse_rating      int check (dark_horse_rating between 1 and 5),
  prediction_rationale   text
);

create index if not exists idx_wc_predictions_team on public.wc_predictions (team_id);

-- ── wc_groups ───────────────────────────────────────────────────────────
create table if not exists public.wc_groups (
  id                      uuid primary key default gen_random_uuid(),
  letter                  char(1) not null unique check (letter between 'A' and 'L'),
  name                    text,
  venue_cities            text[] not null default '{}',
  overview                text,
  predicted_qualification text
);

-- ── RLS ─────────────────────────────────────────────────────────────────
alter table public.wc_teams       enable row level security;
alter table public.wc_players     enable row level security;
alter table public.wc_predictions enable row level security;
alter table public.wc_groups      enable row level security;

do $$
declare
  t text;
begin
  foreach t in array array['wc_teams','wc_players','wc_predictions','wc_groups']
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
