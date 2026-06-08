"""
wc_fix_predictions.py
Three fixes to wc_predictions:

  1. EXIT ROUNDS - set predicted_exit_round for all 48 teams to the exact bracket
     distribution (1 Winners, 1 Runner-up, 2 SF, 4 QF, 8 R16, 16 R32, 16 Group).
  2. ODDS FORMAT - convert any American (+900 / -150) or fractional (6/4) odds in
     tournament_winner_odds and group_winner_odds to decimal.
  3. RATIONALE - for any team whose exit round changed, rewrite only the
     sentence(s) in prediction_rationale that now contradict the new round, using
     Claude (same targeted approach as wc_lint_fix.py).

All changes are logged. Run --dry-run first to review the plan.

Usage:
    python wc_fix_predictions.py --dry-run
    python wc_fix_predictions.py            # review, confirm, apply
    python wc_fix_predictions.py --yes      # apply without prompting
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from wc_lib import CONTENT_RULES, MODEL, get_anthropic, get_supabase
from wc_populate import _extract_json, swap_dashes

CHANGELOG = Path(__file__).with_name("wc_prediction_fixes_applied.md")

# ── target bracket distribution (48 teams), by team slug ────────────────────
EXIT_ROUNDS: dict[str, str] = {
    # Winners (1)
    "france": "Winners",
    # Runner-up (1)
    "argentina": "Runner-up",
    # Semi-finals (2)
    "spain": "Semi-finals",
    "england": "Semi-finals",
    # Quarter-finals (4)
    "brazil": "Quarter-finals",
    "germany": "Quarter-finals",
    "netherlands": "Quarter-finals",
    "portugal": "Quarter-finals",
    # Round of 16 (8)
    "belgium": "Round of 16",
    "croatia": "Round of 16",
    "colombia": "Round of 16",
    "mexico": "Round of 16",
    "uruguay": "Round of 16",
    "japan": "Round of 16",
    "turkiye": "Round of 16",
    "ecuador": "Round of 16",
    # Round of 32 (16)
    "austria": "Round of 32",
    "australia": "Round of 32",
    "egypt": "Round of 32",
    "norway": "Round of 32",
    "cote-divoire": "Round of 32",
    "paraguay": "Round of 32",
    "scotland": "Round of 32",
    "senegal": "Round of 32",
    "korea-republic": "Round of 32",
    "canada": "Round of 32",
    "sweden": "Round of 32",
    "algeria": "Round of 32",
    "iran": "Round of 32",
    "czechia": "Round of 32",
    "tunisia": "Round of 32",
    "dr-congo": "Round of 32",
    # Group Stage (16) - the remaining weakest sides
    "south-africa": "Group Stage",
    "bosnia-and-herzegovina": "Group Stage",
    "qatar": "Group Stage",
    "switzerland": "Group Stage",
    "morocco": "Group Stage",
    "haiti": "Group Stage",
    "united-states": "Group Stage",
    "curacao": "Group Stage",
    "new-zealand": "Group Stage",
    "cabo-verde": "Group Stage",
    "saudi-arabia": "Group Stage",
    "iraq": "Group Stage",
    "jordan": "Group Stage",
    "uzbekistan": "Group Stage",
    "ghana": "Group Stage",
    "panama": "Group Stage",
}


# ── odds conversion ─────────────────────────────────────────────────────────

def to_decimal(v: str | None) -> tuple[str | None, bool]:
    """Return (value, changed). Converts American/fractional odds to decimal."""
    if v is None:
        return None, False
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return s, False  # already decimal
    m = re.fullmatch(r"([+-])(\d+)", s)  # American
    if m:
        n = int(m.group(2))
        dec = n / 100 + 1 if m.group(1) == "+" else 100 / n + 1
        return f"{dec:.2f}", True
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", s)  # fractional
    if m:
        dec = int(m.group(1)) / int(m.group(2)) + 1
        return f"{dec:.2f}", True
    return s, False  # unrecognised, leave as-is


# ── rationale reconciliation (Claude, targeted) ─────────────────────────────

STAGE_HINTS = [
    "final", "semi", "quarter", "round of", "last 16", "last 8", "knockout",
    "group stage", "out of the group", "escape the group", "win the tournament",
    "lift", "champion", "winners", "runner-up", "go all the way", "deep run",
    "go far", "go deep", "progress", "advance", "exit", "eliminat",
]


def needs_review(rationale: str) -> bool:
    low = (rationale or "").lower()
    return any(h in low for h in STAGE_HINTS)


def reconcile_prompt(name: str, old: str, new: str, text: str) -> str:
    return f"""\
A SavvyPlays tournament prediction has been revised. {name}'s predicted exit round
changed from "{old}" to "{new}". Below is their prediction rationale, written when
the call was "{old}". Rewrite ONLY the sentence(s) whose claim now contradicts a
"{new}" finish (for example, references to reaching a stage beyond {new}, winning
the tournament when they no longer do, or going out in the group when they now
progress further). Keep every other sentence exactly as written. Preserve the
facts, the confident Australian analyst tone and the length. If nothing in the
text contradicts a "{new}" finish, return the text completely unchanged.

RATIONALE:
\"\"\"
{text}
\"\"\"

