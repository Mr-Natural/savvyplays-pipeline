#!/usr/bin/env python3
"""
wc_daily_recap.py
=================

Generate the "World Cup Daily" audio recap script for a given day of the 2026
FIFA World Cup, grounded in real data.

Pipeline
--------
1. Resolve the target date (default: yesterday, US Eastern).
2. Pull every COMPLETED match for that date from wc_matches (with our published
   wc_match_previews prediction + betting pick).
3. For each match, web-search (via Claude) for the colour the database doesn't
   hold: key moments, the standout player, and any injury news.
4. Grade our predictions deterministically in Python (never trust the model to
   decide win/loss): was the result right, did the betting pick land.
5. Compute the RUNNING scorecard across ALL completed matches to date.
6. Pull tomorrow's fixtures (with their betting angle).
7. Feed everything to Claude (claude-sonnet-4-6) with the script template, the
   standard CONTENT_RULES from wc_lib, and the voice guidelines, to write the
   final 800-1200 word script.
8. Write the script to recaps/day_<X>_recap.txt and a machine-readable summary
   to recaps/day_<X>_summary.json.

Usage
-----
    python wc_daily_recap.py                  # yesterday (US Eastern)
    python wc_daily_recap.py --date 2026-06-11
    python wc_daily_recap.py --date 2026-06-11 --dry-run   # skip the file writes
    python wc_daily_recap.py --no-research                 # skip web search (faster, less colour)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from wc_lib import (
    CONTENT_RULES,
    MODEL,
    get_anthropic,
    get_supabase,
    lint_content,
)

# Reuse the canonical web-search + JSON-extraction helpers used across the
# pipeline so research calls behave identically to wc_populate / wc_generate.
from wc_populate import ask_json, _text_of, clean_strings, swap_dashes


# Tournament kicks off June 11, 2026 (Day 1). Used to number the days.
TOURNAMENT_START = date(2026, 6, 11)
RECAPS_DIR = Path(__file__).with_name("recaps")

# The whole group stage runs in June/July, which is always EDT (UTC-4). Prefer
# a real tz database if present; fall back to the fixed summer offset so the
# script has no hard dependency on `tzdata` being installed on Windows.
try:
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata
    EASTERN = timezone(timedelta(hours=-4), name="EDT")


# ── voice guidelines (from the brief) ────────────────────────────────────────

VOICE_GUIDELINES = """\
VOICE FOR THIS SCRIPT (spoken-word daily recap):
- Conversational, not scripted-sounding. Short sentences. Punchy observations.
- Honest about prediction misses. Credibility comes from accountability, not
  from pretending you got everything right. If a pick lost, say so plainly.
- No filler phrases ("without further ado", "let's dive in", "having said that").
- Australian sports-analyst tone: occasional dry humour, no fake enthusiasm.
- Each match segment is self-contained. A listener may skip around, so do not
  rely on something said in an earlier segment.
- Total length: 800 to 1200 words (about 5 to 7 minutes at speaking pace).
"""

SCRIPT_TEMPLATE = """\
WORLD CUP DAILY — DAY {day} RECAP
SavvyPlays | {date_long}

[INTRO — 15 seconds]
One line to set up the day: how many matches, the headline storyline.

[MATCH SEGMENT — one per completed match, 45 to 60 seconds each]
TeamA score TeamB. Venue, City.
The result: one sentence on the scoreline and how it played out.
The key moment: the turning point or decisive incident.
The standout: one player who defined the match and why.
Group impact: what it means for the group, who is in trouble, who is through.
Our call: what we predicted, whether it hit, and an honest read on the miss if it
did not. Use the supplied grading, do not invent whether a pick won or lost.

[BETTING SCORECARD — 30 seconds]
Today's picks went X from Y. Name the best call and the worst miss. Then give the
running tournament record and win percentage. Use ONLY the supplied numbers.

