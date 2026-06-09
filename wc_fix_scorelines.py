"""
wc_fix_scorelines.py
Add realistic variety to the Matchday 1 scoreline predictions WITHOUT regenerating
previews. Updates only three fields in wc_match_previews:
  - scoreline_prediction       (normalised to "Team A X-X Team B", team_a first)
  - betting_preview.savvyplays_pick
  - betting_preview.pick_rationale

All 24 scorelines are normalised; 11 matches get new draw/upset/high-scoring
results and matching picks. The narrative body of the previews is left untouched.

Usage:
    python wc_fix_scorelines.py --dry-run
    python wc_fix_scorelines.py            # confirm, then apply
    python wc_fix_scorelines.py --yes
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from wc_lib import get_supabase

CHANGELOG = Path(__file__).with_name("wc_scoreline_fixes_applied.md")

# match_number -> (team_a_score, team_b_score), team_a-first perspective
RESULTS: dict[int, tuple[int, int]] = {
    1: (1, 0), 2: (0, 1), 3: (1, 0), 4: (1, 0), 5: (0, 2), 6: (1, 1),
    7: (1, 2), 8: (0, 1), 9: (4, 0), 10: (1, 1), 11: (0, 0), 12: (1, 0),
    13: (0, 1), 14: (3, 0), 15: (0, 0), 16: (1, 0), 17: (1, 0), 18: (1, 0),
    19: (1, 1), 20: (2, 0), 21: (3, 1), 22: (1, 1), 23: (0, 1), 24: (2, 1),
}

# match_number -> (savvyplays_pick, pick_rationale) for the changed matches only
PICKS: dict[int, tuple[str, str]] = {
    2: ("Czechia ML",
        "Czechia's set-piece height and Souček's late runs are exactly the profile "
        "that unsettles a Korea side still building defensive cohesion at the back. "
        "At a price, the value sits with the Czechs to nick this outright."),
    6: ("Both Teams to Score — Yes",
        "Morocco carry the pace on the counter and the defensive nous to hurt Brazil, "
        "who have leaked goals in transition all season. Both nets bulging looks the "
        "smart play between two sides with quality at either end."),
    7: ("Scotland ML",
        "Scotland hold too much Premier League and Serie A experience for Haiti, who "
        "will not roll over and should grab one of their own. Back the Scots to win a "
        "game closer than the gap on paper suggests."),
    10: ("Draw",
         "Japan are the best-organised side outside the top seeds and have beaten "
         "Germany and Spain in recent windows, so the Dutch will not have this their "
         "own way. A share of the points is live at a tempting price."),
    11: ("Under 1.5 Goals",
         "Two cautious, defensively sound teams who both know a point keeps "
         "qualification firmly in their hands. Goals could be scarce, and Under 1.5 "
         "holds genuine appeal."),
    13: ("Egypt Double Chance",
         "Salah gives Egypt a puncher's chance against an ageing Belgium core that no "
         "longer presses with the old venom. Egypt Double Chance is the sensible way "
         "into a real upset angle."),
    15: ("Under 1.5 Goals",
         "Uruguay grind games down and Saudi Arabia sit deep, a recipe for a "
         "low-tempo, low-event opener. Under 1.5 is the percentage call."),
    19: ("Draw",
         "Norway have the firepower in Haaland but a soft underbelly at the back, and "
         "Iraq are stubborn and physical. The draw looks overpriced for a game that "
         "profiles as tight."),
    21: ("Over 2.5 Goals",
         "Portugal's attacking depth should yield several goals, and DR Congo carry "
         "enough threat through Wissa to find a consolation. Over 2.5 is the play in a "
         "game with goals written all over it."),
    22: ("Both Teams to Score — Yes",
         "Croatia's midfield can keep the ball off England long enough to create, and "
         "both sides carry the quality to find the net. Both teams to score rates "
         "strongly in a heavyweight opener."),
    24: ("Over 2.5 Goals",
         "Ghana have the firepower to win this but a back line that gives up chances, "
         "and Panama will fancy scoring. Lean to the overs in an open Group L opener."),
}


def one(v):
    return (v[0] if isinstance(v, list) else v) or {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sb = get_supabase()

    rows = (
        sb.table("wc_matches")
        .select("match_number,team_a:wc_teams!team_a_id(name),team_b:wc_teams!team_b_id(name),"
                "preview:wc_match_previews(id,betting_preview)")
        .lte("match_number", 24)
        .order("match_number")
        .execute()
        .data
    )

    plan = []
    for r in rows:
        prev = one(r.get("preview"))
        if not prev:
            continue
        num = r["match_number"]
        a, b = one(r.get("team_a")).get("name", "?"), one(r.get("team_b")).get("name", "?")
        ra, rb = RESULTS[num]
        scoreline = f"{a} {ra}-{rb} {b}"
        patch = {"scoreline_prediction": scoreline}
        pick_note = "(scoreline only)"
        if num in PICKS:
            pick, rationale = PICKS[num]
            bp = dict(prev.get("betting_preview") or {})
            bp["savvyplays_pick"] = pick
            bp["pick_rationale"] = rationale
            patch["betting_preview"] = bp
            pick_note = f"pick -> {pick}"
        plan.append({"num": num, "id": prev["id"], "scoreline": scoreline,
                     "patch": patch, "note": pick_note})

    print(f"\n{'#':>3}  {'Scoreline':<34}  Change")
    print("-" * 80)
    for p in plan:
        print(f"{p['num']:>3}  {p['scoreline']:<34}  {p['note']}")
    print(f"\n{len(plan)} previews ({len(PICKS)} with new pick + rationale)")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return
    if not args.yes:
        resp = input(f"\nApply scoreline + pick updates to {len(plan)} previews? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return

    for p in plan:
        sb.table("wc_match_previews").update(p["patch"]).eq("id", p["id"]).execute()
    print(f"\nApplied updates to {len(plan)} previews.")

    lines = [
        "# World Cup 2026 scoreline variety fixes applied", "",
        f"Run: {datetime.now():%Y-%m-%d %H:%M:%S}", "",
        "| # | Scoreline | Pick change |", "| --- | --- | --- |",
        *[f"| {p['num']} | {p['scoreline']} | {p['note']} |" for p in plan],
    ]
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Change log written to {CHANGELOG}")


if __name__ == "__main__":
    main()
