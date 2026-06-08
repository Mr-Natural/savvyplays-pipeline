"""
wc_populate.py
Research and populate World Cup 2026 team data into Supabase using the Claude
API with server-side web search.

Per team it generates: the wc_teams row, 3-5 wc_players rows, and the
wc_predictions row. Per group it generates the wc_groups row. Teams are
processed in batches by group with a delay between batches to respect rate
limits.

Usage:
    python wc_populate.py                 # all 12 groups
    python wc_populate.py --group D       # single group (testing)
    python wc_populate.py --team new-zealand  # single team
    python wc_populate.py --players-only  # only regenerate players (team must exist)
    python wc_populate.py --warmups-only  # only re-scrape warmup_matches
    python wc_populate.py --no-groups     # skip the group-overview pass

Env (see .env.example): SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import json
import re
import time

from wc_lib import (
    CONTENT_RULES,
    GROUPS,
    MODEL,
    all_teams,
    batched,
    get_anthropic,
    get_supabase,
    lint_record,
    teams_in_group,
)

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
BATCH_DELAY_SECONDS = 20

# fields the script owns (authoritative from wc_lib, never trust the model)
OWNED = ("name", "slug", "group_letter", "confederation", "flag_emoji")

# Real columns per table. We whitelist on upsert so stray/nested keys the model
# invents (e.g. nesting "players" inside the team object) are dropped rather than
# rejected by PostgREST with "Could not find the '<x>' column".
TEAM_MODEL_COLUMNS = (
    "fifa_ranking", "nickname", "manager", "best_wc_finish", "wc_appearances",
    "qualifying_path", "recent_form", "warmup_matches", "overview",
    "strengths", "weaknesses",
)
PLAYER_COLUMNS = (
    "name", "position", "club", "age", "caps", "goals", "description",
    "is_star_player", "is_player_to_watch", "sort_order",
)
PREDICTION_COLUMNS = (
    "predicted_group_pos", "predicted_exit_round", "top_scorer_name",
    "top_scorer_goals", "group_winner_odds", "tournament_winner_odds",
    "dark_horse_rating", "prediction_rationale",
)


# ── Claude helpers ─────────────────────────────────────────────────────────

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
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(raw[start : i + 1])
    raise ValueError("unbalanced JSON in model output")


# em dash (U+2014) and the horizontal-bar variant (U+2015); the en dash (U+2013)
# is left alone since it is legitimate in ranges and scorelines (e.g. "2013–18").
_DASH_RE = re.compile(r"\s*[—―]\s*")


def swap_dashes(text: str) -> str:
    """Replace em dashes with a comma so none ever reach the database.

    A comma is always grammatical (a semicolon only works between independent
    clauses), so we default to ', ' and normalise any surrounding whitespace.
    Per the content rules we never publish em dashes; this is the last-ditch
    swap before upsert.
    """
    if "—" not in text and "―" not in text:
        return text
    out = _DASH_RE.sub(", ", text)
    out = re.sub(r"\s+,", ",", out)   # no space before the comma
    out = re.sub(r",\s*,", ", ", out)  # collapse a doubled comma
    return out


def clean_strings(obj):
    """Recursively apply swap_dashes to every string in a dict/list tree."""
    if isinstance(obj, str):
        return swap_dashes(obj)
    if isinstance(obj, list):
        return [clean_strings(x) for x in obj]
    if isinstance(obj, dict):
        return {k: clean_strings(v) for k, v in obj.items()}
    return obj


def ask_json(client, prompt: str, max_tokens: int = 8000, retries: int = 2) -> dict:
    """Call Claude with web search; return parsed JSON, retrying on failures."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                tools=[WEB_SEARCH_TOOL],
                system=CONTENT_RULES,
                messages=[{"role": "user", "content": prompt}],
            )
            return _extract_json(_text_of(msg))
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"    ! attempt {attempt + 1} failed ({e}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"giving up after {retries + 1} attempts: {last_err}")


