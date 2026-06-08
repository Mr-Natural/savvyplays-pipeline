"""
wc_fix_from_report.py
Apply the concrete caps / goals / club corrections found in
wc_verification_report.md to the wc_players table in Supabase.

This script does NO web/LLM calls. It reads the existing verification report,
extracts every correction where the report gives a concrete actual value,
prints them for review, asks for confirmation, then writes to Supabase
(matching each player on name + team slug) and saves a change log.

Extraction rules (per the report's own conventions):
  - caps:  "stored 130, actual ~143-144"     -> set caps  to 143
  - goals: "stored 28, actual ~16"            -> set goals to 16
  - club:  "stored `Crystal Palace` vs web `Arsenal`" -> set club to Arsenal
  - approximate values ("~127")               -> use the number given
  - ranges ("143-144", "73-77")               -> use the PRIMARY (lower) figure
  - cosmetic club differences (e.g. Bayern Munchen vs Bayern Munich) are ignored
  - anything flagged "unverified", "plausible but unconfirmed", "could not be
    verified", "uncertain", etc. is ignored -- only concrete corrections apply.

Usage:
    python wc_fix_from_report.py            # review table, confirm, then write
    python wc_fix_from_report.py --dry-run  # review table only, never writes
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from wc_lib import all_teams

REPORT = Path(__file__).with_name("wc_verification_report.md")
CHANGELOG = Path(__file__).with_name("wc_fixes_applied.md")

NAME_TO_SLUG = {t["name"]: t["slug"] for t in all_teams()}


# ── text helpers ───────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Lower-case + strip accents, for accent-insensitive name matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def cosmetic_club(a: str, b: str) -> bool:
    """True if two club strings are the same club under a spelling/accent
    normalisation (e.g. 'Bayern Munchen' vs 'Bayern Munich') -- not a real move."""
    def n(s: str) -> str:
        s = re.sub(r"[^a-z0-9 ]", " ", _norm(s))
        s = s.replace("munchen", "munich")   # German/English city spelling
        return re.sub(r"\s+", " ", s).strip()
    return n(a) == n(b)


# A number token: optional ~, an integer, an optional range tail, optional +.
# Range/`+` are resolved by resolve_num(). En dash (U+2013) and hyphen are range
# separators; the em dash (U+2014) is a clause separator and is NOT matched here.
NUM_RE = re.compile(r"(~?)\s*(\d+)(?:\s*[–-]\s*(\d+))?\s*(\+?)")

MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"

# Words that mean "the number near here is the ACTUAL (new) value".
NEW_MARKERS = re.compile(
    r"\b(?:actual|verified|should\s+be|shows?|record(?:s|ed)?|lists?|cites?|"
    r"states?|indicates?|reports?|confirm(?:ed|s)?|now|gives?|says?|"
    r"references?|place[sd]?|approximately|current)\b",
    re.I,
)
# A "stored" anchor means "the number near here is the STORED (old) value".
OLD_MARKER = re.compile(r"\bstored\b", re.I)

# If any clause that mentions the field also contains one of these, the report
# did not actually confirm a concrete value -> skip that field.
SKIP_RE = re.compile(
    r"unverified|unconfirmed|could not|cannot|can't|not verifiable|unverifiable|"
    r"not independently|not corroborated|not be (?:confirmed|verified)|"
    r"not confirmed|plausible|uncertain|disputed|contested|ambig|manual review|"
    r"treat as|not directly verified",
    re.I,
)


def resolve_num(approx: str, lo: str, hi: str | None, plus: str) -> int:
    """Resolve a matched number token to a single int.

    Ranges resolve to the PRIMARY (lower) figure -- the report writes ranges
    ascending with the more authoritative source first ("~55-60" = 55 from
    Transfermarkt, 60 from a single source).
    """
    if hi:
        return min(int(lo), int(hi))
    return int(lo)  # '~' and trailing '+' are just dropped


def clean_line(s: str) -> str:
    """Strip date / source noise so only stored & actual values survive.

    Parentheticals are dropped unless they carry a value marker
    (stored / actual / not), in which case only the parens are removed.
    """
    # collapse parentheticals that are pure sourcing/date noise
    for _ in range(3):  # a few passes handle the (rare) nested case
        new = re.sub(
            r"\(([^()]*)\)",
            lambda m: m.group(1)
            if re.search(r"stored|actual|\bnot\b", m.group(1), re.I)
            else " ",
            s,
        )
        if new == s:
            break
        s = new

    s = re.sub(r"\bas of\b[^,;.:]*", " ", s, flags=re.I)       # "as of 6 June 2026"
    s = re.sub(r"\bper\b\s+[^,;.:()]*", " ", s, flags=re.I)     # "per Transfermarkt ..."
    s = re.sub(r"\b(?:19|20)\d{2}\s*[/–-]\s*\d{2}\b", " ", s)   # season "2025/26"
    # \b after the day digits stops "March 2026" being read as "March 20" + "26"
    s = re.sub(rf"\b{MONTH}\s+\d{{1,2}}\b(?:st|nd|rd|th)?,?\s*(?:(?:19|20)\d{{2}})?", " ", s)
    s = re.sub(rf"\b\d{{1,2}}\b(?:st|nd|rd|th)?\s+{MONTH},?\s*(?:(?:19|20)\d{{2}})?", " ", s)
    s = re.sub(rf"\b{MONTH},?\s*(?:19|20)\d{{2}}", " ", s)      # "June 2026"
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)                   # stray years
    s = re.sub(rf"\b{MONTH}\b", " ", s)                         # stray month names
    s = re.sub(r"\bby\s+~?\d+(?:\s*[–-]\s*\d+)?", " ", s, flags=re.I)  # "understated by 14"
    return re.sub(r"\s+", " ", s).strip()


def _field_of(clean: str, start: int, end: int, units: list[tuple[int, str]]
              ) -> tuple[str | None, bool]:
    """Return (field, adjacent) for a number, where adjacent means a unit word
    sits right after it (e.g. "35 caps") -- a strong, reliable signal."""
    am = re.match(r"[ ]{0,2}(caps?|goals?)\b", clean[end:end + 10], re.I)
    if am:
        return ("caps" if am.group(1).lower().startswith("cap") else "goals"), True
    before_units = [f for p, f in units if p < start]
    return (before_units[-1] if before_units else None), False


def scan_numeric(clean: str) -> dict[str, tuple[int, int]]:
    """Pull {field: (old, new)} from a cleaned bullet for caps and goals.

    Each "stored" / "actual"-type marker is bound to the single number it
    introduces ("stored as 12", "actual is 15") or, failing that, to a number
    directly in front of it ("53 stored, 54 actual"). '~' forces new and a
    preceding 'not ' forces old, covering "should be 76, not 75".
    """
    units = [
        (m.start(), "caps" if m.group(1).lower().startswith("cap") else "goals")
        for m in re.finditer(r"\b(caps?|goals?)\b", clean, re.I)
    ]
    nums = [(m.start(), m.end(), resolve_num(*m.groups()), m.group(1))
            for m in NUM_RE.finditer(clean)]
    roles: list[str | None] = [None] * len(nums)

    def num_starting_at(pos: int) -> int | None:
        for i, (s, e, _, _) in enumerate(nums):
            if s <= pos < e:
                return i
        return None

    def num_ending_before(pos: int) -> int | None:
        # only across pure whitespace, so "67 ; stored figure" doesn't bind 67
        for i, (s, e, _, _) in enumerate(nums):
            if e <= pos and clean[e:pos].strip() == "":
                return i
        return None

    markers = (
        [(m.start(), m.end(), "old") for m in OLD_MARKER.finditer(clean)]
        + [(m.start(), m.end(), "new") for m in NEW_MARKERS.finditer(clean)]
    )
    # filler words that may sit between a marker and its number
    # ("current figure is 198", "confirmed international goals are 2").
    right_re = re.compile(
        r"(?:\s+(?:as|is|are|at|of|to|=|figures?|international|senior|caps?|"
        r"goals?|the|in|currently|now|only|around|about|roughly))*"
        r"\s*:?\s*(~?\s*\d+)"
    )
    # pass 1: bind each marker to the number it directly introduces (right)
    unbound = []
    for ms, me, role in markers:
        rm = right_re.match(clean[me:me + 30])
        tgt = num_starting_at(me + rm.start(1)) if rm else None
        if tgt is not None and roles[tgt] is None:
            roles[tgt] = role
        else:
            unbound.append((ms, role))
    # pass 2: a marker with no number to its right describes the one before it
    for ms, role in unbound:
        tgt = num_ending_before(ms)
        if tgt is not None and roles[tgt] is None:
            roles[tgt] = role

    for i, (s, e, _, approx) in enumerate(nums):
        if re.search(r"\bnot\s*$", clean[max(0, s - 6):s], re.I):
            roles[i] = "old"
        elif approx == "~":
            roles[i] = "new"

    out: dict[str, tuple[int, int]] = {}
    for field in ("caps", "goals"):
        olds, news, spare = [], [], []
        for i, (s, e, val, _) in enumerate(nums):
            f, adjacent = _field_of(clean, s, e, units)
            if f != field:
                continue
            if roles[i] == "old":
                olds.append(val)
            elif roles[i] == "new":
                news.append(val)
            elif adjacent:               # unmarked but clearly this field
                spare.append(val)
        if not olds:
            continue
        if news:
            new = news[0]
        elif len(spare) == 1:            # "stored as 35 — wrong; had 47 caps"
            new = spare[0]
        else:
            continue
        if olds[0] != new:
            out[field] = (olds[0], new)
    return out


def field_skipped(clean: str, field: str) -> bool:
    """True if any clause mentioning this field is hedged as unverified."""
    for clause in re.split(r"[;.]", clean):
        if re.search(rf"\b{field[:-1]}s?\b", clause, re.I) and SKIP_RE.search(clause):
            return True
    return False


# ── report parsing ─────────────────────────────────────────────────────────

def split_sections(text: str) -> list[tuple[str, list[str]]]:
    """Return [(team_name, [lines]), ...] for each '### Team (Group X)' block."""
    sections: list[tuple[str, list[str]]] = []
    name: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^###\s+(.+?)\s+\(Group\s+[A-L]\)\s*$", line)
        if m:
            if name is not None:
                sections.append((name, buf))
            name, buf = m.group(1), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        sections.append((name, buf))
    return sections


def known_players(lines: list[str]) -> list[str]:
    names: list[str] = []
    for line in lines:
        m = re.match(r"^- Player (.+?):", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
        m = re.match(r"^- club mismatch (.+?): stored ", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def match_player(bullet: str, players: list[str]) -> str | None:
    """Find which known player a summary bullet refers to."""
    b = _norm(bullet)
    # 1) full-name substring, preferring the earliest / longest match
    hits = [(b.find(_norm(p)), -len(p), p) for p in players if _norm(p) in b]
    if hits:
        return min(hits)[2]
    # 2) fall back to a unique surname (last whitespace token)
    for p in players:
        surname = _norm(p.split()[-1])
        if len(surname) < 3:
            continue
        same = [q for q in players if _norm(q.split()[-1]) == surname]
        if len(same) == 1 and re.search(rf"\b{re.escape(surname)}\b", b):
            return p
    return None


def parse_corrections(text: str) -> list[dict]:
    corrections: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for team_name, lines in split_sections(text):
        slug = NAME_TO_SLUG.get(team_name)
        if not slug:
            print(f"  ! unknown team in report, skipping: {team_name!r}")
            continue
        players = known_players(lines)

        def add(player: str, field: str, old, new) -> None:
            key = (slug, player, field)
            if key in seen:
                return
            seen.add(key)
            corrections.append(
                {
                    "team_name": team_name,
                    "team_slug": slug,
                    "player_name": player,
                    "field": field,
                    "old_value": old,
                    "new_value": new,
                }
            )

        for line in lines:
            # structured club corrections
            m = re.match(r"^- club mismatch (.+?): stored `([^`]*)` vs web `([^`]*)`", line)
            if m:
                player, old_club, new_club = (s.strip() for s in m.groups())
                if (new_club and old_club != new_club
                        and not cosmetic_club(old_club, new_club)):
                    add(player, "club", old_club, new_club)
                continue

            if not line.startswith("- "):
                continue
            body = line[2:].strip()
            low = body.lower()
            if (
                body.startswith("Player ")
                or low.startswith(("recent form", "stored recent_form",
                                   "content rule", "club check", "club mismatch"))
                or not re.search(r"\b(caps?|goals?)\b", body, re.I)
            ):
                continue

            player = match_player(body, players)
            if not player:
                continue
            clean = clean_line(body)
            for field, (old, new) in scan_numeric(clean).items():
                if not field_skipped(clean, field):
                    add(player, field, old, new)

    return corrections


# ── output ─────────────────────────────────────────────────────────────────

def print_table(rows: list[dict]) -> None:
    headers = ("Team", "Player", "Field", "Old", "New")
    data = [
        (r["team_name"], r["player_name"], r["field"],
         str(r["old_value"]), str(r["new_value"]))
        for r in rows
    ]
    widths = [max(len(headers[i]), *(len(d[i]) for d in data)) for i in range(5)]

    def fmt(cells: tuple[str, ...]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for d in data:
        print(fmt(d))


def write_changelog(results: list[dict]) -> None:
    applied = [r for r in results if r["status"] == "applied"]
    lines = [
        "# World Cup 2026 fixes applied",
        "",
        f"Run: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Source: {REPORT.name}",
        f"Applied: {len(applied)} / {len(results)} proposed change(s)",
        "",
        "| Team | Player | Field | Old | New | Status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r['team_name']} | {r['player_name']} | {r['field']} | "
            f"{r['old_value']} | {r['new_value']} | {r['status']} |"
        )
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nChange log written to {CHANGELOG}")


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the review table and exit without writing")
    args = ap.parse_args()

    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not REPORT.exists():
        raise SystemExit(f"Report not found: {REPORT}")

    corrections = parse_corrections(REPORT.read_text(encoding="utf-8"))
    corrections.sort(key=lambda r: (r["team_name"], r["player_name"], r["field"]))

    if not corrections:
        print("No concrete corrections found in the report.")
        return

    print(f"\nProposed changes ({len(corrections)}):\n")
    print_table(corrections)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    resp = input(
        f"\nApply these {len(corrections)} change(s) to Supabase wc_players? [y/N] "
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
    for c in corrections:
        tid = slug_to_id.get(c["team_slug"])
        if not tid:
            status = "FAILED: team slug not found"
        else:
            res = (
                sb.table("wc_players")
                .update({c["field"]: c["new_value"]})
                .eq("team_id", tid)
                .eq("name", c["player_name"])
                .execute()
            )
            status = "applied" if res.data else "FAILED: no matching player row"
        print(f"  [{status}] {c['team_name']} / {c['player_name']} "
              f"{c['field']}: {c['old_value']} -> {c['new_value']}")
        results.append({**c, "status": status})

    applied = sum(r["status"] == "applied" for r in results)
    print(f"\nDone. {applied}/{len(results)} change(s) applied.")
    write_changelog(results)


if __name__ == "__main__":
    main()
