"""
wc_fix_preview_plagiarism.py
De-risk the 8 preview fields flagged by wc_plagiarism_check_previews.py and fix the
factual discrepancies it surfaced. Rather than overwrite each field with the
report's (sometimes partial) suggested rewrite, this does a targeted Claude rewrite
per field: rewrite entirely in original words so no phrasing overlaps published
sources, apply the listed factual corrections, and preserve the full length, facts
and analyst voice. swap_dashes + lint are applied; changes are logged.

Usage:
    python wc_fix_preview_plagiarism.py --dry-run
    python wc_fix_preview_plagiarism.py            # confirm, then apply
    python wc_fix_preview_plagiarism.py --yes
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from wc_lib import CONTENT_RULES, MODEL, get_anthropic, get_supabase, lint_content
from wc_populate import _extract_json, swap_dashes

CHANGELOG = Path(__file__).with_name("wc_preview_plagiarism_fixes_applied.md")

# Flagged field -> published phrasings to avoid + factual corrections to apply.
FIXES = [
    {"num": 1, "field": "team_a_analysis",
     "phrases": ["'5-1 demolition of Serbia in Toluca'",
                 "the 'Lira starting ahead of Álvarez' framing used by SI/Goal/CBS"],
     "facts": ["Do not state a specific scoreline for the March Belgium friendly or "
               "claim it was played 'at this very stadium'; that venue and scoreline "
               "are unverified. Refer to it simply as a draw with Belgium in March."]},
    {"num": 3, "field": "match_overview",
     "phrases": ["'surgically repaired tibia' (lifted from TSN)"],
     "facts": ["Alphonso Davies suffered a hamstring injury/strain, not a torn "
               "hamstring; do not write that he 'tore' it.",
               "Moïse Bombito played about 30 minutes in a closed-door scrimmage, "
               "not merely 'warm-up minutes'."]},
    {"num": 13, "field": "team_a_analysis",
     "phrases": ["'hold the double pivot' (worldcuppass.com)",
                 "Reuters wire on Lukaku ('25 minutes off the bench', 'grabbed an "
                 "assist', 'second international appearance in a year')",
                 "'a season at Napoli wrecked by hamstring/muscle problems'"],
     "facts": []},
    {"num": 16, "field": "team_a_analysis",
     "phrases": ["'10 goals in AFC qualifying' phrasing from worldcupwiki.com"],
     "facts": []},
    {"num": 16, "field": "team_b_analysis",
     "phrases": ["'won just once in their last 10 games' (Yahoo/NBC)",
                 "'17 matches against European opposition' (101greatgoals)",
                 "the 'xG 1.49 to 0.12 at Raymond James Stadium' construction"],
     "facts": ["Liberato Cacace plays as a left-back, not a left wing-back."]},
    {"num": 17, "field": "team_a_analysis",
     "phrases": ["Fox Sports framing of Saliba's post-tournament surgery and 'all 26 "
                 "players available'",
                 "the Dembélé fact-sequence (back-to-back UCL, missed Côte d'Ivoire, "
                 "returns vs Northern Ireland) as ordered by Fox Sports/Goal"],
     "facts": []},
    {"num": 20, "field": "team_b_analysis",
     "phrases": ["'seven goals and 11 assists in 36 ...' stat chain (Asianet)",
                 "'returns after nearly a year out with injury'"],
     "facts": ["Ehsan Haddad has around 75 senior caps, not 91."]},
    {"num": 21, "field": "team_b_analysis",
     "phrases": ["'in the job since August 2022 after ... eight African countries'",
                 "'defensive structure and fast vertical transitions' (worldcuppass)",
                 "'one goal short of the all-time DR Congo record' (worldcuppass/Yahoo)"],
     "facts": ["Chancel Mbemba's winner against Cameroon was a stoppage-time goal, "
               "not specifically a header."]},
]


def one(v):
    return (v[0] if isinstance(v, list) else v) or {}


def prompt(a: str, b: str, field: str, phrases: list[str], facts: list[str], text: str) -> str:
    ph = "\n".join(f"  - {p}" for p in phrases) or "  - (general published match-report phrasing)"
    fx = "\n".join(f"  - {f}" for f in facts) or "  - (none)"
    return f"""\
