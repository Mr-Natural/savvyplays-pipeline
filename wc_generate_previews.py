"""
wc_generate_previews.py
Generate match preview content for wc_matches and write it to wc_match_previews.

For each eligible match (both teams assigned, status 'scheduled') it pulls both
teams' stored data, key players and predictions, fetches current betting odds,
then asks Claude (with web search) for a structured preview matching the
wc_match_previews schema. All prose passes swap_dashes() + the content linter
before upsert. The match status is bumped to 'preview_published'.

Flags:
    python wc_generate_previews.py --match 1          # one match by number
    python wc_generate_previews.py --matchday 1       # all of matchday 1 (1-24)
    python wc_generate_previews.py --stage "Round of 32"
    python wc_generate_previews.py --matchday 1 --dry-run   # print, write nothing

Odds: uses The Odds API if ODDS_API_KEY is set (the sport key is auto-detected);
otherwise the model sources odds via web search. Env (see .env.example):
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ANTHROPIC_API_KEY, [ODDS_API_KEY].
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from wc_lib import get_anthropic, get_supabase, lint_record
from wc_populate import ask_json, clean_strings  # ask_json applies CONTENT_RULES as system

MD_RANGES = {1: (1, 24), 2: (25, 48), 3: (49, 72)}

# Only these keys are written to wc_match_previews (drop anything the model invents).
PREVIEW_COLUMNS = (
    "headline", "subheadline", "match_overview", "team_a_analysis",
    "team_b_analysis", "key_battle", "tactical_angle", "betting_preview",
    "scoreline_prediction", "verdict",
)

ODDS_BASE = "https://api.the-odds-api.com/v4"


# ── extra content rules layered on top of CONTENT_RULES ─────────────────────

MATCH_RULES = """\
You are writing a single-match preview for the 2026 FIFA World Cup. In addition
to every standing content rule above, follow these:

- Be honest about form limitations. National teams arrive on two or three warm-up
  friendlies and a qualifying campaign that ended months ago. Do not pretend two
  friendlies are form. "The warm-up results tell us little beyond squad selection"
  beats inventing statistical confidence.
- Lean on the qualifying campaign and each team's tactical identity and system,
  not recent scorelines. How did they play across qualifying? What shape?
- The key battle must be specific and tactical, a real positional matchup with a
  reason, not "star A versus star B".
- Betting angles must be genuinely analytical: reference the odds structure, where
  value sits and why. Never just "we like Team X".
- The headline must earn a click from social. No "Team A vs Team B Preview".
- Reference venue and conditions where they matter (Mexico City altitude, grass vs
  turf, host-nation crowd, travel between host cities).
- Acknowledge the group context and what is at stake. A dead rubber reads
  differently to a must-win opener.
- Keep scoreline predictions realistic. 1-0 and 2-1 are far more common than 4-2
  in World Cup group stages. Do not chase drama.
