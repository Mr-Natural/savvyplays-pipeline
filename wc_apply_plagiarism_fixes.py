"""
wc_apply_plagiarism_fixes.py
Apply the "suggested rewrite" blocks from wc_plagiarism_report.md to Supabase.

This script does NO web/LLM calls. It reads the existing plagiarism report,
extracts every "- suggested rewrite:" block, works out which row and column it
belongs to from the section heading, prints them for review, asks for
confirmation, then writes to Supabase and saves a change log.

Section headings in the report look like:
    ### Germany / overview — confidence 72
    ### Brazil / player Vinícius Júnior — confidence 55
    ### Group L / overview — confidence 60

so each rewrite maps to:
    Team / overview|strengths|weaknesses  -> wc_teams.<field>   (match on slug)
    Team / player <name>                  -> wc_players.description (team + name)
    Group X / overview                    -> wc_groups.overview  (match on letter)

Safety:
  - Partial / instructional rewrites (e.g. "Sentence 3 rewrite: ...",
    "Sentences 1 and 2 need rewriting. For sentence 1, try: ...") are NOT whole
    field replacements, so they are flagged and SKIPPED for manual review.
  - Em dashes in a rewrite are swapped before writing (same rule as the
    populate script), so none reach the database.
  - Unknown teams / no matching row are reported, never silently dropped.

Usage:
    python wc_apply_plagiarism_fixes.py                  # review, confirm, write
    python wc_apply_plagiarism_fixes.py --dry-run        # review only, never writes
    python wc_apply_plagiarism_fixes.py --min-confidence 60  # only >=60
    python wc_apply_plagiarism_fixes.py --yes            # skip the prompt (unattended)
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from wc_lib import all_teams
from wc_populate import swap_dashes  # reuse the em-dash -> comma swap

REPORT = Path(__file__).with_name("wc_plagiarism_report.md")
CHANGELOG = Path(__file__).with_name("wc_plagiarism_fixes_applied.md")

NAME_TO_SLUG = {t["name"]: t["slug"] for t in all_teams()}

TEAM_FIELDS = {"overview", "strengths", "weaknesses"}

# "### <label> — confidence <n>" (accept em/en dash or hyphen as the separator)
HEADING_RE = re.compile(r"^###\s+(.*?)\s*[—–-]\s*confidence\s+(\d+)\s*$", re.I)
REWRITE_PREFIX = "- suggested rewrite:"

# A rewrite that is really per-sentence guidance, not a full replacement block.
PARTIAL_RE = re.compile(
    r"(^\s*sentences?\s+\d)"          # "Sentence 3 ...", "Sentences 1 and 2 ..."
    r"|(\bsentence\s+\d+\s*:)"        # "Sentence 2: ..."
    r"|(\bsentence\s+\d+\s+rewrite\b)"  # "Sentence 3 rewrite: ..."
    r"|(\bneeds?\s+rewriting\b)"
    r"|(\bfor\s+sentence\s+\d)"
    r"|(\brewrite\s+sentence\b)"
    r"|(\btry:\s*[\"'])",            # "... try: '...'"
    re.I,
)


# ── parsing ─────────────────────────────────────────────────────────────────

def build_item(label: str, conf: int, new_text: str) -> dict | None:
    """Map a report section to a concrete (table, column, key) target."""
    if " / " not in label:
        return None
    left, right = (p.strip() for p in label.split(" / ", 1))
    rl = right.lower()

    skip = "partial/manual rewrite" if PARTIAL_RE.search(new_text) else None
    base = {
        "label": label,
        "confidence": conf,
        "new_text": new_text,
        "skip_reason": skip,
    }

    if left.startswith("Group "):
        if rl != "overview":
            return None
        letter = left[len("Group "):].strip().upper()
        base.update(table="wc_groups", column="overview",
                    group_letter=letter, target=f"Group {letter}")
        return base

    if rl in TEAM_FIELDS:
        slug = NAME_TO_SLUG.get(left)
        base.update(table="wc_teams", column=rl, team_name=left,
                    team_slug=slug, target=left)
        if slug is None:
            base["skip_reason"] = base["skip_reason"] or f"unknown team: {left}"
        return base

    if rl.startswith("player "):
        player = right[len("player "):].strip()
        slug = NAME_TO_SLUG.get(left)
        base.update(table="wc_players", column="description", team_name=left,
                    team_slug=slug, player_name=player, target=f"{left} / {player}")
        if slug is None:
            base["skip_reason"] = base["skip_reason"] or f"unknown team: {left}"
        return base

    return None


def parse_rewrites(text: str) -> list[dict]:
    """Pull every suggested-rewrite block out of the report."""
    lines = text.splitlines()
    n = len(lines)
    items: list[dict] = []
    seen: set[tuple] = set()
    label: str | None = None
    conf = 0
    i = 0

    while i < n:
        line = lines[i]
        m = HEADING_RE.match(line)
        if m:
            label, conf = m.group(1).strip(), int(m.group(2))
            i += 1
            continue

        if label and line.startswith(REWRITE_PREFIX):
            parts = [line[len(REWRITE_PREFIX):].strip()]
            j = i + 1
            # a rewrite may wrap onto following prose lines until a blank line,
            # the next section, or the next bullet
            while j < n:
                nxt = lines[j]
                if not nxt.strip() or nxt.startswith("### ") or nxt.startswith("- "):
                    break
                parts.append(nxt.rstrip())
                j += 1
            new_text = "\n".join(parts).strip()
            i = j

            if not new_text:
                continue
            item = build_item(label, conf, new_text)
            if not item:
                continue
            key = (item.get("table"), item.get("target"), item["column"])
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            continue

        i += 1

    return items


# ── output ──────────────────────────────────────────────────────────────────

def status_preview(it: dict, min_conf: int) -> str:
    if it["skip_reason"]:
        return f"skip ({it['skip_reason']})"
    if it["confidence"] < min_conf:
        return f"skip (conf<{min_conf})"
    return "apply"


def print_table(items: list[dict], min_conf: int) -> None:
    headers = ("Target", "Field", "Conf", "Chars", "Plan")
    rows = [
        (
            it["target"],
            it["column"],
            str(it["confidence"]),
            str(len(it["new_text"])),
            status_preview(it, min_conf),
        )
        for it in items
    ]
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]

    def fmt(cells) -> str:
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


def write_changelog(results: list[dict], min_conf: int) -> None:
    applied = [r for r in results if r["status"] == "applied"]
    lines = [
        "# World Cup 2026 plagiarism fixes applied",
        "",
        f"Run: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Source: {REPORT.name}",
        f"Min confidence: {min_conf}",
        f"Applied: {len(applied)} / {len(results)} suggested rewrite(s)",
        "",
        "| Target | Field | Conf | Status |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r['target']} | {r['column']} | {r['confidence']} | {r['status']} |"
        )
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nChange log written to {CHANGELOG}")


# ── apply ─────────────────────────────────────────────────────────────────--

def apply_one(sb, slug_to_id: dict[str, str], it: dict) -> str:
    text = swap_dashes(it["new_text"])  # never let an em dash reach the DB
    table, col = it["table"], it["column"]

    if table == "wc_teams":
        res = sb.table("wc_teams").update({col: text}).eq("slug", it["team_slug"]).execute()
        return "applied" if res.data else "FAILED: no matching team row"

    if table == "wc_players":
        tid = slug_to_id.get(it["team_slug"])
        if not tid:
            return "FAILED: team slug not found"
        res = (
            sb.table("wc_players")
            .update({col: text})
            .eq("team_id", tid)
            .eq("name", it["player_name"])
            .execute()
        )
        return "applied" if res.data else "FAILED: no matching player row"

    if table == "wc_groups":
        res = sb.table("wc_groups").update({col: text}).eq("letter", it["group_letter"]).execute()
        return "applied" if res.data else "FAILED: no matching group row"

    return "FAILED: unknown table"


# ── main ─────────────────────────────────────────────────────────────────--

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the review table and exit without writing")
    ap.add_argument("--min-confidence", type=int, default=0,
                    help="only apply rewrites at or above this confidence (default 0)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (for unattended runs)")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not REPORT.exists():
        raise SystemExit(f"Report not found: {REPORT}")

    items = parse_rewrites(REPORT.read_text(encoding="utf-8"))
    items.sort(key=lambda it: (it["table"], it["target"], it["column"]))

    if not items:
        print("No suggested rewrites found in the report.")
        return

    applicable = [
        it for it in items
        if not it["skip_reason"] and it["confidence"] >= args.min_confidence
    ]
    skipped = len(items) - len(applicable)

    print(f"\nSuggested rewrites found: {len(items)} "
          f"({len(applicable)} to apply, {skipped} skipped)\n")
    print_table(items, args.min_confidence)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    if not applicable:
        print("\nNothing to apply.")
        return

    if not args.yes:
        resp = input(
            f"\nApply these {len(applicable)} rewrite(s) to Supabase? [y/N] "
        ).strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted. No changes written.")
            return

    from wc_lib import get_supabase

    sb = get_supabase()
    slug_to_id = {
        r["slug"]: r["id"]
        for r in sb.table("wc_teams").select("id,slug").execute().data
    }

    results: list[dict] = []
    for it in items:
        if it["skip_reason"]:
            status = f"skipped: {it['skip_reason']}"
        elif it["confidence"] < args.min_confidence:
            status = f"skipped: conf<{args.min_confidence}"
        else:
            status = apply_one(sb, slug_to_id, it)
        print(f"  [{status}] {it['target']} / {it['column']} (conf {it['confidence']})")
        results.append({**it, "status": status})

    applied = sum(r["status"] == "applied" for r in results)
    print(f"\nDone. {applied}/{len(results)} rewrite(s) applied.")
    write_changelog(results, args.min_confidence)


if __name__ == "__main__":
    main()