# ── prompts ────────────────────────────────────────────────────────────────

def team_prompt(team: dict) -> str:
    host_note = " They are a tournament HOST." if team["is_host"] else ""
    return f"""\
Research {team['name']} (FIFA official name) ahead of the 2026 FIFA World Cup.
They are in Group {team['group_letter']}.{host_note}

Use web search to find, as of June 2026: their current FIFA ranking, current head
coach, the confirmed/expected 26-man squad, their last 10 competitive results, and
every pre-tournament warm-up friendly scheduled between now and June 11 2026.

Then return ONE JSON object only (no markdown fences, no commentary) with EXACTLY
this shape:

{{
  "team": {{
    "fifa_ranking": <int>,
    "nickname": "<team nickname, e.g. La Albiceleste>",
    "manager": "<current head coach full name>",
    "best_wc_finish": "<e.g. Winners (2022) or Quarter-finals (2010)>",
    "wc_appearances": <int including 2026>,
    "qualifying_path": "<how they qualified, e.g. AFC third round Group A runner-up, or Host>",
    "recent_form": [
      {{"opponent": "<name>", "score": "<2-1>", "date": "<YYYY-MM-DD>", "competition": "<comp>", "result": "<W|D|L>"}}
    ],
    "warmup_matches": [
      {{"opponent": "<name>", "date": "<YYYY-MM-DD>", "venue": "<city/stadium>", "competition": "<Friendly>", "result": "<W|D|L|scheduled>", "score": "<2-1 or null if scheduled>"}}
    ],
    "overview": "<250-400 word team narrative>",
    "strengths": "<2-3 sentences>",
    "weaknesses": "<2-3 sentences>"
  }},
  "players": [
    {{"name": "<full name with diacritics>", "position": "<GK|DEF|MID|FWD>", "club": "<current club>", "age": <int as of June 2026>, "caps": <int>, "goals": <int>, "description": "<50-100 words>", "is_star_player": <bool>, "is_player_to_watch": <bool>, "sort_order": <int>}}
  ],
  "prediction": {{
    "predicted_group_pos": <1-4>,
    "predicted_exit_round": "<Group Stage|Round of 32|Round of 16|Quarter-finals|Semi-finals|Runner-up|Winners>",
    "top_scorer_name": "<predicted tournament top scorer for this team>",
    "top_scorer_goals": <number>,
    "group_winner_odds": "<market odds to win the group, e.g. 2.50 or 6/4>",
    "tournament_winner_odds": "<outright winner odds>",
    "dark_horse_rating": <1-5, 5 = maximum dark horse>,
    "prediction_rationale": "<100-200 word reasoning for the predicted exit round>"
  }}
}}

Rules: provide 3 to 5 players. Exactly ONE player has is_star_player true. Mark one
breakout/underrated pick is_player_to_watch true. Recent_form must be REAL matches
that actually happened. Follow every content rule in the system prompt (no em
dashes, no banned words, take a stance)."""


def warmups_prompt(team: dict) -> str:
    return f"""\
Use web search to find every pre-tournament warm-up friendly for {team['name']}
scheduled or played between now and 11 June 2026. For matches already played,
include the real final score and result. Return ONE JSON object only:

{{"warmup_matches": [
  {{"opponent": "<name>", "date": "<YYYY-MM-DD>", "venue": "<city/stadium>", "competition": "Friendly", "result": "<W|D|L|scheduled>", "score": "<2-1 or null>"}}
]}}"""


