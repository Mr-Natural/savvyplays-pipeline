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
import re
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

- Each preview must contain exactly ONE primary pick. Never combine picks with
  '/' or 'and'. If multiple angles have value, choose the single strongest one
  for the primary pick. Secondary angles can be mentioned in the betting_preview
  prose but must not appear in the pick field.
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
- TOURNAMENT SCORING CONTEXT: This World Cup is averaging 3.05 goals per match
  through 48 games — the highest group-stage rate since 1958, well above the 2.5
  average from 2018 and 2022. Only one match has finished 0-0 (Spain vs Cabo Verde
  MD1). Calibrate picks accordingly:
  * Default to Over 2.5 Goals unless both teams have a specific defensive profile
    (e.g. both drew 0-0 earlier or a dead rubber where both park the bus).
  * Default to BTTS Yes where both teams have shown attacking intent, even with a
    clear quality gap. Lower-ranked sides (DR Congo, Curaçao, Iraq, Jordan, New
    Zealand, Cabo Verde) have all scored against top opposition.
  * MD3 squad rotation: teams already qualified often rest key players. Second-
    string defenders tend to concede more — factor this into totals AND ML picks
    (dead-rubber favourites are less reliable on the moneyline).
  * Prefer smaller Asian Handicap lines or Double Chance over heavy favourite ML
    in mismatch games. The expanded 48-team format has been more competitive than
    expected.
  * Scoreline predictions should reflect the 3.0+ average. 2-1 and 2-0 are the
    new baseline; 3-1 and 2-2 are common. Do not default to 1-0.
  * State the tournament scoring context briefly in the betting_preview so readers
    understand the reasoning.
"""


KNOCKOUT_RULES = """\
You are writing a knockout-round preview for the 2026 FIFA World Cup. In addition
to every standing content rule, follow these knockout-specific instructions:

