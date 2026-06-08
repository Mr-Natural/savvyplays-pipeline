"""
wc_populate_matches.py
Seed the wc_matches table with the 2026 World Cup fixtures.

  - Matchday 1 (matches 1-24) is hardcoded from the confirmed FIFA schedule.
  - Matchdays 2-3 (matches 25-72) are sourced via Claude web search at run time,
    because exact kickoff times can still shift.
  - Knockout rounds (matches 73-104) are seeded as placeholder rows with no team
    IDs; fill them in later with --knockout-update as the bracket is confirmed.

All kickoff times are stored in UTC. Group-stage matchups, slugs and group
letters are derived from wc_lib so they always match the populated wc_teams.

Usage:
    python wc_populate_matches.py                 # MD1 + MD2/3 (web) + knockouts
    python wc_populate_matches.py --no-web        # MD1 + knockouts only (offline)
    python wc_populate_matches.py --dry-run       # print, write nothing
    python wc_populate_matches.py --knockout-update  # fill knockout teams (web)

Env (see .env.example): SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from wc_lib import all_teams, get_anthropic, get_supabase

# wc_populate already owns the web-search JSON helper used across the pipeline.
from wc_populate import ask_json

ET = timezone(timedelta(hours=-4))   # US Eastern in June (EDT). Spec times are ET.

NAME_TO_SLUG = {t["name"]: t["slug"] for t in all_teams()}
SLUG_TO_GROUP = {t["slug"]: t["group_letter"] for t in all_teams()}

# Common alternate names a web search might return, mapped to FIFA-official names.
TEAM_ALIASES = {
    "south korea": "Korea Republic",
    "korea": "Korea Republic",
    "ivory coast": "Côte d'Ivoire",
    "cote d'ivoire": "Côte d'Ivoire",
    "cape verde": "Cabo Verde",
    "turkey": "Türkiye",
    "usa": "United States",
    "united states of america": "United States",
    "czech republic": "Czechia",
    "bosnia": "Bosnia and Herzegovina",
    "bosnia & herzegovina": "Bosnia and Herzegovina",
    "dr congo": "DR Congo",
    "democratic republic of the congo": "DR Congo",
    "congo dr": "DR Congo",
    "ir iran": "Iran",
    "iran ir": "Iran",
    "republic of korea": "Korea Republic",
    "south korea republic": "Korea Republic",
}

# Real wc_matches columns we write. status/score/result are intentionally omitted
# from upserts so re-running never clobbers a published preview or a final score.
ROW_COLUMNS = (
    "match_number", "stage", "group_letter", "team_a_id", "team_b_id",
    "team_a_placeholder", "team_b_placeholder", "kickoff_utc", "venue", "city",
)


# ── confirmed Matchday 1 schedule (times in ET) ─────────────────────────────
# (match_number, team_a, team_b, (Y, M, D, hour, minute) ET, venue, city)
MATCHDAY_1 = [
    (1,  "Mexico", "South Africa",            (2026, 6, 11, 15, 0), "Estadio Azteca",        "Mexico City"),
    (2,  "Korea Republic", "Czechia",         (2026, 6, 11, 22, 0), "Estadio Akron",         "Guadalajara"),
    (3,  "Canada", "Bosnia and Herzegovina",  (2026, 6, 12, 15, 0), "BMO Field",             "Toronto"),
    (4,  "United States", "Paraguay",         (2026, 6, 12, 21, 0), "SoFi Stadium",          "Los Angeles"),
    (5,  "Qatar", "Switzerland",              (2026, 6, 13, 15, 0), "Levi's Stadium",        "San Francisco"),
    (6,  "Brazil", "Morocco",                 (2026, 6, 13, 18, 0), "MetLife Stadium",       "New York/New Jersey"),
    (7,  "Haiti", "Scotland",                 (2026, 6, 13, 21, 0), "Gillette Stadium",      "Boston"),
    (8,  "Australia", "Türkiye",              (2026, 6, 13, 18, 0), "BC Place",              "Vancouver"),
    (9,  "Germany", "Curaçao",                (2026, 6, 14, 13, 0), "NRG Stadium",           "Houston"),
    (10, "Netherlands", "Japan",              (2026, 6, 14, 16, 0), "AT&T Stadium",          "Dallas"),
    (11, "Côte d'Ivoire", "Ecuador",          (2026, 6, 14, 19, 0), "Mercedes-Benz Stadium", "Atlanta"),
    (12, "Sweden", "Tunisia",                 (2026, 6, 14, 22, 0), "Hard Rock Stadium",     "Miami"),
    (13, "Belgium", "Egypt",                  (2026, 6, 15, 13, 0), "Lumen Field",           "Seattle"),
    (14, "Spain", "Cabo Verde",               (2026, 6, 15, 16, 0), "Arrowhead Stadium",     "Kansas City"),
    (15, "Saudi Arabia", "Uruguay",           (2026, 6, 15, 19, 0), "Estadio BBVA",          "Monterrey"),
    (16, "Iran", "New Zealand",               (2026, 6, 15, 22, 0), "Levi's Stadium",        "San Francisco"),
    (17, "France", "Senegal",                 (2026, 6, 16, 13, 0), "MetLife Stadium",       "New York/New Jersey"),
    (18, "Argentina", "Algeria",              (2026, 6, 16, 16, 0), "Hard Rock Stadium",     "Miami"),
    (19, "Iraq", "Norway",                    (2026, 6, 16, 19, 0), "BMO Field",             "Toronto"),
    (20, "Austria", "Jordan",                 (2026, 6, 16, 22, 0), "SoFi Stadium",          "Los Angeles"),
    (21, "Portugal", "DR Congo",              (2026, 6, 17, 13, 0), "NRG Stadium",           "Houston"),
    (22, "England", "Croatia",                (2026, 6, 17, 16, 0), "Mercedes-Benz Stadium", "Atlanta"),
    (23, "Uzbekistan", "Colombia",            (2026, 6, 17, 19, 0), "Gillette Stadium",      "Boston"),
    (24, "Ghana", "Panama",                   (2026, 6, 17, 22, 0), "Estadio Akron",         "Guadalajara"),
]

# Knockout bracket: FIFA match-number ranges -> stage (32 matches, 73-104).
KNOCKOUT_STAGES = [
    (range(73, 89),  "Round of 32"),
    (range(89, 97),  "Round of 16"),
    (range(97, 101), "Quarter-finals"),
    (range(101, 103), "Semi-finals"),
    (range(103, 104), "Third-place"),
    (range(104, 105), "Final"),
]


# ── helpers ─────────────────────────────────────────────────────────────────

def et_to_utc(parts: tuple[int, int, int, int, int]) -> str:
    """(Y, M, D, hour, minute) in ET -> UTC ISO8601 string."""
    y, mo, d, h, mi = parts
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc).isoformat()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


# accent/alias-insensitive lookup of an official slug from any name spelling
_NORM_NAME_TO_SLUG = {_norm(n): s for n, s in NAME_TO_SLUG.items()}


def resolve_slug(name: str) -> str | None:
    n = _norm(name)
    if n in TEAM_ALIASES:
        n = _norm(TEAM_ALIASES[n])
    return _NORM_NAME_TO_SLUG.get(n)


def group_fixture(num: int, name_a: str, name_b: str,
                  kickoff_utc: str | None, venue: str | None,
                  city: str | None) -> dict | None:
    slug_a, slug_b = resolve_slug(name_a), resolve_slug(name_b)
    if not slug_a or not slug_b:
        print(f"  ! match {num}: unknown team "
              f"({name_a if not slug_a else name_b!r}) - skipped")
        return None
    return {
        "match_number": num,
        "stage": "Group Stage",
        "group_letter": SLUG_TO_GROUP[slug_a],
        "team_a_slug": slug_a,
        "team_b_slug": slug_b,
        "team_a_placeholder": None,
        "team_b_placeholder": None,
        "kickoff_utc": kickoff_utc,
        "venue": venue,
        "city": city,
    }


def matchday_1_fixtures() -> list[dict]:
    out = []
    for num, a, b, et, venue, city in MATCHDAY_1:
        fx = group_fixture(num, a, b, et_to_utc(et), venue, city)
        if fx:
            out.append(fx)
    return out


def knockout_fixtures() -> list[dict]:
    out = []
    for rng, stage in KNOCKOUT_STAGES:
        for num in rng:
            out.append({
                "match_number": num,
                "stage": stage,
                "group_letter": None,
                "team_a_slug": None,
                "team_b_slug": None,
                "team_a_placeholder": "TBD",
                "team_b_placeholder": "TBD",
                "kickoff_utc": None,
                "venue": None,
                "city": None,
            })
    return out


# ── web-sourced fixtures (Matchdays 2 and 3) ────────────────────────────────

def matchday_prompt(md: int, lo: int, hi: int) -> str:
    return f"""\