Return ONE JSON object only, no markdown fences:
{{"text": "<the full rationale, only contradicting sentences changed>"}}"""


def reconcile(client, name: str, old: str, new: str, text: str) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=CONTENT_RULES,
        messages=[{"role": "user", "content": reconcile_prompt(name, old, new, text)}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    out = _extract_json(raw).get("text")
    if not isinstance(out, str) or not out.strip():
        raise ValueError("model returned no 'text'")
    return swap_dashes(out.strip())


# ── helpers ─────────────────────────────────────────────────────────────────

def one(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def fetch_predictions(sb) -> list[dict]:
    rows = (
        sb.table("wc_predictions")
        .select("id,team_id,predicted_exit_round,tournament_winner_odds,"
                "group_winner_odds,prediction_rationale,team:wc_teams(name,slug)")
        .execute()
        .data
    )
    out = []
    for r in rows:
        t = one(r.get("team")) or {}
        out.append({**r, "name": t.get("name", "?"), "slug": t.get("slug", "")})
    return out


def write_changelog(exit_changes, odds_changes, rationale_changes) -> None:
    lines = [
        "# World Cup 2026 prediction fixes applied",
        "",
        f"Run: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        f"## Exit rounds ({len(exit_changes)} changed)",
        "",
        "| Team | Old | New |",
        "| --- | --- | --- |",
        *[f"| {c['name']} | {c['old']} | {c['new']} |" for c in exit_changes],
        "",
        f"## Odds converted to decimal ({len(odds_changes)})",
        "",
        "| Team | Field | Old | New |",
        "| --- | --- | --- | --- |",
        *[f"| {c['name']} | {c['field']} | {c['old']} | {c['new']} |" for c in odds_changes],
        "",
        f"## Rationale rewrites ({len(rationale_changes)})",
        "",
    ]
    for c in rationale_changes:
        lines += [
            f"### {c['name']} ({c['old']} -> {c['new']})",
            "",
            f"**Before:** {c['before']}",
            "",
            f"**After:** {c['after']}",
            "",
        ]
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nChange log written to {CHANGELOG}")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; no API calls, no writes")
    ap.add_argument("--yes", action="store_true", help="apply without prompting")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sb = get_supabase()
    preds = fetch_predictions(sb)

    exit_changes, odds_changes, rationale_candidates = [], [], []
    unknown = []

    for p in preds:
        slug = p["slug"]
        new_round = EXIT_ROUNDS.get(slug)
        if not new_round:
            unknown.append(p["name"])
            continue
        old_round = p.get("predicted_exit_round")
        if new_round != old_round:
            exit_changes.append({**p, "old": old_round, "new": new_round})
            if p.get("prediction_rationale") and needs_review(p["prediction_rationale"]):
                rationale_candidates.append({**p, "old": old_round, "new": new_round})

        for field in ("tournament_winner_odds", "group_winner_odds"):
            new_val, changed = to_decimal(p.get(field))
            if changed:
                odds_changes.append({**p, "field": field, "old": p.get(field), "new": new_val})

    print(f"\nScanned {len(preds)} predictions.")
    if unknown:
        print(f"  ! no target round for: {', '.join(unknown)}")

    print(f"\nEXIT ROUND CHANGES ({len(exit_changes)}):")
    for c in sorted(exit_changes, key=lambda c: c["new"]):
        print(f"  {c['name']:<26} {str(c['old']):<14} -> {c['new']}")

    print(f"\nODDS CONVERSIONS ({len(odds_changes)}):")
    for c in odds_changes:
        print(f"  {c['name']:<26} {c['field']:<22} {c['old']} -> {c['new']}")

    print(f"\nRATIONALES TO REVIEW ({len(rationale_candidates)} of {len(exit_changes)} "
          f"exit changes mention a stage):")
    for c in rationale_candidates:
        print(f"  {c['name']} ({c['old']} -> {c['new']})")

    if args.dry_run:
        print("\n--dry-run: no API calls, nothing written.")
        return

    if not args.yes:
        resp = input(f"\nApply {len(exit_changes)} exit + {len(odds_changes)} odds changes, "
                     f"and reconcile {len(rationale_candidates)} rationale(s) via Claude? [y/N] "
                     ).strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted. No changes written.")
            return

    # 1+2: apply deterministic exit + odds updates per prediction row
    updates: dict[str, dict] = {}
    for c in exit_changes:
        updates.setdefault(c["id"], {})["predicted_exit_round"] = c["new"]
    for c in odds_changes:
        updates.setdefault(c["id"], {})[c["field"]] = c["new"]
    for pid, patch in updates.items():
        sb.table("wc_predictions").update(patch).eq("id", pid).execute()
    print(f"\nApplied exit/odds updates to {len(updates)} row(s).")

    # 3: reconcile rationales via Claude
    client = get_anthropic()
    rationale_changes = []
    for c in rationale_candidates:
        before = c["prediction_rationale"]
        try:
            after = reconcile(client, c["name"], c["old"], c["new"], before)
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {c['name']}: {e}")
            continue
        if after.strip() == before.strip():
            print(f"  [no change] {c['name']}")
            continue
        sb.table("wc_predictions").update(
            {"prediction_rationale": after}).eq("id", c["id"]).execute()
        rationale_changes.append({**c, "before": before, "after": after})
        print(f"  [rewritten] {c['name']}")

    print(f"\nDone. {len(exit_changes)} exit, {len(odds_changes)} odds, "
          f"{len(rationale_changes)} rationale change(s).")
    write_changelog(exit_changes, odds_changes, rationale_changes)


if __name__ == "__main__":
    main()
