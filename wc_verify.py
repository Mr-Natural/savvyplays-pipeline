"""
wc_verify.py
Fact-check the populated World Cup data against the live web using Claude with
web search, and write a discrepancy report.

For each team it checks:
  - Is the manager correct and currently in charge?
  - Are the listed key players in the confirmed 26-man squad?
  - Are cap counts and goal tallies approximately correct?
  - Is the FIFA ranking within +/-3 of the actual ranking?
  - Are the recent match results real matches that actually happened?

It then runs a SEPARATE, dedicated web search for every player to confirm their
current club (searching '"<player>" <national team> 2026 squad club') and flags
any club that differs from what is stored. That is one extra API call per player,
so it is the most expensive part of a verify run.

Also re-runs the content linter over stored text.

Usage:
    python wc_verify.py              # all teams -> wc_verification_report.md
    python wc_verify.py --group D    # single group
    python wc_verify.py --fix        # also auto-correct low-risk fields
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from wc_lib import GROUPS, get_anthropic, get_supabase, lint_record
from wc_populate import ask_json

REPORT = Path(__file__).with_name("wc_verification_report.md")


def _norm_club(s: str | None) -> str:
    import unicodedata

    # transliterate accents (München -> Munchen) so they don't shatter tokens
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    drop = {"fc", "cf", "afc", "sc", "ac", "the", "club", "de"}
    toks = [t for t in s.split() if t not in drop]
    return " ".join(toks).strip()


def club_matches(stored: str | None, found: str | None) -> bool:
    """True if the two club strings plausibly refer to the same club."""
    a, b = _norm_club(stored), _norm_club(found)
    if not a or not b:
        return True  # nothing to compare against -> don't flag
    if a == b or a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return True
    jaccard = len(ta & tb) / len(ta | tb)
    return jaccard >= 0.5


def club_check_prompt(player_name: str, team_name: str) -> str:
    return f"""\
Use web search to confirm the CURRENT club team of {player_name}, a {team_name}
international, for the 2026 season. Search exactly: "{player_name}" {team_name} 2026
squad club. Transfers happen often, so trust recent web sources over prior
knowledge. Return ONE JSON object only:

{{"current_club": "<club name>", "confidence": <0-100>, "note": "<short, e.g. joined Jan 2026>"}}"""


def verify_clubs(client, team: dict) -> tuple[list[str], int]:
    """Independent per-player web search to confirm each stored club.

    One dedicated search per player, so this is the costly part of a verify run.
    Returns (report lines, mismatch count).
    """
    players = team.get("_players", [])
    detail: list[str] = []
    checked = 0
    mismatches = 0

    for p in players:
        name = p.get("name")
        if not name:
            continue
        stored = p.get("club")
        try:
            r = ask_json(client, club_check_prompt(name, team["name"]), max_tokens=1500)
        except Exception as e:  # noqa: BLE001
            detail.append(f"- club check {name}: ERROR ({e})")
            continue
        checked += 1
        found = r.get("current_club")
        if found and not club_matches(stored, found):
            mismatches += 1
            note = r.get("note", "")
            detail.append(
                f"- club mismatch {name}: stored `{stored}` vs web `{found}`."
                + (f" {note}" if note else "")
            )

    if checked:
        summary = f"- club check: {checked - mismatches}/{checked} players matched web sources"
    else:
        summary = "- club check: no players to verify"
    return [summary, *detail], mismatches


def verify_prompt(team: dict, players: list[dict]) -> str:
    plist = "\n".join(
        f"  - {p['name']} ({p.get('position', '?')}, {p.get('club', '?')}): "
        f"{p.get('caps', '?')} caps, {p.get('goals', '?')} goals"
        for p in players
    )
    form = json.dumps(team.get("recent_form", []), ensure_ascii=False)
    return f"""\
Fact-check this stored data for {team['name']} against current web sources
(as of June 2026). Be strict and cite what you find.

Stored manager: {team.get('manager')}
Stored FIFA ranking: {team.get('fifa_ranking')}
Stored key players:
{plist}
Stored recent_form: {form}

Use web search, then return ONE JSON object only:

{{
  "manager": {{"ok": <bool>, "actual": "<current head coach>", "note": "<short>"}},
  "fifa_ranking": {{"ok": <bool, true if within +/-3>, "actual": <int>, "note": "<short>"}},
  "players": [
    {{"name": "<name>", "in_squad": <bool>, "caps_ok": <bool>, "goals_ok": <bool>, "note": "<short>"}}
  ],
  "recent_form_real": <bool>,
  "recent_form_note": "<short>",
  "issues": ["<concise discrepancy>", "..."],
  "corrections": {{"fifa_ranking": <int or null>, "manager": "<string or null>"}}
}}