def players_prompt(team: dict) -> str:
    return f"""\
Identify the 3 to 5 key players for {team['name']} (FIFA official name) ahead of
the 2026 FIFA World Cup.

You MUST use web search to verify each player's CURRENT club team; transfers happen
often, so do not trust training data for club affiliations. Also check caps and
goals. Return ONE JSON object only (no markdown fences, no commentary):

{{"players": [
  {{"name": "<full name with diacritics>", "position": "<GK|DEF|MID|FWD>", "club": "<current club, web-verified>", "age": <int as of June 2026>, "caps": <int>, "goals": <int>, "description": "<50-100 words>", "is_star_player": <bool>, "is_player_to_watch": <bool>, "sort_order": <int>}}
]}}

Exactly ONE player has is_star_player true. Mark one breakout/underrated pick
is_player_to_watch true. Follow every content rule in the system prompt."""


def group_prompt(letter: str) -> str:
    names = ", ".join(t["name"] for t in teams_in_group(letter))
    return f"""\
Group {letter} of the 2026 FIFA World Cup contains: {names}.
Use web search for current form and odds. Return ONE JSON object only:

{{
  "name": "<short descriptive label, e.g. Group of Death, or The Open Group>",
  "venue_cities": ["<host city>", "<host city>"],
  "overview": "<150-250 word group analysis with a concrete fact or stat; take stances>",
  "predicted_qualification": "<e.g. 1st: Argentina, 2nd: Austria, 3rd: Algeria>"
}}"""


# ── upserts ────────────────────────────────────────────────────────────────

def replace_players(sb, team_id: str, raw_players: list[dict]) -> int:
    """Delete the team's existing players and insert the new set."""
    sb.table("wc_players").delete().eq("team_id", team_id).execute()
    players = []
    for i, p in enumerate(raw_players):
        row = {k: p[k] for k in PLAYER_COLUMNS if k in p}
        row["team_id"] = team_id
        row.setdefault("sort_order", i)
        players.append(row)
    if players:
        sb.table("wc_players").insert(players).execute()
    return len(players)


def team_id_for_slug(sb, slug: str) -> str | None:
    res = sb.table("wc_teams").select("id").eq("slug", slug).limit(1).execute()
    return res.data[0]["id"] if res.data else None


def upsert_team(sb, team: dict, data: dict) -> str:
    team_data = dict(data.get("team", {}))

    # The model sometimes nests players/prediction inside "team" instead of at
    # the top level; accept either location.
    players = data.get("players") or team_data.get("players") or []
    prediction = data.get("prediction") or team_data.get("prediction") or {}

    row = {k: team[k] for k in OWNED}
    row.update({k: team_data[k] for k in TEAM_MODEL_COLUMNS if k in team_data})
    res = sb.table("wc_teams").upsert(row, on_conflict="slug").execute()
    team_id = res.data[0]["id"]

    replace_players(sb, team_id, players)

    pred = {k: prediction[k] for k in PREDICTION_COLUMNS if k in prediction}
    if pred:
        pred["team_id"] = team_id
        sb.table("wc_predictions").upsert(pred, on_conflict="team_id").execute()

    return team_id


def lint_team(team: dict, data: dict) -> list[str]:
    t = data.get("team", {})
    issues = lint_record(
        team["name"],
        {
            "overview": t.get("overview"),
            "strengths": t.get("strengths"),
            "weaknesses": t.get("weaknesses"),
            "rationale": data.get("prediction", {}).get("prediction_rationale"),
        },
    )
    for p in data.get("players", []):
        issues += lint_record(
            f"{team['name']}/{p.get('name', '?')}", {"description": p.get("description")}
        )
    return issues


# ── runners ────────────────────────────────────────────────────────────────

def run_team(sb, client, team: dict) -> bool:
    print(f"  - {team['name']} (Group {team['group_letter']}) ...", flush=True)
    try:
        data = ask_json(client, team_prompt(team))
        data = clean_strings(data)  # swap em dashes before upsert
        for issue in lint_team(team, data):
            print(f"      lint: {issue}")
        upsert_team(sb, team, data)
        n = len(data.get("players", []))
        print(f"    OK ({n} players)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    FAIL: {e}")
        return False