"""


# ── odds (best effort) ──────────────────────────────────────────────────────

def _detect_sport_key(api_key: str) -> str | None:
    import requests
    try:
        r = requests.get(f"{ODDS_BASE}/sports", params={"apiKey": api_key}, timeout=20)
        r.raise_for_status()
        for s in r.json():
            key = s.get("key", "")
            if "world_cup" in key and s.get("group", "").lower().startswith("soccer"):
                return key
    except Exception as e:  # noqa: BLE001
        print(f"    odds: sport-key detection failed ({e})")
    return None


def fetch_odds(team_a: str, team_b: str) -> dict | None:
    """Return {match_odds, over_under, source} from The Odds API, or None."""
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return None
    import requests

    sport = os.environ.get("ODDS_SPORT") or _detect_sport_key(api_key)
    if not sport:
        return None
    try:
        r = requests.get(
            f"{ODDS_BASE}/sports/{sport}/odds",
            params={"apiKey": api_key, "regions": "au,us,uk",
                    "markets": "h2h,totals", "oddsFormat": "decimal"},
            timeout=20,
        )
        r.raise_for_status()
        events = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"    odds: fetch failed ({e})")
        return None

    a, b = team_a.lower(), team_b.lower()
    for ev in events:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        names = f"{home} {away}".lower()
        if not (a.split()[-1] in names and b.split()[-1] in names):
            continue
        bk = (ev.get("bookmakers") or [None])[0]
        if not bk:
            continue
        out: dict = {"source": bk.get("title", "The Odds API")}
        for mk in bk.get("markets", []):
            if mk["key"] == "h2h":
                prices = {o["name"]: o["price"] for o in mk["outcomes"]}
                out["match_odds"] = {
                    "team_a": str(prices.get(home if home.lower().startswith(a.split()[0]) else away, "")),
                    "draw": str(prices.get("Draw", "")),
                    "team_b": str(prices.get(away if home.lower().startswith(a.split()[0]) else home, "")),
                }
            elif mk["key"] == "totals":
                pts = mk["outcomes"][0].get("point")
                over = next((o["price"] for o in mk["outcomes"] if o["name"] == "Over"), None)
                under = next((o["price"] for o in mk["outcomes"] if o["name"] == "Under"), None)
                out["over_under"] = {"line": str(pts), "over": str(over), "under": str(under)}
        return out
    return None


# ── context + prompt ────────────────────────────────────────────────────────

def fetch_team_context(sb, team_id: str) -> dict:
    row = (
        sb.table("wc_teams")
        .select("*, players:wc_players(*), prediction:wc_predictions(*)")
        .eq("id", team_id)
        .single()
        .execute()
        .data
    )
    pred = row.get("prediction")
    if isinstance(pred, list):
        pred = pred[0] if pred else None
    players = sorted(row.get("players") or [], key=lambda p: p.get("sort_order", 0))
    return {**row, "players": players, "prediction": pred}


def _team_block(label: str, t: dict) -> str:
    players = "\n".join(
        f"    - {p['name']} ({p.get('position', '?')}, {p.get('club', '?')}): "
        f"{p.get('caps', '?')} caps, {p.get('goals', '?')} goals"
        + (" [STAR]" if p.get("is_star_player") else "")
        + (" [watch]" if p.get("is_player_to_watch") else "")
        for p in t.get("players", [])
    )
    pred = t.get("prediction") or {}
    form = json.dumps(t.get("recent_form", []), ensure_ascii=False)
    warm = json.dumps(t.get("warmup_matches", []), ensure_ascii=False)
    return f"""\
{label}: {t['name']} (FIFA #{t.get('fifa_ranking', '?')}, {t.get('confederation', '?')})
  Manager: {t.get('manager', '?')}
  Qualifying: {t.get('qualifying_path', '?')}
  Overview: {t.get('overview', '')}
  Strengths: {t.get('strengths', '')}
  Weaknesses: {t.get('weaknesses', '')}
  Recent form: {form}
  Warm-ups: {warm}
  Predicted: {pred.get('predicted_exit_round', '?')} (group pos {pred.get('predicted_group_pos', '?')})
  Key players:
{players}"""


def preview_prompt(match: dict, ta: dict, tb: dict, odds: dict | None) -> str:
    kickoff = match.get("kickoff_utc") or "TBC"
    context = (
        f"Group {match.get('group_letter')} matchday "
        f"{1 if match['match_number'] <= 24 else 2 if match['match_number'] <= 48 else 3}"
        if match["stage"] == "Group Stage"
        else match["stage"]
    )
    odds_note = (
        f"Current odds from {odds.get('source')}: {json.dumps({k: v for k, v in odds.items() if k != 'source'})}. "
        "Use these in betting_preview and set betting_preview.source accordingly."
        if odds
        else "No odds feed was available. Use web search to find current match odds "
        "(match result, over/under 2.5, both teams to score) from a major bookmaker "
        "and set betting_preview.source to that bookmaker."
    )
    return f"""\
{MATCH_RULES}

Write the preview for this 2026 FIFA World Cup fixture.

MATCH: {ta['name']} vs {tb['name']}
Stage: {context}
Venue: {match.get('venue', '?')}, {match.get('city', '?')}
Kickoff (UTC): {kickoff}

{_team_block("TEAM A", ta)}

{_team_block("TEAM B", tb)}

{odds_note}

Use web search to confirm any late team news, injuries or suspensions, and the
current odds. Then return ONE JSON object only (no markdown fences, no commentary)
with EXACTLY this shape:

{{
  "headline": "<compelling, clickable, not 'A vs B Preview'>",
  "subheadline": "<one secondary line>",
  "match_overview": "<300-500 words: storyline, tactical matchup, what's at stake>",
  "team_a_analysis": "<150-250 words on {ta['name']}'s approach, form and key considerations>",
  "team_b_analysis": "<150-250 words on {tb['name']}'s approach, form and key considerations>",
  "key_battle": {{
    "player_a": {{"name": "<{ta['name']} player>", "position": "<GK|DEF|MID|FWD>", "club": "<club>"}},
    "player_b": {{"name": "<{tb['name']} player>", "position": "<GK|DEF|MID|FWD>", "club": "<club>"}},
    "description": "<specific, tactical reason this matchup decides the game>"
  }},
  "tactical_angle": "<100-150 words: formations, pressing triggers, set-piece threats>",
  "betting_preview": {{
    "match_odds": {{"team_a": "<decimal>", "draw": "<decimal>", "team_b": "<decimal>"}},
    "over_under": {{"line": "2.5", "over": "<decimal>", "under": "<decimal>"}},
    "btts": {{"yes": "<decimal>", "no": "<decimal>"}},
    "savvyplays_pick": "<the value play, e.g. '{ta['name']} +0.5' or 'Under 2.5'>",
    "pick_rationale": "<~100 words on why this is the value>",
    "confidence": "<Low|Medium|High>",
    "source": "<bookmaker / odds source>"
  }},
  "scoreline_prediction": "<realistic, e.g. 1-0 or 2-1>",
  "verdict": "<2-3 sentence summary verdict>"
}}

team_a is {ta['name']}, team_b is {tb['name']}; keep that order in match_odds and
key_battle. Follow every content rule (no em dashes, no banned words, take a stance)."""


# ── slug + write ────────────────────────────────────────────────────────────

def build_slug(match: dict, slug_a: str, slug_b: str) -> str:
    if match["stage"] == "Group Stage":
        md = 1 if match["match_number"] <= 24 else 2 if match["match_number"] <= 48 else 3
        ctx = f"group-{(match.get('group_letter') or '').lower()}-matchday-{md}"
    else:
        ctx = f"{match['stage'].lower().replace(' ', '-')}-match-{match['match_number']}"
    return f"{slug_a}-vs-{slug_b}-{ctx}"


def lint_preview(label: str, data: dict) -> list[str]:
    issues = lint_record(label, {
        "headline": data.get("headline"),
        "subheadline": data.get("subheadline"),
        "match_overview": data.get("match_overview"),
        "team_a_analysis": data.get("team_a_analysis"),
        "team_b_analysis": data.get("team_b_analysis"),
        "tactical_angle": data.get("tactical_angle"),
        "verdict": data.get("verdict"),
    })
    kb = data.get("key_battle") or {}
    issues += lint_record(f"{label}/key_battle", {"description": kb.get("description")})
    bp = data.get("betting_preview") or {}
    issues += lint_record(f"{label}/betting", {"rationale": bp.get("pick_rationale")})
    return issues


def upsert_preview(sb, match: dict, slug: str, data: dict) -> None:
    row = {k: data[k] for k in PREVIEW_COLUMNS if k in data}
    row["match_id"] = match["id"]
    row["slug"] = slug
    row["published_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("wc_match_previews").upsert(row, on_conflict="match_id").execute()
    sb.table("wc_matches").update({"status": "preview_published"}).eq("id", match["id"]).execute()


# ── runner ──────────────────────────────────────────────────────────────────

def select_matches(sb, args) -> list[dict]:
    q = sb.table("wc_matches").select("*").eq("status", "scheduled") \
        .not_.is_("team_a_id", "null").not_.is_("team_b_id", "null")
    if args.match:
        q = q.eq("match_number", args.match)
    elif args.matchday:
        lo, hi = MD_RANGES[args.matchday]
        q = q.gte("match_number", lo).lte("match_number", hi)
    elif args.stage:
        q = q.eq("stage", args.stage)
    return q.order("match_number").execute().data


def run_match(sb, client, match: dict, dry_run: bool) -> bool:
    ta = fetch_team_context(sb, match["team_a_id"])
    tb = fetch_team_context(sb, match["team_b_id"])
    slug = build_slug(match, ta["slug"], tb["slug"])
    print(f"  - Match {match['match_number']}: {ta['name']} vs {tb['name']}  [{slug}]", flush=True)

    odds = fetch_odds(ta["name"], tb["name"])
    try:
        data = ask_json(client, preview_prompt(match, ta, tb, odds), max_tokens=8000)
    except Exception as e:  # noqa: BLE001
        print(f"      FAIL: {e}")
        return False

    data = clean_strings(data)  # swap em dashes before write
    for issue in lint_preview(f"M{match['match_number']}", data):
        print(f"      lint: {issue}")

    if dry_run:
        def block(label: str, text) -> None:
            print(f"\n  === {label} ===\n{text}")
        block("HEADLINE", data.get("headline"))
        block("SUBHEADLINE", data.get("subheadline"))
        block("MATCH OVERVIEW", data.get("match_overview"))
        block(f"{ta['name'].upper()} ANALYSIS", data.get("team_a_analysis"))
        block(f"{tb['name'].upper()} ANALYSIS", data.get("team_b_analysis"))
        block("KEY BATTLE", json.dumps(data.get("key_battle"), ensure_ascii=False, indent=2))
        block("TACTICAL ANGLE", data.get("tactical_angle"))
        block("BETTING PREVIEW", json.dumps(data.get("betting_preview"), ensure_ascii=False, indent=2))
        block("SCORELINE", data.get("scoreline_prediction"))
        block("VERDICT", data.get("verdict"))
        return True

    upsert_preview(sb, match, slug, data)
    print("      OK (published)")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--match", type=int, help="single FIFA match number")
    g.add_argument("--matchday", type=int, choices=[1, 2, 3], help="group-stage matchday")
    g.add_argument("--stage", help='knockout stage, e.g. "Round of 32"')
    ap.add_argument("--dry-run", action="store_true", help="print output, write nothing")
    args = ap.parse_args()

    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sb = get_supabase()
    client = get_anthropic()

    matches = select_matches(sb, args)
    if not matches:
        print("No eligible matches (need both teams assigned and status 'scheduled').")
        return

    print(f"Generating previews for {len(matches)} match(es)"
          f"{' [dry-run]' if args.dry_run else ''}\n")
    ok = sum(run_match(sb, client, m, args.dry_run) for m in matches)
    print(f"\nDone. success={ok} fail={len(matches) - ok}")


if __name__ == "__main__":
    main()