Only include a non-null correction when you are confident. Keep notes short."""


def fetch_teams(sb, letters: list[str]) -> list[dict]:
    rows = (
        sb.table("wc_teams")
        .select("*")
        .in_("group_letter", letters)
        .order("group_letter")
        .execute()
        .data
    )
    for t in rows:
        t["_players"] = (
            sb.table("wc_players")
            .select("name,position,club,caps,goals")
            .eq("team_id", t["id"])
            .order("sort_order")
            .execute()
            .data
        )
    return rows


def render(team: dict, v: dict) -> tuple[list[str], bool]:
    lines: list[str] = [f"### {team['name']} (Group {team['group_letter']})"]
    flagged = False

    mgr = v.get("manager", {})
    if not mgr.get("ok", True):
        flagged = True
        lines.append(f"- Manager: stored `{team.get('manager')}` vs actual `{mgr.get('actual')}`. {mgr.get('note', '')}")

    rk = v.get("fifa_ranking", {})
    if not rk.get("ok", True):
        flagged = True
        lines.append(f"- FIFA ranking: stored `{team.get('fifa_ranking')}` vs actual `{rk.get('actual')}`. {rk.get('note', '')}")

    for p in v.get("players", []):
        probs = []
        if not p.get("in_squad", True):
            probs.append("not in 26-man squad")
        if not p.get("caps_ok", True):
            probs.append("caps off")
        if not p.get("goals_ok", True):
            probs.append("goals off")
        if probs:
            flagged = True
            lines.append(f"- Player {p.get('name')}: {', '.join(probs)}. {p.get('note', '')}")

    if not v.get("recent_form_real", True):
        flagged = True
        lines.append(f"- Recent form questionable: {v.get('recent_form_note', '')}")

    for issue in v.get("issues", []):
        flagged = True
        lines.append(f"- {issue}")

    # content lint over stored prose
    lint = lint_record(
        team["name"],
        {"overview": team.get("overview"), "strengths": team.get("strengths"),
         "weaknesses": team.get("weaknesses")},
    )
    for issue in lint:
        flagged = True
        lines.append(f"- content rule: {issue}")

    return lines, flagged


def apply_fix(sb, team: dict, v: dict) -> list[str]:
    corr = v.get("corrections", {}) or {}
    update = {}
    if corr.get("fifa_ranking") is not None and not v.get("fifa_ranking", {}).get("ok", True):
        update["fifa_ranking"] = corr["fifa_ranking"]
    if corr.get("manager") and not v.get("manager", {}).get("ok", True):
        update["manager"] = corr["manager"]
    if update:
        sb.table("wc_teams").update(update).eq("id", team["id"]).execute()
        return [f"  fixed: {update}"]
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="single group letter A-L")
    ap.add_argument("--fix", action="store_true", help="auto-correct low-risk fields")
    args = ap.parse_args()

    sb = get_supabase()
    client = get_anthropic()
    letters = [args.group.upper()] if args.group else list(GROUPS.keys())

    teams = fetch_teams(sb, letters)
    if not teams:
        print("No teams found. Run wc_populate.py first.")
        return

    out = ["# World Cup 2026 verification report", ""]
    flagged_count = 0

    for team in teams:
        print(f"Verifying {team['name']} ...", flush=True)
        try:
            v = ask_json(client, verify_prompt(team, team["_players"]), max_tokens=3000)
        except Exception as e:  # noqa: BLE001
            out += [f"### {team['name']}", f"- ERROR during verification: {e}", ""]
            print(f"  ERROR: {e}")
            continue
        lines, flagged = render(team, v)

        club_lines, club_mismatches = verify_clubs(client, team)
        lines += club_lines

        team_flagged = flagged or club_mismatches > 0
        if not team_flagged:
            lines.append("- No discrepancies found.")
        flagged_count += int(team_flagged)

        if args.fix and flagged:
            lines += apply_fix(sb, team, v)
        out += lines + [""]
        print("  flagged" if team_flagged else "  clean")

    out.insert(2, f"Teams checked: {len(teams)} | flagged: {flagged_count}")
    out.insert(3, "")
    REPORT.write_text("\n".join(out), encoding="utf-8")
    print(f"\nReport written to {REPORT}")


if __name__ == "__main__":
    main()