TOURNAMENT CONTEXT (mandatory inclusions):
- Open with how each team qualified: group position (1st/2nd/3rd), points, GD,
  and a one-line read on their group-stage trajectory (e.g. "started slow, peaked
  in MD3" or "dominant throughout but conceded in every game").
- List each team's three group-stage results with scorelines.
- Name every goalscorer from the tournament so far with their tally (e.g.
  "Haaland 5, Sorloth 2, Odegaard 1"). Pull this from the tournament_stats
  context block provided -- do not invent or estimate tallies.
- Reference key moments: red cards, penalty saves, comeback wins, injuries
  sustained during the group stage that affect selection.

KNOCKOUT DYNAMICS:
- There are no draws. If level after 90 minutes, extra time and penalties follow.
  Reference each team's penalty shootout history and temperament where relevant.
- Yellow card accumulation resets after the group stage. Note any players who
  were suspended for MD3 and are now available, or any who picked up knocks.
- Squad rotation from MD3 dead rubbers means some teams' best XI hasn't played
  together for 6+ days. Flag where match sharpness could be an issue.
- Fatigue and travel matter more in knockouts. Note days of rest between last
  group game and this fixture, plus any cross-country travel between venues.

BETTING CALIBRATION:
- The tournament is averaging {goals_per_match} goals per match. Continue to
  calibrate totals picks against this baseline, but note that knockout football
  historically trends slightly lower than group stages (higher stakes, more
  cautious approaches). A small regression toward 2.5 is reasonable.
- Knockout upsets are more common than group-stage upsets because one bad half
  ends your tournament. Double Chance and Draw No Bet offer value on live
  underdogs. Do not dismiss lower-ranked teams that showed fight in the groups.
- Asian Handicap lines tighten in knockouts. Be precise about where value sits.
- Each preview must contain exactly ONE primary pick. Never combine picks with
  '/' or 'and' or '+'. Secondary angles go in the prose only.

HEADLINE & FRAMING:
- The headline must capture the elimination stakes. "Loser Goes Home" energy
  without being cliche.
- Acknowledge the bracket path -- who the winner likely faces next. This adds
  tactical context (teams might play for a specific side of the draw).
- Reference the venue, crowd composition (host-nation advantage for USA/Mexico/
  Canada), and conditions.

SCORELINE PREDICTIONS:
- Reflect the knockout context. 1-0 and 2-1 are more common in elimination
  games than group stages. Scoreline predictions can trend slightly lower than
  the group-stage calibration, but do not default to 0-0 draws (the match must
  produce a winner).
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


_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def build_tournament_stats(sb, team_a_id: str, team_b_id: str) -> tuple[str, float]:
    """Build the tournament_stats prompt block and return (block_text, goals_per_match).

    Aggregates from completed group-stage matches: per-team results, record, group
    finishing position, and goalscorer tallies from match_events. The tournament
    GPM is averaged across every completed match (group + any knockouts so far).
    """
    matches = (
        sb.table("wc_matches")
        .select("match_number,stage,group_letter,team_a_id,team_b_id,"
                "score_a,score_b,result,status,match_events")
        .eq("status", "completed")
        .execute()
        .data
    )
    teams = sb.table("wc_teams").select("id,name,group_letter").execute().data
    name_of = {t["id"]: t["name"] for t in teams}
    group_of = {t["id"]: t["group_letter"] for t in teams}

    scored = [m for m in matches if m["score_a"] is not None and m["score_b"] is not None]
    if scored:
        gpm = round(sum(m["score_a"] + m["score_b"] for m in scored) / len(scored), 2)
    else:
        gpm = 2.5
    completed_n = len(scored)

    def team_record(team_id: str) -> dict:
        letter = group_of.get(team_id)
        # Group-stage matches feed the group standings block below. The team's
        # own record (W/D/L, scorers, results list) spans every completed match
        # they've played — group + knockouts — so knockout previews see the
        # full tournament picture.
        group_matches = [m for m in scored
                         if m["stage"] == "Group Stage" and m["group_letter"] == letter]
        own = [m for m in scored if team_id in (m["team_a_id"], m["team_b_id"])]
        own.sort(key=lambda m: m["match_number"])

        results, w, d, l, gf, ga = [], 0, 0, 0, 0, 0
        scorers: dict[str, int] = {}
        for m in own:
            is_a = m["team_a_id"] == team_id
            us, them = (m["score_a"], m["score_b"]) if is_a else (m["score_b"], m["score_a"])
            opp_id = m["team_b_id"] if is_a else m["team_a_id"]
            opp = name_of.get(opp_id, "?")
            # Determine outcome, respecting knockout shootouts. A 1-1 draw with
            # result='team_b_pens' means team_b advanced on penalties and gets
            # the W; team_a takes the L. Ordinary regulation results still use
            # goal comparison.
            result = m.get("result")
            if us > them:
                outcome = "W"
            elif us < them:
                outcome = "L"
            elif result == "team_a_pens":
                outcome = "W" if is_a else "L"
            elif result == "team_b_pens":
                outcome = "L" if is_a else "W"
            else:
                outcome = "D"
            if outcome == "W": w += 1
            elif outcome == "D": d += 1
            else: l += 1
            gf += us; ga += them
            pens_tag = " (pens)" if result in ("team_a_pens", "team_b_pens") else ""
            results.append(f"vs {opp}: {us}-{them} {outcome}{pens_tag}")
            for ev in (m.get("match_events") or []):
                et = (ev.get("event_type") or "").lower()
                ev_side = ev.get("team")
                ours = "a" if is_a else "b"
                if et in ("goal", "penalty") and ev_side == ours:
                    p = (ev.get("player") or "").strip()
                    if p:
                        scorers[p] = scorers.get(p, 0) + 1

        # Group standings (4 teams in the group), rank by points/GD/GF.
        groupmates: dict[str, dict] = {}
        for m in group_matches:
            for tid, sf, sa in ((m["team_a_id"], m["score_a"], m["score_b"]),
                                (m["team_b_id"], m["score_b"], m["score_a"])):
                row = groupmates.setdefault(tid, {"pts": 0, "gd": 0, "gf": 0})
                row["pts"] += 3 if sf > sa else 1 if sf == sa else 0
                row["gd"] += sf - sa
                row["gf"] += sf
        ranked = sorted(groupmates.items(),
                        key=lambda kv: (-kv[1]["pts"], -kv[1]["gd"], -kv[1]["gf"]))
        position = next((i + 1 for i, (tid, _) in enumerate(ranked) if tid == team_id), 0)

        return {
            "group": letter, "position": position,
            "results": results, "w": w, "d": d, "l": l,
            "gf": gf, "ga": ga, "gd": gf - ga, "pts": w * 3 + d,
            "scorers": sorted(scorers.items(), key=lambda kv: (-kv[1], kv[0])),
        }

    def fmt(team_id: str) -> str:
        r = team_record(team_id)
        nm = name_of.get(team_id, "?")
        pos = _ORDINAL.get(r["position"], f"{r['position']}th") if r["position"] else "?"
        results_block = "\n    ".join(r["results"]) if r["results"] else "(no completed group matches)"
        gd_str = f"+{r['gd']}" if r["gd"] > 0 else str(r["gd"])
        scorers_str = ", ".join(f"{p} {n}" for p, n in r["scorers"]) or "none recorded"
        return (
            f"{nm} -- Group {r['group']}, finished {pos}:\n"
            f"  Results:\n    {results_block}\n"
            f"  Record: {r['w']}W-{r['d']}D-{r['l']}L, GF {r['gf']}, GA {r['ga']}, "
            f"GD {gd_str}, {r['pts']} points\n"
            f"  Goalscorers: {scorers_str}"
        )

    block = (
        "TOURNAMENT STATS (use exactly these numbers -- do not invent or estimate):\n\n"
        f"Tournament average so far: {gpm} goals per match across {completed_n} "
        f"completed matches.\n\n"
        f"{fmt(team_a_id)}\n\n"
        f"{fmt(team_b_id)}"
    )
    return block, gpm


def preview_prompt(match: dict, ta: dict, tb: dict, odds: dict | None,
                   tournament_stats: str | None = None,
                   goals_per_match: float | None = None) -> str:
    kickoff = match.get("kickoff_utc") or "TBC"
    is_group = match["stage"] == "Group Stage"
    context = (
        f"Group {match.get('group_letter')} matchday "
        f"{1 if match['match_number'] <= 24 else 2 if match['match_number'] <= 48 else 3}"
        if is_group else match["stage"]
    )
    odds_note = (
        f"Current odds from {odds.get('source')}: {json.dumps({k: v for k, v in odds.items() if k != 'source'})}. "
        "Use these in betting_preview and set betting_preview.source accordingly."
        if odds
        else "No odds feed was available. Use web search to find current match odds "
        "(match result, over/under 2.5, both teams to score) from a major bookmaker "
        "and set betting_preview.source to that bookmaker."
    )

    if is_group:
        rules_block = MATCH_RULES
        stats_section = ""
    else:
        rules_block = KNOCKOUT_RULES.format(goals_per_match=goals_per_match or 2.5)
        stats_section = f"\n{tournament_stats}\n" if tournament_stats else ""

    return f"""\
{rules_block}
{stats_section}
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


def check_scoreline_pick_consistency(label: str, data: dict) -> list[str]:
    """Warn (don't correct) when the SavvyPlays pick contradicts the predicted
    scoreline. Catches Over/Under-vs-total and BTTS-vs-clean-sheet mismatches.

    The pick is left as-is: some picks are deliberate value plays that differ
    from the modal scoreline. This surface only warns so a human can eyeball
    the tradeoff.
    """
    sp = (data.get("scoreline_prediction") or "").strip()
    bp = data.get("betting_preview") or {}
    pick = (bp.get("savvyplays_pick") or "").strip()
    if not sp or not pick:
        return []
    m = re.search(r"(\d+)\s*[-–—]\s*(\d+)", sp)
    if not m:
        return []
    a, b = int(m.group(1)), int(m.group(2))
    total = a + b
    plow = pick.lower()
    out: list[str] = []

    ou = re.search(r"\b(over|under)\s+(\d+(?:\.\d+)?)", plow)
    if ou:
        line = float(ou.group(2))
        if ou.group(1) == "under" and total > line:
            out.append(f"{label}: scoreline {a}-{b} total={total} contradicts pick {pick!r} (over the line)")
        elif ou.group(1) == "over" and total < line:
            out.append(f"{label}: scoreline {a}-{b} total={total} contradicts pick {pick!r} (under the line)")

    if "btts" in plow or "both teams to score" in plow:
        both = a > 0 and b > 0
        wants_no = re.search(r"\bno\b", plow) is not None
        wants_yes = (not wants_no) and re.search(r"\byes\b", plow) is not None
        # Default to 'yes' when neither yes/no is explicit (matches BettingPreviewPanel).
        wants_yes = wants_yes or not (wants_no or wants_yes)
        if wants_yes and not both:
            out.append(f"{label}: scoreline {a}-{b} has a clean sheet but pick {pick!r} needs both to score")
        elif wants_no and both:
            out.append(f"{label}: scoreline {a}-{b} has both scoring but pick {pick!r} is BTTS No")
    return out


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

    stats_block, gpm = (None, None)
    if match["stage"] != "Group Stage":
        stats_block, gpm = build_tournament_stats(sb, match["team_a_id"], match["team_b_id"])

    odds = fetch_odds(ta["name"], tb["name"])
    try:
        data = ask_json(
            client,
            preview_prompt(match, ta, tb, odds,
                           tournament_stats=stats_block, goals_per_match=gpm),
            max_tokens=8000,
        )
    except Exception as e:  # noqa: BLE001
        print(f"      FAIL: {e}")
        return False

    data = clean_strings(data)  # swap em dashes before write
    for issue in lint_preview(f"M{match['match_number']}", data):
        print(f"      lint: {issue}")
    for issue in check_scoreline_pick_consistency(f"M{match['match_number']}", data):
        print(f"      warn: {issue}")

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
