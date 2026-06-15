#!/usr/bin/env python3
"""
SavvyPlays — World Cup Post-Match Update
========================================

For each COMPLETED 2026 FIFA World Cup match, web-searches for the actual
in-match events (goalscorers + minutes, cards, substitutions) and a short
factual recap, then stores them on `wc_matches`:

    match_events  jsonb  -- [{minute, event_type, team, player, description}]
    match_summary text   -- 2-3 sentence factual summary

This runs AFTER wc_update_results.py (which writes the verified final score).
The known score is passed to the model as ground truth, and the goal events are
reconciled against it — so the events stay consistent with what actually
happened rather than what the model "remembers".

Facts only. Events are deliberately terse — "Irankunda 23'", a player name plus a
minute plus an event type. No prose about HOW a goal was scored: we did not watch
every match, and the more descriptive the text, the more room for error.

Usage
-----
    # Fill in events for every completed match still missing them (cron default)
    python wc_post_match_update.py

    # A single match by FIFA match number
    python wc_post_match_update.py --match 8

    # Every completed match kicking off on a date (UTC)
    python wc_post_match_update.py --date 2026-06-13

    # Re-run matches that already have events
    python wc_post_match_update.py --match 8 --force

    # Show what would be written, write nothing
    python wc_post_match_update.py --date 2026-06-13 --dry-run

Environment
-----------
Reads ANTHROPIC_API_KEY, SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the
World_Cup pipeline .env (via wc_lib). TLS interception is handled by wc_lib's
truststore injection, same as the rest of the pipeline.

Cron
----
Designed to run unattended alongside the results/odds capture. Recommended order
each cycle:  wc_update_results.py  →  wc_post_match_update.py  →  wc_capture_odds.py
By default it only touches completed matches that have no events yet, so repeated
runs are cheap and idempotent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from wc_lib import get_anthropic, get_supabase

# Most capable model — chosen for factual accuracy on extraction. The rest of the
# pipeline standardises on claude-sonnet-4-6 (wc_lib.MODEL); switch this to match
# if you'd rather trade a little accuracy for cost/consistency.
MODEL = "claude-opus-4-8"

# Same server-side web-search tool the preview generator uses.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 6}

ALLOWED_EVENTS = {
    "goal", "own_goal", "penalty", "penalty_missed",
    "yellow_card", "red_card", "substitution",
}
GOAL_EVENTS = {"goal", "penalty"}  # count toward the scoring team's tally

SYSTEM = (
    "You are a meticulous football data extractor. You report only verified, "
    "objective match facts: who, what, and what minute. You never describe how a "
    "goal was scored or editorialise. If you cannot verify an event from your "
    "search results, you omit it. Under-reporting is always better than guessing. "
    "Use official FIFA player-name spellings with correct diacritics."
)


# ── model output parsing ─────────────────────────────────────────────────────

def _text_of(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def _extract_json(raw: str) -> dict:
    """Pull the first balanced JSON object out of model text."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    if start == -1:
        raise ValueError("no JSON object in model output")
    depth = in_str = esc = 0
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = 0
            elif c == "\\":
                esc = 1
            elif c == '"':
                in_str = 0
        elif c == '"':
            in_str = 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start : i + 1])
    raise ValueError("unbalanced JSON in model output")


def _minute_key(minute: str) -> float:
    """Sort key for a minute string like '45', '45+2', '90+3'."""
    m = re.match(r"\s*(\d+)(?:\s*\+\s*(\d+))?", str(minute))
    if not m:
        return 999.0
    base = int(m.group(1))
    extra = int(m.group(2)) if m.group(2) else 0
    return base + extra / 100.0


# ── prompt ────────────────────────────────────────────────────────────────────

def build_prompt(m: dict, name_a: str, name_b: str) -> str:
    date = ""
    if m.get("kickoff_utc"):
        try:
            date = datetime.fromisoformat(
                m["kickoff_utc"].replace("Z", "+00:00")
            ).strftime("%d %B %Y")
        except ValueError:
            date = ""
    where = ", ".join(x for x in (m.get("venue"), m.get("city")) if x)
    sa, sb = m["score_a"], m["score_b"]

    return f"""\
Find the verified match report for this 2026 FIFA World Cup group-stage fixture and \
extract its key events.

Match: {name_a} vs {name_b}{f' on {date}' if date else ''}{f' ({where})' if where else ''}
Confirmed final score (ground truth): {name_a} {sa} - {sb} {name_b}

Use web search to find the official match report or a reputable report (FIFA, ESPN, \
BBC, Guardian, etc.). Then return ONE JSON object only, no markdown fences, no \
commentary, EXACTLY this shape:

{{
  "match_summary": "<2 to 3 plain factual sentences: who won, the score, and the \
decisive moments. No adjectives about quality of play.>",
  "events": [
    {{"minute": "<shirt-clock minute as a string, e.g. \\"23\\" or \\"90+2\\">",
     "event_type": "<goal|own_goal|penalty|penalty_missed|yellow_card|red_card|substitution>",
     "team": "<\\"a\\" for {name_a} or \\"b\\" for {name_b}>",
     "player": "<player full name or surname, correct diacritics>",
     "description": "<null in almost all cases; only an objective qualifier such as \
the player replaced on a substitution. NEVER describe how a goal was scored.>"}}
  ]
}}

Strict rules:
- The goal events (goal + penalty for each side, plus own_goals credited to the \
opposing side) MUST reconcile with the final score above. If your sources disagree \
with the score, trust the score and only include goals you can attribute confidently.
- Include yellow cards, red cards and substitutions only where clearly reported.
- Order events by minute. Use "a"/"b" exactly for the team field.
- Do not invent minutes or scorers. Omit any event you cannot verify."""


# ── extraction ─────────────────────────────────────────────────────────────────

