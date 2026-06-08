"""
wc_lint_fix.py
Find and fix content-rule violations in the World Cup data with minimal,
targeted sentence rewrites.

For every text field in Supabase (wc_teams.overview/strengths/weaknesses,
wc_players.description, wc_groups.overview, wc_predictions.prediction_rationale)
it runs wc_lib.lint_content(). Any field that fails (banned word/phrase, em dash,
consecutive sentences starting with the same word) is sent to Claude with the
FULL field text plus a precise list of which sentences failed and why, and the
model is asked to rewrite ONLY those sentences, preserving meaning and tone.

The rewrite then has swap_dashes() applied and is re-linted; it is only written
back if it now passes. Em-dash-only failures are fixed by swap_dashes alone, with
no API call.

Much cheaper than a populate run: it edits individual sentences, it does not
regenerate whole profiles, and it only calls the API for fields that fail lint.

Usage:
    python wc_lint_fix.py             # review, confirm, fix + write
    python wc_lint_fix.py --dry-run   # list failing fields only (no API, no writes)
    python wc_lint_fix.py --yes       # skip the prompt (unattended)
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from wc_lib import (
    BANNED_WORDS,
    CONTENT_RULES,
    MODEL,
    get_anthropic,
    get_supabase,
    lint_content,
)
from wc_populate import _extract_json, swap_dashes

CHANGELOG = Path(__file__).with_name("wc_lint_fixes_applied.md")
MAX_ATTEMPTS = 3


# ── per-sentence diagnostics (mirrors wc_lib.lint_content) ──────────────────

def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def first_word(s: str) -> str:
    parts = s.split()
    return parts[0].lower().strip(",.;:") if parts else ""


def sentence_problems(text: str) -> list[tuple[int, str, list[str]]]:
    """Return [(idx, sentence, reasons)] for each offending sentence."""
    sents = split_sentences(text)
    probs: dict[int, list[str]] = {}

    for i, s in enumerate(sents):
        sl = s.lower()
        reasons: list[str] = []
        if "—" in s or "―" in s:
            reasons.append("em dash")
        for w in BANNED_WORDS:
            if " " in w:
                if w in sl:
                    reasons.append(f'banned phrase "{w}"')
            elif re.search(rf"\b{re.escape(w)}\b", sl):
                reasons.append(f'banned word "{w}"')
        if reasons:
            probs.setdefault(i, []).extend(reasons)

    for i in range(1, len(sents)):
        if first_word(sents[i]) and first_word(sents[i]) == first_word(sents[i - 1]):
            probs.setdefault(i, []).append(
                f'starts with the same word "{first_word(sents[i])}" as the previous sentence'
            )

    return [(i, sents[i], probs[i]) for i in sorted(probs)]


# ── Claude rewrite ──────────────────────────────────────────────────────────

def build_prompt(field_label: str, text: str, per: list[tuple[int, str, list[str]]]) -> str:
    listing = "\n".join(
        f'- "{sent}"\n  problem: {"; ".join(reasons)}' for _, sent, reasons in per
    ) or "(see overall flags below)"
    overall = "; ".join(lint_content(text)) or "(none)"
    return f"""\
This {field_label} field breaks our content rules. Rewrite ONLY the sentences
listed below, making the smallest change that fixes each problem. Leave every
other sentence exactly as written. Keep the facts, the meaning, and the confident
analyst tone intact.

FULL FIELD TEXT:
\"\"\"
{text}
\"\"\"

SENTENCES TO FIX:
{listing}

OVERALL LINT FLAGS: {overall}

