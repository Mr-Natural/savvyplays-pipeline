"""
wc_plagiarism_check.py
Check the generated World Cup prose for substantial overlap with already-published
material. For each text block it takes the most distinctive sentences, searches the
web for near-exact phrase matches, and reports a confidence score with a suggested
rewrite for anything that looks lifted.

Usage:
    python wc_plagiarism_check.py            # all content -> wc_plagiarism_report.md
    python wc_plagiarism_check.py --group D
    python wc_plagiarism_check.py --min-confidence 40
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from wc_lib import GROUPS, get_anthropic, get_supabase
from wc_populate import ask_json

REPORT = Path(__file__).with_name("wc_plagiarism_report.md")


def distinctive_sentences(text: str, n: int = 3) -> list[str]:
    """Pick the n longest sentences (most distinctive / least boilerplate)."""
    if not text:
        return []
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 8]
    sents.sort(key=lambda s: len(s), reverse=True)
    return sents[:n]


def collect_blocks(sb, letters: list[str]) -> list[dict]:
    """Each block: {label, text}."""
    blocks: list[dict] = []

    teams = (
        sb.table("wc_teams")
        .select("id,name,group_letter,overview,strengths,weaknesses")
        .in_("group_letter", letters)
        .execute()
        .data
    )
    team_ids = [t["id"] for t in teams]
    for t in teams:
        for field in ("overview", "strengths", "weaknesses"):
            if t.get(field):
                blocks.append({"label": f"{t['name']} / {field}", "text": t[field]})

    if team_ids:
        players = (
            sb.table("wc_players")
            .select("name,description,team_id")
            .in_("team_id", team_ids)
            .execute()
            .data
        )
        name_by_id = {t["id"]: t["name"] for t in teams}
        for p in players:
            if p.get("description"):
                blocks.append(
                    {"label": f"{name_by_id.get(p['team_id'], '?')} / player {p['name']}",
                     "text": p["description"]}
                )
        preds = (
            sb.table("wc_predictions")
            .select("team_id,prediction_rationale")
            .in_("team_id", team_ids)
            .execute()
            .data
        )
        for p in preds:
            if p.get("prediction_rationale"):
                blocks.append(
                    {"label": f"{name_by_id.get(p['team_id'], '?')} / rationale",
                     "text": p["prediction_rationale"]}
                )

    groups = (
        sb.table("wc_groups").select("letter,overview").in_("letter", letters).execute().data
    )
    for g in groups:
        if g.get("overview"):
            blocks.append({"label": f"Group {g['letter']} / overview", "text": g["overview"]})

    return blocks


def check_prompt(label: str, sentences: list[str]) -> str:
    quoted = "\n".join(f'  {i + 1}. "{s}"' for i, s in enumerate(sentences))
    return f"""\
Check whether the following sentences appear, verbatim or near-verbatim, in any
already-published material online. Search each as an exact phrase.

Source: {label}
{quoted}

Return ONE JSON object only:

{{
  "confidence": <0-100, likelihood this content is copied/derivative>,
  "matches": [
    {{"sentence": "<which sentence>", "url": "<source if found>", "note": "<what matched>"}}
  ],
  "suggested_rewrite": "<only if confidence >= 40, else empty string>"
}}

A high score means the phrasing clearly exists elsewhere. Common factual
statements that anyone would write similarly are NOT plagiarism; score those low."""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="single group letter A-L")
    ap.add_argument("--min-confidence", type=int, default=40,
                    help="flag threshold for the report summary")
    args = ap.parse_args()

    sb = get_supabase()
    client = get_anthropic()
    letters = [args.group.upper()] if args.group else list(GROUPS.keys())

    blocks = collect_blocks(sb, letters)
    if not blocks:
        print("No content found. Run wc_populate.py first.")
        return

    out = ["# World Cup 2026 plagiarism report", ""]
    flagged = 0
    print(f"Checking {len(blocks)} content blocks ...\n")

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
                out.append(f"- match: \"{m.get('sentence', '')[:90]}\" -> {m.get('url', 'n/a')} ({m.get('note', '')})")
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
