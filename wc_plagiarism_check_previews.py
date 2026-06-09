"""
wc_plagiarism_check_previews.py
Run the same plagiarism check as wc_plagiarism_check.py, but scoped to the match
preview prose in wc_match_previews (Matchday 1, matches 1-24). For each of the
fields below it takes the most distinctive sentences, web-searches for near-exact
matches, and reports a confidence score with a suggested rewrite for anything that
looks lifted.

Fields checked: match_overview, team_a_analysis, team_b_analysis, tactical_angle,
verdict.

Reuses the checking logic (distinctive_sentences, check_prompt) from
wc_plagiarism_check.py; only the data source differs.

Usage:
    python wc_plagiarism_check_previews.py
    python wc_plagiarism_check_previews.py --min-confidence 40
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wc_lib import get_anthropic, get_supabase
from wc_populate import ask_json
from wc_plagiarism_check import check_prompt, distinctive_sentences

REPORT = Path(__file__).with_name("wc_plagiarism_report_previews.md")

FIELDS = ["match_overview", "team_a_analysis", "team_b_analysis", "tactical_angle", "verdict"]


def one(v):
    return (v[0] if isinstance(v, list) else v) or {}


def collect_preview_blocks(sb) -> list[dict]:
    rows = (
        sb.table("wc_matches")
        .select("match_number,team_a:wc_teams!team_a_id(name),team_b:wc_teams!team_b_id(name),"
                "preview:wc_match_previews(" + ",".join(FIELDS) + ")")
        .lte("match_number", 24)
        .order("match_number")
        .execute()
        .data
    )
    blocks: list[dict] = []
    for r in rows:
        p = one(r.get("preview"))
        if not p:
            continue
        a = one(r.get("team_a")).get("name", "?")
        b = one(r.get("team_b")).get("name", "?")
        for f in FIELDS:
            if p.get(f):
                blocks.append({"label": f"M{r['match_number']} {a} v {b} / {f}", "text": p[f]})
    return blocks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-confidence", type=int, default=40,
                    help="flag threshold for the report summary")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sb = get_supabase()
    client = get_anthropic()

    blocks = collect_preview_blocks(sb)
    if not blocks:
        print("No preview content found. Run wc_generate_previews.py first.")
        return

    out = ["# World Cup 2026 plagiarism report (match previews)", ""]
    flagged = 0
    print(f"Checking {len(blocks)} preview content blocks ...\n")

    for b in blocks:
        sents = distinctive_sentences(b["text"])
        if not sents:
            continue
        print(f"  {b['label']} ...", flush=True)
        try:
            r = ask_json(client, check_prompt(b["label"], sents), max_tokens=2000)
        except Exception as e:  # noqa: BLE001
            out += [f"### {b['label']}", f"- ERROR: {e}", ""]
            continue
        conf = int(r.get("confidence", 0))
        if conf >= args.min_confidence:
            flagged += 1
            out.append(f"### {b['label']} — confidence {conf}")
            for m in r.get("matches", []):
                out.append(f"- match: \"{m.get('sentence', '')[:90]}\" -> "
                           f"{m.get('url', 'n/a')} ({m.get('note', '')})")
            if r.get("suggested_rewrite"):
                out.append(f"- suggested rewrite: {r['suggested_rewrite']}")
            out.append("")
            print(f"    FLAGGED ({conf})")
        else:
            print(f"    ok ({conf})")

    out.insert(2, f"Blocks checked: {len(blocks)} | flagged (>= {args.min_confidence}): {flagged}")
    out.insert(3, "")
    REPORT.write_text("\n".join(out), encoding="utf-8")
    print(f"\nReport written to {REPORT}")


if __name__ == "__main__":
    main()
