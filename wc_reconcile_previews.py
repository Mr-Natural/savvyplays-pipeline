"""
wc_reconcile_previews.py
After the scoreline/pick changes (wc_fix_scorelines.py), the narrative of the
result-changed previews can contradict the new call. This does a cheap, targeted
rewrite: for each affected match it sends the headline, subheadline, overview,
team analyses, tactical angle and verdict to Claude with the NEW scoreline + pick,
and asks it to rewrite ONLY the sentences that contradict them. Everything else is
left byte-for-byte. swap_dashes is applied and changes are logged.

Source of truth is the already-updated scoreline_prediction + savvyplays_pick in
the DB, so this never invents a result.

Usage:
    python wc_reconcile_previews.py --dry-run
    python wc_reconcile_previews.py            # confirm, then apply
    python wc_reconcile_previews.py --yes
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from wc_lib import CONTENT_RULES, MODEL, get_anthropic, get_supabase, lint_content
from wc_populate import _extract_json, swap_dashes

CHANGELOG = Path(__file__).with_name("wc_reconcile_fixes_applied.md")

# Matches whose predicted RESULT changed (M9 4-0 and M14 3-0 were unchanged).
RECONCILE = [2, 6, 7, 10, 11, 13, 15, 19, 21, 22, 24]

FIELDS = [
    "headline", "subheadline", "match_overview",
    "team_a_analysis", "team_b_analysis", "tactical_angle", "verdict",
]


def one(v):
    return (v[0] if isinstance(v, list) else v) or {}


def reconcile_prompt(a: str, b: str, scoreline: str, pick: str, fields: dict) -> str:
    return f"""\
The predicted result for this 2026 FIFA World Cup fixture has been revised, and the
preview below was written for a DIFFERENT result, so parts of it now contradict the
new call. Match: {a} vs {b}. NEW predicted scoreline: "{scoreline}". NEW betting
pick: "{pick}".

Rewrite ONLY the sentences whose claim contradicts the new scoreline or pick. For
example: naming a different winner, predicting a clean sheet when both teams now
score, calling a low-scoring game when the pick is now an over, or describing a
comfortable win when the result is now a draw or an upset. Make the smallest change
that removes each contradiction. Leave every other sentence exactly as written.
Preserve the facts, the confident Australian analyst tone and the length. Keep
"football" (never "soccer"); no em dashes; none of the banned words.

CURRENT FIELDS (JSON):
{json.dumps(fields, ensure_ascii=False, indent=2)}

Return ONE JSON object only, no markdown fences, with these exact keys, each value
the full field text with only contradicting sentences changed (return any field
that needs no change exactly as given):
{{{", ".join(f'"{k}": "..."' for k in FIELDS)}}}"""


def reconcile(client, a, b, scoreline, pick, fields) -> dict:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=CONTENT_RULES,
        messages=[{"role": "user", "content": reconcile_prompt(a, b, scoreline, pick, fields)}],
    )
    raw = "".join(blk.text for blk in msg.content if getattr(blk, "type", "") == "text")
    return _extract_json(raw)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sb = get_supabase()

    sel = ("match_number,team_a:wc_teams!team_a_id(name),team_b:wc_teams!team_b_id(name),"
           "preview:wc_match_previews(id," + ",".join(FIELDS) +
           ",scoreline_prediction,betting_preview)")
    rows = (sb.table("wc_matches").select(sel)
            .in_("match_number", RECONCILE).order("match_number").execute().data)

    targets = []
    for r in rows:
        p = one(r.get("preview"))
        if not p:
            continue
        targets.append({
            "num": r["match_number"],
            "a": one(r.get("team_a")).get("name", "?"),
            "b": one(r.get("team_b")).get("name", "?"),
            "preview": p,
        })

    print(f"\nWill reconcile {len(targets)} previews against their new result + pick:")
    for t in targets:
        p = t["preview"]
        print(f"  M{t['num']:>2}  {p['scoreline_prediction']:<30}  "
              f"pick: {(p.get('betting_preview') or {}).get('savvyplays_pick')}")

    if args.dry_run:
        print("\n--dry-run: no API calls, nothing written.")
        return
    if not args.yes:
        resp = input(f"\nReconcile narrative for {len(targets)} previews via Claude "
                     "and write changes? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return

    client = get_anthropic()
    log = []
    for t in targets:
        p = t["preview"]
        scoreline = p["scoreline_prediction"]
        pick = (p.get("betting_preview") or {}).get("savvyplays_pick", "")
        fields = {k: p.get(k) or "" for k in FIELDS}
        try:
            out = reconcile(client, t["a"], t["b"], scoreline, pick, fields)
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] M{t['num']}: {e}")
            continue

        patch, changed_fields = {}, []
        for k in FIELDS:
            new = out.get(k)
            if not isinstance(new, str) or not new.strip():
                continue
            new = swap_dashes(new.strip())
            if new != (fields[k] or "").strip():
                patch[k] = new
                changed_fields.append(k)
                lint = lint_content(new)
                banned = [i for i in lint if "banned" in i]
                log.append({"num": t["num"], "field": k, "before": fields[k],
                            "after": new, "banned": banned})

        if patch:
            sb.table("wc_match_previews").update(patch).eq("id", p["id"]).execute()
            print(f"  [updated] M{t['num']}: {', '.join(changed_fields)}")
        else:
            print(f"  [no change] M{t['num']}")

    # changelog
    lines = ["# World Cup 2026 preview reconciliation", "",
             f"Run: {datetime.now():%Y-%m-%d %H:%M:%S}",
             f"Fields rewritten: {len(log)}", ""]
    for c in log:
        lines += [f"### M{c['num']} - {c['field']}"
                  + (f"  ⚠ {c['banned']}" if c['banned'] else ""), "",
                  f"**Before:** {c['before']}", "", f"**After:** {c['after']}", ""]
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(log)} field(s) rewritten. Change log: {CHANGELOG}")


if __name__ == "__main__":
    main()