When fixing: no em dashes; none of the banned words or phrases; and never start a
sentence with the same first word as the sentence immediately before or after it.
Keep "football" (never "soccer"). Return ONE JSON object only, no markdown fences:
{{"text": "<the complete field text, with only the listed sentences changed>"}}"""


def rewrite_once(client, field_label: str, text: str,
                 per: list[tuple[int, str, list[str]]]) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=CONTENT_RULES,
        messages=[{"role": "user", "content": build_prompt(field_label, text, per)}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = _extract_json(raw)
    out = data.get("text")
    if not isinstance(out, str) or not out.strip():
        raise ValueError("model returned no 'text'")
    return out.strip()


def fix_field(client, field_label: str, text: str) -> tuple[str, int, str | None]:
    """Return (new_text, api_calls, error). new_text is lint-clean unless error."""
    current = swap_dashes(text)  # em-dash-only failures resolve here, no API
    calls = 0
    while lint_content(current) and calls < MAX_ATTEMPTS:
        per = sentence_problems(current)
        try:
            out = rewrite_once(client, field_label, current, per)
        except Exception as e:  # noqa: BLE001
            return current, calls, f"error: {e}"
        calls += 1
        current = swap_dashes(out)

    if lint_content(current):
        return current, calls, f"still fails lint after {calls} rewrite(s)"
    return current, calls, None


# ── gather fields ───────────────────────────────────────────────────────────

def gather_fields(sb) -> list[dict]:
    fields: list[dict] = []

    teams = sb.table("wc_teams").select(
        "id,name,overview,strengths,weaknesses"
    ).execute().data
    name_by_id = {t["id"]: t["name"] for t in teams}
    for t in teams:
        for col in ("overview", "strengths", "weaknesses"):
            fields.append({
                "table": "wc_teams", "key_col": "id", "key_val": t["id"],
                "column": col, "field": col, "target": t["name"],
                "text": t.get(col) or "",
            })

    players = sb.table("wc_players").select("id,name,description,team_id").execute().data
    for p in players:
        fields.append({
            "table": "wc_players", "key_col": "id", "key_val": p["id"],
            "column": "description", "field": "description",
            "target": f'{name_by_id.get(p["team_id"], "?")} / {p["name"]}',
            "text": p.get("description") or "",
        })

    groups = sb.table("wc_groups").select("letter,overview").execute().data
    for g in groups:
        fields.append({
            "table": "wc_groups", "key_col": "letter", "key_val": g["letter"],
            "column": "overview", "field": "overview",
            "target": f'Group {g["letter"]}', "text": g.get("overview") or "",
        })

    preds = sb.table("wc_predictions").select(
        "id,team_id,prediction_rationale"
    ).execute().data
    for pr in preds:
        fields.append({
            "table": "wc_predictions", "key_col": "id", "key_val": pr["id"],
            "column": "prediction_rationale", "field": "prediction_rationale",
            "target": f'{name_by_id.get(pr["team_id"], "?")} (rationale)',
            "text": pr.get("prediction_rationale") or "",
        })

    return fields


# ── output ──────────────────────────────────────────────────────────────────

def print_table(rows: list[dict]) -> None:
    headers = ("Target", "Field", "Lint issues")
    data = [
        (r["target"], r["field"], "; ".join(r["issues"])[:70])
        for r in rows
    ]
    widths = [max(len(headers[i]), *(len(d[i]) for d in data)) for i in range(3)]

    def fmt(cells) -> str:
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for d in data:
        print(fmt(d))


def write_changelog(results: list[dict]) -> None:
    applied = [r for r in results if r["status"].startswith("applied")]
    lines = [
        "# World Cup 2026 lint fixes applied",
        "",
        f"Run: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Applied: {len(applied)} / {len(results)} failing field(s)",
        "",
        "| Target | Field | Before | API calls | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        before = "; ".join(r["issues"]).replace("|", "/")
        lines.append(
            f"| {r['target']} | {r['field']} | {before} | {r['calls']} | {r['status']} |"
        )
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nChange log written to {CHANGELOG}")


# ── main ─────────────────────────────────────────────────────────────────--

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="list failing fields only; no API calls, no writes")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (for unattended runs)")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sb = get_supabase()
    fields = gather_fields(sb)

    failing = []
    for f in fields:
        issues = lint_content(f["text"])
        if issues:
            failing.append({**f, "issues": issues})

    print(f"\nScanned {len(fields)} fields. {len(failing)} fail lint.\n")
    if not failing:
        print("All clean. Nothing to do.")
        return

    failing.sort(key=lambda r: (r["target"], r["field"]))
    print_table(failing)

    if args.dry_run:
        print("\n--dry-run: no API calls, nothing written.")
        return

    if not args.yes:
        resp = input(
            f"\nRewrite and apply fixes to these {len(failing)} field(s)? "
            "This calls the Claude API and writes to Supabase. [y/N] "
        ).strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted. No changes written.")
            return

    client = get_anthropic()
    results: list[dict] = []

    for f in failing:
        new_text, calls, err = fix_field(client, f["field"], f["text"])
        if err:
            status = f"FAILED: {err}"
        else:
            res = (
                sb.table(f["table"])
                .update({f["column"]: new_text})
                .eq(f["key_col"], f["key_val"])
                .execute()
            )
            if not res.data:
                status = "FAILED: no matching row"
            elif calls == 0:
                status = "applied (dash swap, no API)"
            else:
                status = f"applied ({calls} rewrite call(s))"
        print(f"  [{status}] {f['target']} / {f['field']}")
        results.append({**f, "calls": calls, "status": status})

    applied = sum(r["status"].startswith("applied") for r in results)
    print(f"\nDone. {applied}/{len(results)} field(s) fixed and written.")
    write_changelog(results)


if __name__ == "__main__":
    main()