def run_players(sb, client, team: dict) -> bool:
    print(f"  - {team['name']} players ...", flush=True)
    team_id = team_id_for_slug(sb, team["slug"])
    if not team_id:
        print("    SKIP: team not populated yet (run without --players-only first)")
        return False
    try:
        data = ask_json(client, players_prompt(team), max_tokens=4000)
        data = clean_strings(data)  # swap em dashes before upsert
        players = data.get("players") or data.get("team", {}).get("players") or []
        for p in players:
            for issue in lint_record(
                f"{team['name']}/{p.get('name', '?')}", {"description": p.get("description")}
            ):
                print(f"      lint: {issue}")
        n = replace_players(sb, team_id, players)
        print(f"    OK ({n} players)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    FAIL: {e}")
        return False


def run_warmups(sb, client, team: dict) -> bool:
    print(f"  - {team['name']} warm-ups ...", flush=True)
    try:
        data = ask_json(client, warmups_prompt(team), max_tokens=2000)
        data = clean_strings(data)  # swap em dashes before upsert
        sb.table("wc_teams").update(
            {"warmup_matches": data.get("warmup_matches", [])}
        ).eq("slug", team["slug"]).execute()
        print(f"    OK ({len(data.get('warmup_matches', []))} matches)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    FAIL: {e}")
        return False


def run_group(sb, client, letter: str) -> bool:
    print(f"  - Group {letter} overview ...", flush=True)
    try:
        data = ask_json(client, group_prompt(letter), max_tokens=2000)
        data = clean_strings(data)  # swap em dashes before upsert
        for issue in lint_record(f"Group {letter}", {"overview": data.get("overview")}):
            print(f"      lint: {issue}")
        row = {"letter": letter, **data}
        sb.table("wc_groups").upsert(row, on_conflict="letter").execute()
        print("    OK")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    FAIL: {e}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="single group letter A-L")
    ap.add_argument("--team", help="single team slug, e.g. new-zealand")
    ap.add_argument("--warmups-only", action="store_true",
                    help="only re-scrape the warmup_matches field")
    ap.add_argument("--players-only", action="store_true",
                    help="only regenerate players for already-populated teams")
    ap.add_argument("--no-groups", action="store_true")
    args = ap.parse_args()

    if args.warmups_only and args.players_only:
        ap.error("--warmups-only and --players-only are mutually exclusive")
    if args.team and args.group:
        ap.error("--team and --group are mutually exclusive")

    # resolve and validate the team selection before touching any credentials
    if args.team:
        teams = [t for t in all_teams() if t["slug"] == args.team.lower()]
        if not teams:
            ap.error(f"unknown team slug: {args.team}")
        letters = [teams[0]["group_letter"]]
    else:
        if args.group and args.group.upper() not in GROUPS:
            ap.error(f"unknown group: {args.group} (expected A-L)")
        letters = [args.group.upper()] if args.group else list(GROUPS.keys())
        teams = [t for t in all_teams() if t["group_letter"] in letters]

    sb = get_supabase()
    client = get_anthropic()

    ok = 0
    fail = 0
    print(f"Processing {len(teams)} teams across groups {', '.join(letters)}\n")

    batches = list(batched(teams, 4))
    for bi, batch in enumerate(batches):
        print(f"Batch {bi + 1}/{len(batches)}: {', '.join(t['name'] for t in batch)}")
        for team in batch:
            if args.warmups_only:
                done = run_warmups(sb, client, team)
            elif args.players_only:
                done = run_players(sb, client, team)
            else:
                done = run_team(sb, client, team)
            ok += done
            fail += not done
        print()
        if bi < len(batches) - 1:  # delay between batches, not after the last
            time.sleep(BATCH_DELAY_SECONDS)

    if not args.warmups_only and not args.players_only and not args.no_groups and not args.team:
        print("Group overviews:")
        for letter in letters:
            ok += run_group(sb, client, letter)

    print(f"\nDone. success={ok} fail={fail}")


if __name__ == "__main__":
    main()