def fetch_events(client, m: dict, name_a: str, name_b: str, retries: int = 2) -> dict:
    """Call Claude with web search; return {match_summary, events} validated."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                tools=[WEB_SEARCH_TOOL],
                system=SYSTEM,
                messages=[{"role": "user", "content": build_prompt(m, name_a, name_b)}],
            )
            data = _extract_json(_text_of(msg))
            return _validate(data)
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"      ! attempt {attempt + 1} failed ({e}); retrying in {wait}s",
                  flush=True)
            time.sleep(wait)
    raise RuntimeError(f"giving up after {retries + 1} attempts: {last_err}")


def _validate(data: dict) -> dict:
    """Keep only well-formed events; drop anything malformed rather than guess."""
    summary = (data.get("match_summary") or "").strip()
    clean: list[dict] = []
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue
        et = str(ev.get("event_type", "")).strip().lower()
        team = str(ev.get("team", "")).strip().lower()
        player = str(ev.get("player", "")).strip()
        minute = str(ev.get("minute", "")).strip().rstrip("'")
        if et not in ALLOWED_EVENTS or team not in {"a", "b"} or not player or not minute:
            continue
        desc = ev.get("description")
        desc = desc.strip() if isinstance(desc, str) and desc.strip() else None
        clean.append({
            "minute": minute,
            "event_type": et,
            "team": team,
            "player": player,
            "description": desc,
        })
    clean.sort(key=lambda e: _minute_key(e["minute"]))
    return {"match_summary": summary, "events": clean}


def reconcile_goals(events: list[dict], score_a: int, score_b: int) -> str | None:
    """Soft check: do goal events match the known score? Returns a warning or None."""
    a = sum(1 for e in events if e["event_type"] in GOAL_EVENTS and e["team"] == "a")
    b = sum(1 for e in events if e["event_type"] in GOAL_EVENTS and e["team"] == "b")
    a += sum(1 for e in events if e["event_type"] == "own_goal" and e["team"] == "b")
    b += sum(1 for e in events if e["event_type"] == "own_goal" and e["team"] == "a")
    if a != score_a or b != score_b:
        return f"goal events {a}-{b} != final score {score_a}-{score_b}"
    return None


# ── data access ──────────────────────────────────────────────────────────────

SELECT = (
    "id,match_number,kickoff_utc,venue,city,score_a,score_b,status,match_events,"
    "team_a:wc_teams!team_a_id(name),"
    "team_b:wc_teams!team_b_id(name)"
)


def select_matches(sb, args) -> list[dict]:
    q = (
        sb.table("wc_matches").select(SELECT)
        .eq("status", "completed")
        .not_.is_("score_a", "null")
        .not_.is_("score_b", "null")
    )
    if args.match is not None:
        q = q.eq("match_number", args.match)
    elif args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        q = (q.gte("kickoff_utc", day.isoformat())
              .lt("kickoff_utc", (day + timedelta(days=1)).isoformat()))
    return q.order("match_number").execute().data


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Populate post-match events on wc_matches.")
    p.add_argument("--date", help="Match date to process, YYYY-MM-DD (kickoff, UTC).")
    p.add_argument("--match", type=int, help="Only this FIFA match number.")
    p.add_argument("--dry-run", action="store_true", help="Show output, write nothing.")
    p.add_argument("--force", action="store_true",
                   help="Re-process matches that already have events.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sb = get_supabase()
    client = get_anthropic()

    try:
        matches = select_matches(sb, args)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "match_events" in msg or "column" in msg.lower() or "schema cache" in msg.lower():
            sys.exit(
                "Could not read match_events — apply the migration first:\n"
                "  psql ... -f 004_wc_add_match_events.sql\n"
                f"(underlying error: {msg})"
            )
        raise

    # Skip matches that already have events, unless --force or an explicit
    # single --match was requested (both are deliberate re-processing).
    if not args.force and args.match is None:
        matches = [m for m in matches if not m.get("match_events")]

    if not matches:
        print("No completed matches need post-match events.")
        return 0

    print(f"Processing {len(matches)} completed match(es)"
          f"{' [dry-run]' if args.dry_run else ''}\n", flush=True)

    done = failed = 0
    for m in matches:
        name_a = (m.get("team_a") or {}).get("name") or "Team A"
        name_b = (m.get("team_b") or {}).get("name") or "Team B"
        sa, sb_ = m["score_a"], m["score_b"]
        print(f"  - #{m['match_number']:>3}  {name_a} {sa}-{sb_} {name_b}", flush=True)

        try:
            data = fetch_events(client, m, name_a, name_b)
        except Exception as e:  # noqa: BLE001
            print(f"      FAIL: {e}", flush=True)
            failed += 1
            continue

        warn = reconcile_goals(data["events"], sa, sb_)
        if warn:
            print(f"      lint: {warn}", flush=True)

        if args.dry_run:
            print(f"      summary: {data['match_summary']}", flush=True)
            for e in data["events"]:
                who = name_a if e["team"] == "a" else name_b
                tag = f" ({e['description']})" if e["description"] else ""
                print(f"        {e['minute']:>4}'  {e['event_type']:<14} "
                      f"{e['player']} [{who}]{tag}", flush=True)
            done += 1
            continue

        try:
            sb.table("wc_matches").update({
                "match_events": data["events"],
                "match_summary": data["match_summary"],
            }).eq("id", m["id"]).execute()
        except Exception as e:  # noqa: BLE001
            print(f"      WRITE FAIL: {e}", flush=True)
            failed += 1
            continue
        print(f"      OK  ({len(data['events'])} events)", flush=True)
        done += 1

    print(f"\nDone. {'previewed' if args.dry_run else 'written'}={done} fail={failed}")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    raise SystemExit(main())