Use web search to find the official FIFA World Cup 2026 group-stage fixtures for
MATCHDAY {md} (FIFA match numbers {lo} to {hi} inclusive). For each match return
the FIFA match number, both teams using official FIFA names (use "Iran" not "IR
Iran", "Korea Republic" not "South Korea"), the kickoff as a full ISO 8601
timestamp WITH timezone offset (e.g. 2026-06-18T16:00:00-04:00), the OFFICIAL
stadium name (e.g. "MetLife Stadium", "Estadio Azteca", "BC Place", never a
"<City> Stadium" placeholder) and the host city. Return ONE JSON object only,
no commentary:

{{"matches": [
  {{"match_number": {lo}, "team_a": "<name>", "team_b": "<name>",
    "kickoff_iso": "2026-06-18T16:00:00-04:00", "venue": "<stadium>", "city": "<city>"}}
]}}"""


def fetch_web_matchday(client, md: int) -> list[dict]:
    lo, hi = (25, 48) if md == 2 else (49, 72)
    print(f"  web: sourcing Matchday {md} (matches {lo}-{hi}) ...", flush=True)
    data = ask_json(client, matchday_prompt(md, lo, hi), max_tokens=6000)
    out = []
    for m in data.get("matches", []):
        try:
            num = int(m["match_number"])
            if not lo <= num <= hi:
                print(f"  ! match {num} outside {lo}-{hi}, skipped")
                continue
            kickoff = datetime.fromisoformat(m["kickoff_iso"]).astimezone(timezone.utc).isoformat()
        except (KeyError, ValueError, TypeError) as e:
            print(f"  ! bad web match row {m!r}: {e}")
            continue
        fx = group_fixture(num, m.get("team_a", ""), m.get("team_b", ""),
                           kickoff, m.get("venue"), m.get("city"))
        if fx:
            out.append(fx)
    print(f"    got {len(out)} valid fixtures")
    return out


# ── DB write ────────────────────────────────────────────────────────────────

def to_row(fx: dict, slug_to_id: dict[str, str]) -> dict:
    row = {k: fx.get(k) for k in ROW_COLUMNS if k != "team_a_id" and k != "team_b_id"}
    row["team_a_id"] = slug_to_id.get(fx.get("team_a_slug"))
    row["team_b_id"] = slug_to_id.get(fx.get("team_b_slug"))
    return row


def fetch_slug_to_id(sb) -> dict[str, str]:
    rows = sb.table("wc_teams").select("id,slug").execute().data
    return {r["slug"]: r["id"] for r in rows}


def upsert_fixtures(sb, fixtures: list[dict], slug_to_id: dict[str, str]) -> int:
    rows = [to_row(fx, slug_to_id) for fx in fixtures]
    # batch to stay well under any payload limits
    for i in range(0, len(rows), 50):
        sb.table("wc_matches").upsert(
            rows[i:i + 50], on_conflict="match_number"
        ).execute()
    return len(rows)


def update_knockout_teams(sb, client) -> int:
    """Fill knockout team IDs/placeholders from confirmed bracket info (web)."""
    prompt = """\
Use web search to find which teams (if any) are CONFIRMED for the 2026 FIFA World
Cup knockout stage (match numbers 73-104), with their FIFA match number, stadium,
host city and kickoff as ISO 8601 with offset. Only include matches whose teams
are officially confirmed. Return ONE JSON object only:

{"matches": [
  {"match_number": 73, "team_a": "<name or placeholder>", "team_b": "<name or placeholder>",
   "kickoff_iso": "2026-06-28T16:00:00-04:00", "venue": "<stadium>", "city": "<city>"}
]}"""
    data = ask_json(client, prompt, max_tokens=4000)
    slug_to_id = fetch_slug_to_id(sb)
    updated = 0
    for m in data.get("matches", []):
        try:
            num = int(m["match_number"])
        except (KeyError, ValueError, TypeError):
            continue
        if not 73 <= num <= 104:
            continue
        patch: dict = {}
        slug_a, slug_b = resolve_slug(m.get("team_a", "")), resolve_slug(m.get("team_b", ""))
        if slug_a:
            patch["team_a_id"] = slug_to_id.get(slug_a)
            patch["team_a_placeholder"] = None
        elif m.get("team_a"):
            patch["team_a_placeholder"] = m["team_a"]
        if slug_b:
            patch["team_b_id"] = slug_to_id.get(slug_b)
            patch["team_b_placeholder"] = None
        elif m.get("team_b"):
            patch["team_b_placeholder"] = m["team_b"]
        if m.get("kickoff_iso"):
            try:
                patch["kickoff_utc"] = datetime.fromisoformat(
                    m["kickoff_iso"]).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                pass
        if m.get("venue"):
            patch["venue"] = m["venue"]
        if m.get("city"):
            patch["city"] = m["city"]
        if patch:
            sb.table("wc_matches").update(patch).eq("match_number", num).execute()
            updated += 1
    return updated


# ── main ────────────────────────────────────────────────────────────────────

def print_table(fixtures: list[dict]) -> None:
    print(f"\n{'#':>3}  {'stage':<14} {'grp':<3} {'team a':<24} {'team b':<24} "
          f"{'kickoff (UTC)':<20} venue")
    print("-" * 120)
    for fx in sorted(fixtures, key=lambda f: f["match_number"]):
        a = fx.get("team_a_slug") or fx.get("team_a_placeholder") or "TBD"
        b = fx.get("team_b_slug") or fx.get("team_b_placeholder") or "TBD"
        print(f"{fx['match_number']:>3}  {fx['stage']:<14} "
              f"{(fx.get('group_letter') or ''):<3} {a:<24} {b:<24} "
              f"{(fx.get('kickoff_utc') or 'TBD'):<20} {fx.get('venue') or ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-web", action="store_true",
                    help="skip the web-sourced Matchday 2-3 step (MD1 + knockouts only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print fixtures, write nothing")
    ap.add_argument("--knockout-update", action="store_true",
                    help="fill knockout team IDs from confirmed bracket (web) and exit")
    args = ap.parse_args()

    if args.knockout_update:
        sb, client = get_supabase(), get_anthropic()
        n = update_knockout_teams(sb, client)
        print(f"Knockout rows updated: {n}")
        return

    fixtures = matchday_1_fixtures()

    if not args.no_web:
        client = get_anthropic()
        for md in (2, 3):
            try:
                fixtures += fetch_web_matchday(client, md)
            except Exception as e:  # noqa: BLE001
                print(f"  ! Matchday {md} web fetch failed: {e}")

    fixtures += knockout_fixtures()

    print_table(fixtures)
    print(f"\nTotal fixtures: {len(fixtures)}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    sb = get_supabase()
    slug_to_id = fetch_slug_to_id(sb)
    n = upsert_fixtures(sb, fixtures, slug_to_id)
    print(f"\nUpserted {n} fixtures to wc_matches.")


if __name__ == "__main__":
    main()