[TOMORROW'S WATCH — 45 seconds]
The matches to circle tomorrow and the one-line betting angle for each. Point to
full previews at savvyplays.com.

[OUTRO — 10 seconds]
Sign off: "Data over hot takes." Keep it short.
"""


# ── date helpers ─────────────────────────────────────────────────────────────

def eastern_date(kickoff_utc: str | None) -> date | None:
    if not kickoff_utc:
        return None
    dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN).date()


def resolve_target_date(arg: str | None) -> date:
    if arg:
        return datetime.strptime(arg, "%Y-%m-%d").date()
    now_et = datetime.now(timezone.utc).astimezone(EASTERN)
    return now_et.date() - timedelta(days=1)


def day_number(target: date) -> int:
    return (target - TOURNAMENT_START).days + 1


# ── data access ──────────────────────────────────────────────────────────────

MATCH_SELECT = (
    "match_number,kickoff_utc,status,score_a,score_b,result,venue,city,"
    "stage,group_letter,"
    "team_a:wc_teams!team_a_id(name),"
    "team_b:wc_teams!team_b_id(name),"
    "preview:wc_match_previews(scoreline_prediction,betting_preview)"
)


def _one(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def fetch_matches(sb) -> list[dict]:
    """Every fixture with its teams and (if any) our published preview."""
    rows = (
        sb.table("wc_matches").select(MATCH_SELECT).order("match_number").execute().data
        or []
    )
    out = []
    for r in rows:
        ta = _one(r.get("team_a")) or {}
        tb = _one(r.get("team_b")) or {}
        preview = _one(r.get("preview")) or {}
        out.append(
            {
                "match_number": r.get("match_number"),
                "kickoff_utc": r.get("kickoff_utc"),
                "status": r.get("status"),
                "score_a": r.get("score_a"),
                "score_b": r.get("score_b"),
                "result": r.get("result"),
                "venue": r.get("venue"),
                "city": r.get("city"),
                "stage": r.get("stage"),
                "group_letter": r.get("group_letter"),
                "team_a": ta.get("name"),
                "team_b": tb.get("name"),
                "scoreline_prediction": preview.get("scoreline_prediction"),
                "betting": preview.get("betting_preview") or {},
                "et_date": eastern_date(r.get("kickoff_utc")),
            }
        )
    return out


# ── prediction grading (deterministic — never let the model decide) ──────────

# Aliases for names that commonly appear in shortened form inside the predicted
# scoreline string. Keys are the canonical wc_teams.name; values are extra
# lowercase strings to look for in addition to the canonical name.
_TEAM_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "United States": ("usa",),
}


def _team_in_text(team: str, text_lower: str) -> bool:
    if not team:
        return False
    if team.lower() in text_lower:
        return True
    for alias in _TEAM_NAME_ALIASES.get(team, ()):
        if alias in text_lower:
            return True
    return False


def grade_result(scoreline: str | None, result: str | None,
                 team_a: str, team_b: str) -> bool | None:
    """Did our predicted scoreline call the right outcome (team_a/team_b/draw)?"""
    if not scoreline or not result:
        return None
    m = re.search(r"(\d+)\s*[-–—]\s*(\d+)", scoreline)
    if not m:
        return None
    pa, pb = int(m.group(1)), int(m.group(2))
    before = scoreline[: m.start()].lower()
    after = scoreline[m.end():].lower()
    # The team named anywhere in the scoreline owns the first score: "Germany 3-1
    # Ecuador" and "2-1 USA" both put the listed team's score first. Check
    # "before" first for both teams so a leading name wins over a trailing one
    # in the "{first} {score} {second}" format.
    if _team_in_text(team_a, before):
        first = "team_a"
    elif _team_in_text(team_b, before):
        first = "team_b"
    elif _team_in_text(team_a, after):
        first = "team_a"
    elif _team_in_text(team_b, after):
        first = "team_b"
    else:
        return None
    second = "team_b" if first == "team_a" else "team_a"
    if pa > pb:
        predicted = first
    elif pb > pa:
        predicted = second
    else:
        predicted = "draw"
    return predicted == result


def grade_pick(pick: str | None, score_a, score_b,
               team_a: str, team_b: str) -> bool | None:
    """Grade the betting pick for the common markets. None = not auto-gradeable."""
    if not pick or score_a is None or score_b is None:
        return None
    p = pick.lower()
    total = score_a + score_b

    m = re.search(r"\b(under|over)\b.*?(\d+(?:\.\d+)?)", p)
    if m:
        line = float(m.group(2))
        return total < line if m.group(1) == "under" else total > line

    if "btts" in p or "both teams to score" in p:
        yes = score_a > 0 and score_b > 0
        return (not yes) if " no" in p else yes

    if "draw no bet" in p:
        # DNB: backed team wins → win, draw → push (None, excluded from W/L),
        # backed team loses → loss. Named team is identified in the pick string.
        drew = score_a == score_b
        if drew:
            return None
        a_won = score_a > score_b
        b_won = score_b > score_a
        if team_a and _team_in_text(team_a, p):
            return a_won
        if team_b and _team_in_text(team_b, p):
            return b_won
        return None  # team not identifiable — leave for a human

    # Double Chance: "<Team> Double Chance" or "<Team> or Draw" / "Draw or <Team>".
    # Wins if the named team wins OR the match draws.
    is_dc = ("double chance" in p
             or re.search(r"\bor\s+draw\b", p)
             or re.search(r"\bdraw\s+or\b", p))
    if is_dc:
        drew = score_a == score_b
        a_won, b_won = score_a > score_b, score_b > score_a
        if team_a and team_a.lower() in p:
            return drew or a_won
        if team_b and team_b.lower() in p:
            return drew or b_won
        return None  # team not identifiable — leave for a human

    if re.search(r"\bdraw\b", p) and "no" not in p:
        return score_a == score_b

    if re.search(r"\bml\b", p) or "moneyline" in p or "to win" in p or "win outright" in p:
        if score_a > score_b:
            winner = "team_a"
        elif score_b > score_a:
            winner = "team_b"
        else:
            winner = "draw"
        if team_a and team_a.lower() in p:
            return winner == "team_a"
        if team_b and team_b.lower() in p:
            return winner == "team_b"

    return None  # unknown market — don't guess


def score_str(m: dict) -> str:
    return f"{m['team_a']} {m['score_a']}-{m['score_b']} {m['team_b']}"


# ── per-match web research ───────────────────────────────────────────────────

RESEARCH_SCHEMA_HINT = """\
Return ONLY a JSON object with these keys:
{
  "key_moment": "one sentence on the decisive incident or turning point",
  "standout_player": "the single player who defined the match",
  "standout_reason": "one sentence on why they stood out (goals, assists, saves, red card drawn, etc.)",
  "injury_news": "any injury or suspension news from the match, or empty string if none",
  "group_impact": "one sentence on what the result means for the group"
}
"""


def research_match(client, m: dict) -> dict:
    prompt = f"""\
{m['team_a']} played {m['team_b']} at the 2026 FIFA World Cup on \
{m['et_date']}. The final score was {m['team_a']} {m['score_a']}, \
{m['team_b']} {m['score_b']} ({m['stage']}{', Group ' + m['group_letter'] if m['group_letter'] else ''}).

Use web search to confirm what happened in THIS match. Prefer sources dated 2026.
Find the goalscorers and minutes, the decisive moment, the standout performer,
and any injuries or suspensions that came out of the game.

{RESEARCH_SCHEMA_HINT}
"""
    try:
        data = ask_json(client, prompt, max_tokens=2000)
        return {k: (data.get(k) or "") for k in
                ("key_moment", "standout_player", "standout_reason",
                 "injury_news", "group_impact")}
    except Exception as e:  # noqa: BLE001
        print(f"    ! research failed for match {m['match_number']}: {e}")
        return {"key_moment": "", "standout_player": "", "standout_reason": "",
                "injury_news": "", "group_impact": ""}


# ── final script generation ──────────────────────────────────────────────────

def build_generation_prompt(target: date, day: int, todays: list[dict],
                            running: dict, tomorrow: list[dict]) -> str:
    date_long = target.strftime("%A, %B %-d, %Y") if sys.platform != "win32" \
        else target.strftime("%A, %B ") + str(target.day) + target.strftime(", %Y")

    lines: list[str] = []
    lines.append(f"Write the World Cup Daily recap for Day {day} ({date_long}).")
    lines.append("")
    lines.append("Use ONLY the facts below. Do not invent scores, scorers, or "
                 "betting outcomes. Every 'Our call' must match the supplied grading.")
    lines.append("")
    lines.append("=== COMPLETED MATCHES TODAY ===")
    for m in todays:
        lines.append("")
        lines.append(f"- {score_str(m)}  |  {m['venue']}, {m['city']}")
        rc = m["result_correct"]
        pc = m["pick_correct"]
        lines.append(f"  Our predicted scoreline: {m['scoreline_prediction'] or 'n/a'} "
                     f"({'result CORRECT' if rc else 'result WRONG' if rc is False else 'result ungraded'})")
        pick = (m["betting"] or {}).get("savvyplays_pick")
        if pick:
            verdict = ("WON" if pc else "LOST" if pc is False else "not auto-graded")
            lines.append(f"  Our betting pick: {pick} ({verdict})")
        r = m["research"]
        if r.get("key_moment"):
            lines.append(f"  Key moment: {r['key_moment']}")
        if r.get("standout_player"):
            lines.append(f"  Standout: {r['standout_player']} — {r.get('standout_reason','')}")
        if r.get("group_impact"):
            lines.append(f"  Group impact: {r['group_impact']}")
        if r.get("injury_news"):
            lines.append(f"  Injury/suspension: {r['injury_news']}")

    lines.append("")
    lines.append("=== BETTING SCORECARD ===")
    lines.append(f"Today: results {running['today_results_correct']}/{running['today_results_total']} "
                 f"correct, picks {running['today_picks_correct']}/{running['today_picks_total']}.")
    lines.append(f"Running tournament record (all completed matches): "
                 f"results {running['results_correct']}/{running['results_total']}, "
                 f"picks {running['picks_correct']}/{running['picks_total']} "
                 f"({running['picks_pct']}%).")
    if running.get("best_call"):
        lines.append(f"Best call so far today: {running['best_call']}")
    if running.get("worst_miss"):
        lines.append(f"Worst miss today: {running['worst_miss']}")

    lines.append("")
    lines.append("=== TOMORROW'S FIXTURES ===")
    if tomorrow:
        for m in tomorrow:
            angle = (m["betting"] or {}).get("savvyplays_pick") or "see preview"
            lines.append(f"- {m['team_a']} vs {m['team_b']}  |  {m['venue']}, {m['city']}  "
                         f"|  betting angle: {angle}")
    else:
        lines.append("- No fixtures scheduled.")

    lines.append("")
    lines.append("=== TEMPLATE TO FOLLOW ===")
    lines.append(SCRIPT_TEMPLATE.format(day=day, date_long=date_long))
    lines.append("")
    lines.append(VOICE_GUIDELINES)
    lines.append("")
    lines.append("Output the finished script text only. No preamble, no JSON, no "
                 "markdown code fences.")
    return "\n".join(lines)


def generate_script(client, prompt: str) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=CONTENT_RULES + "\n\n" + VOICE_GUIDELINES,
        messages=[{"role": "user", "content": prompt}],
    )
    text = _text_of(msg).strip()
    # Last-ditch removal of any em dashes per the content rules.
    return swap_dashes(text)


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate the World Cup Daily recap script.")
    p.add_argument("--date", help="Recap date, YYYY-MM-DD (default: yesterday US Eastern).")
    p.add_argument("--no-research", action="store_true",
                   help="Skip per-match web search (faster, less colour).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the script to stdout without writing files.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    target = resolve_target_date(args.date)
    day = day_number(target)
    tomorrow_date = target + timedelta(days=1)

    print(f"World Cup Daily — Day {day} ({target.isoformat()})")

    sb = get_supabase()
    client = get_anthropic()

    matches = fetch_matches(sb)

    todays = [m for m in matches
              if m["status"] == "completed" and m["et_date"] == target]
    todays.sort(key=lambda m: m["match_number"] or 0)
    tomorrow = [m for m in matches
                if m["et_date"] == tomorrow_date
                and m["status"] in ("scheduled", "preview_published")]
    tomorrow.sort(key=lambda m: m["match_number"] or 0)

    if not todays:
        print(f"  No completed matches found for {target.isoformat()}. Nothing to recap.")
        return 1

    print(f"  {len(todays)} completed match(es) today; {len(tomorrow)} fixture(s) tomorrow.")

    # Grade every completed match (for the running record), and research today's.
    completed = [m for m in matches if m["status"] == "completed"]
    for m in completed:
        m["result_correct"] = grade_result(
            m["scoreline_prediction"], m["result"], m["team_a"], m["team_b"])
        m["pick_correct"] = grade_pick(
            (m["betting"] or {}).get("savvyplays_pick"),
            m["score_a"], m["score_b"], m["team_a"], m["team_b"])

    for m in todays:
        if args.no_research:
            m["research"] = {"key_moment": "", "standout_player": "",
                             "standout_reason": "", "injury_news": "", "group_impact": ""}
        else:
            print(f"  researching match {m['match_number']}: {score_str(m)}")
            m["research"] = research_match(client, m)

    # Running scorecard across ALL completed matches.
    def tally(rows, key):
        graded = [r[key] for r in rows if r.get(key) is not None]
        return sum(1 for g in graded if g), len(graded)

    res_c, res_t = tally(completed, "result_correct")
    pick_c, pick_t = tally(completed, "pick_correct")
    tres_c, tres_t = tally(todays, "result_correct")
    tpick_c, tpick_t = tally(todays, "pick_correct")

    today_won = [m for m in todays if m.get("pick_correct") is True]
    today_lost = [m for m in todays if m.get("pick_correct") is False]
    best_call = ((today_won[0]["betting"].get("savvyplays_pick") + " — " + score_str(today_won[0]))
                 if today_won else "")
    worst_miss = ((today_lost[0]["betting"].get("savvyplays_pick") + " — " + score_str(today_lost[0]))
                  if today_lost else "")

    running = {
        "results_correct": res_c, "results_total": res_t,
        "picks_correct": pick_c, "picks_total": pick_t,
        "picks_pct": round(100 * pick_c / pick_t) if pick_t else 0,
        "today_results_correct": tres_c, "today_results_total": tres_t,
        "today_picks_correct": tpick_c, "today_picks_total": tpick_t,
        "best_call": best_call, "worst_miss": worst_miss,
    }

    prompt = build_generation_prompt(target, day, todays, running, tomorrow)
    print("  generating script via Claude...")
    script = generate_script(client, prompt)
    word_count = len(script.split())
    issues = lint_content(script)

    print(f"  script generated: {word_count} words"
          + (f"; {len(issues)} lint issue(s): {issues}" if issues else "; clean"))

    if args.dry_run:
        print("\n" + "=" * 70 + "\n")
        sys.stdout.buffer.write(script.encode("utf-8"))
        print("\n" + "=" * 70)
        return 0

    RECAPS_DIR.mkdir(exist_ok=True)
    recap_path = RECAPS_DIR / f"day_{day}_recap.txt"
    summary_path = RECAPS_DIR / f"day_{day}_summary.json"

    recap_path.write_text(script + "\n", encoding="utf-8")

    summary = {
        "day": day,
        "date": target.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "word_count": word_count,
        "lint_issues": issues,
        "scorecard": running,
        "matches": [
            {
                "match_number": m["match_number"],
                "fixture": f"{m['team_a']} v {m['team_b']}",
                "score": f"{m['score_a']}-{m['score_b']}",
                "result": m["result"],
                "our_scoreline": m["scoreline_prediction"],
                "our_pick": (m["betting"] or {}).get("savvyplays_pick"),
                "result_correct": m["result_correct"],
                "pick_correct": m["pick_correct"],
                "research": m["research"],
            }
            for m in todays
        ],
        "tomorrow": [
            {
                "fixture": f"{m['team_a']} v {m['team_b']}",
                "venue": m["venue"], "city": m["city"],
                "betting_angle": (m["betting"] or {}).get("savvyplays_pick"),
            }
            for m in tomorrow
        ],
        "recap_file": str(recap_path),
    }
    summary_path.write_text(
        json.dumps(clean_strings(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"  ✓ wrote {recap_path}")
    print(f"  ✓ wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
