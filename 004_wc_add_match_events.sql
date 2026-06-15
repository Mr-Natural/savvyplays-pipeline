-- 004_wc_add_match_events.sql
-- Post-match factual data for World Cup 2026 fixtures, populated by
-- wc_post_match_update.py. Idempotent: safe to run more than once.
--
-- Depends on 002_wc_create_match_tables.sql (public.wc_matches).
--
-- match_events  : ordered list of verified in-match events. Each element:
--   { "minute": "23", "event_type": "goal", "team": "a" | "b",
--     "player": "Irankunda", "description": null }
--   event_type ∈ goal | own_goal | penalty | penalty_missed |
--                yellow_card | red_card | substitution
--   "team" is "a" / "b" mapping to wc_matches.team_a_id / team_b_id.
-- match_summary : 2-3 sentence factual recap (no embellishment).

alter table public.wc_matches
  add column if not exists match_events jsonb;

alter table public.wc_matches
  add column if not exists match_summary text;

-- New columns inherit wc_matches' existing RLS (public read, service write) and
-- the table-level GRANTs from 002, so PostgREST exposes them with no extra grant.