This is the {field} field from the SavvyPlays preview of {a} vs {b}. A plagiarism
check found it reads as derivative of published online sources. Rewrite it ENTIRELY
IN YOUR OWN WORDS so that no sentence overlaps published match reports or squad
previews, while keeping every underlying FACT, the full length and analytical
depth, and the confident Australian analyst voice.

Avoid echoing these published phrasings in particular:
{ph}

Apply these factual corrections (the current text is wrong on these points):
{fx}

CURRENT TEXT:
\"\"\"
{text}
\"\"\"

Follow every content rule (no em dashes, no banned words, vary sentence openings).
Return ONE JSON object only, no markdown fences: {{"text": "<the full rewritten field>"}}"""


def rewrite(client, a, b, field, phrases, facts, text) -> str:
    msg = client.messages.create(
        model=MODEL, max_tokens=2500, system=CONTENT_RULES,
        messages=[{"role": "user", "content": prompt(a, b, field, phrases, facts, text)}],
    )
    raw = "".join(blk.text for blk in msg.content if getattr(blk, "type", "") == "text")
    out = _extract_json(raw).get("text")
    if not isinstance(out, str) or not out.strip():
        raise ValueError("model returned no 'text'")
    return swap_dashes(out.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sb = get_supabase()

    nums = sorted({f["num"] for f in FIXES})
    rows = (sb.table("wc_matches")
            .select("match_number,team_a:wc_teams!team_a_id(name),team_b:wc_teams!team_b_id(name),"
                    "preview:wc_match_previews(id,match_overview,team_a_analysis,team_b_analysis)")
            .in_("match_number", nums).execute().data)
    by_num = {r["match_number"]: r for r in rows}

    print(f"\nWill rewrite {len(FIXES)} flagged field(s):")
    for f in FIXES:
        r = by_num.get(f["num"], {})
        a = one(r.get("team_a")).get("name", "?")
        b = one(r.get("team_b")).get("name", "?")
        print(f"  M{f['num']:>2} {a} v {b} / {f['field']}  "
              f"({len(f['facts'])} fact fix{'es' if len(f['facts']) != 1 else ''})")

    if args.dry_run:
        print("\n--dry-run: no API calls, nothing written.")
        return
    if not args.yes:
        if input(f"\nRewrite {len(FIXES)} fields via Claude and write? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return

    client = get_anthropic()
    log = []
    for f in FIXES:
        r = by_num.get(f["num"])
        p = one(r.get("preview")) if r else {}
        if not p:
            print(f"  [SKIP] M{f['num']}: no preview")
            continue
        a = one(r.get("team_a")).get("name", "?")
        b = one(r.get("team_b")).get("name", "?")
        before = p.get(f["field"]) or ""
        try:
            after = rewrite(client, a, b, f["field"], f["phrases"], f["facts"], before)
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] M{f['num']} {f['field']}: {e}")
            continue
        sb.table("wc_match_previews").update({f["field"]: after}).eq("id", p["id"]).execute()
        banned = [i for i in lint_content(after) if "banned" in i]
        flag = f"  ⚠ {banned}" if banned else ""
        print(f"  [rewritten] M{f['num']} {f['field']}{flag}")
        log.append({"num": f["num"], "team": f"{a} v {b}", "field": f["field"],
                    "facts": f["facts"], "before": before, "after": after, "banned": banned})

    lines = ["# World Cup 2026 preview plagiarism + fact fixes", "",
             f"Run: {datetime.now():%Y-%m-%d %H:%M:%S}",
             f"Fields rewritten: {len(log)}", ""]
    for c in log:
        lines += [f"### M{c['num']} {c['team']} / {c['field']}"
                  + (f"  ⚠ {c['banned']}" if c["banned"] else ""), "",
                  "Fact fixes: " + ("; ".join(c["facts"]) if c["facts"] else "none"), "",
                  f"**Before:** {c['before']}", "", f"**After:** {c['after']}", ""]
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(log)} field(s) rewritten. Change log: {CHANGELOG}")


if __name__ == "__main__":
    main()
