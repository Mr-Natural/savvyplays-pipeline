#!/usr/bin/env python3
"""
SavvyPlays — World Cup Results Updater
======================================

Fetches finished 2026 FIFA World Cup match results for a given date and writes
them into the Supabase `wc_matches` table (score_a, score_b, result, status).

"Web-searching for results" is done programmatically against ESPN's public,
keyless scoreboard JSON API (the same data a web search would surface), so the
script is fully deterministic and needs no API key beyond the Supabase
service-role key.

Usage
-----
    # Update every finished match played today (UTC)
    python wc_update_results.py

    # Update every finished match on a specific date
    python wc_update_results.py --date 2026-06-11

    # Update a single match by its FIFA match number
    # (date is inferred from the match's kickoff if --date is omitted)
    python wc_update_results.py --match 2

    # Preview changes without writing
    python wc_update_results.py --date 2026-06-11 --dry-run

Environment
-----------
Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment, or from a
.env.local file (searched in: --env path, CWD, this script's dir, and the
savvyplays project dir). Pass --insecure (or set WC_INSECURE_SSL=1) if your
network does TLS interception and certificate verification fails.
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("This script requires httpx:  pip install httpx")


ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
)

# ESPN display names that differ from the official FIFA naming used in wc_teams.
# Keys and values are normalised (see _norm) before lookup, so only the spelling
# differences that survive normalisation need listing here.
NAME_ALIASES = {
    "south korea": "Korea Republic",
    "korea republic": "Korea Republic",
    "bosnia herzegovina": "Bosnia and Herzegovina",
    "bosnia and herzegovina": "Bosnia and Herzegovina",
    "czech republic": "Czechia",
    "turkey": "Türkiye",
    "ivory coast": "Côte d'Ivoire",
    "cape verde": "Cabo Verde",
    "cape verde islands": "Cabo Verde",
    "usa": "United States",
    "united states of america": "United States",
    "dr congo": "DR Congo",
    "congo dr": "DR Congo",
    "democratic republic of congo": "DR Congo",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """Lowercase, strip accents and punctuation — for fuzzy team-name matching."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = "".join(c if c.isalnum() else " " for c in ascii_only.lower())
    return " ".join(cleaned.split())


def load_env() -> None:
    """Populate SUPABASE_* from a .env.local file if not already in the env."""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        return
    candidates = []
    if os.environ.get("WC_ENV_FILE"):
        candidates.append(Path(os.environ["WC_ENV_FILE"]))
    candidates += [
        Path.cwd() / ".env.local",
        Path(__file__).resolve().parent / ".env.local",
        Path("C:/dev/savvyplays/.env.local"),
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
            return


def make_client(insecure: bool) -> httpx.Client:
    verify = not (insecure or os.environ.get("WC_INSECURE_SSL") == "1")
    return httpx.Client(
        verify=verify,
        timeout=30,
        headers={"User-Agent": "SavvyPlays-wc-updater/1.0"},
    )


def _get(client: httpx.Client, url: str, **kw) -> httpx.Response:
    """GET with an automatic one-shot fallback to verify=False on TLS errors."""
    try:
        return client.get(url, **kw)
    except (httpx.ConnectError, httpx.TransportError) as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc) or client._transport is None:
            raise
        print(
            "  ! TLS verification failed — retrying without certificate "
            "verification (set --insecure to silence this).",
            file=sys.stderr,
        )
        insecure = httpx.Client(
            verify=False, timeout=client.timeout, headers=dict(client.headers)
        )
        return insecure.get(url, **kw)


# ── ESPN results ─────────────────────────────────────────────────────────────

def fetch_espn_results(client: httpx.Client, date: datetime) -> list[dict]:
    """Return a list of result dicts for the date's scoreboard.

    Each dict: {home, away, home_score, away_score, completed, state}.
    """
    resp = _get(
        client,
        ESPN_SCOREBOARD,
        params={"dates": date.strftime("%Y%m%d")},
    )
    resp.raise_for_status()
    events = resp.json().get("events", []) or []
    out = []
    for ev in events:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status = (comp.get("status") or {}).get("type") or {}
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        def _score(c):
            s = c.get("score")
            try:
                return int(s)
            except (TypeError, ValueError):
                return None

        out.append(
            {
                "home": (home.get("team") or {}).get("displayName"),
                "away": (away.get("team") or {}).get("displayName"),
                "home_score": _score(home),
                "away_score": _score(away),
                "completed": bool(status.get("completed")),
                "state": status.get("state"),
                "detail": status.get("detail") or status.get("description"),
            }
        )
    return out


# ── Supabase ─────────────────────────────────────────────────────────────────

class Supabase:
    def __init__(self, client: httpx.Client):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.client = client
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    def get_matches(self) -> list[dict]:
        sel = (
            "id,match_number,stage,group_letter,kickoff_utc,status,"
            "score_a,score_b,result,team_a_id,team_b_id,"
            "team_a:wc_teams!team_a_id(name,slug),"
            "team_b:wc_teams!team_b_id(name,slug)"
        )
        r = _get(
            self.client,
            f"{self.url}/rest/v1/wc_matches",
            params={"select": sel, "order": "match_number"},
            headers=self.headers,
        )
        r.raise_for_status()
        return r.json()

    def patch_match(self, match_id: str, payload: dict) -> None:
        r = self.client.patch(
            f"{self.url}/rest/v1/wc_matches",
            params={"id": f"eq.{match_id}"},
            headers={**self.headers, "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json=payload,
        )
        r.raise_for_status()


def build_team_lookup(matches: list[dict]) -> dict[str, str]:
    """Map normalised team name/slug -> team id, from the embedded team refs."""
    lookup: dict[str, str] = {}
    for m in matches:
        for side in ("team_a", "team_b"):
            ref = m.get(side) or {}
            tid = m.get(f"{side}_id")
            if not tid:
                continue
            if ref.get("name"):
                lookup[_norm(ref["name"])] = tid
            if ref.get("slug"):
                lookup[_norm(ref["slug"].replace("-", " "))] = tid
    return lookup


def resolve_team_id(espn_name: str, lookup: dict[str, str]) -> str | None:
    norm = _norm(espn_name)
    if norm in lookup:
        return lookup[norm]
    alias = NAME_ALIASES.get(norm)
    if alias and _norm(alias) in lookup:
        return lookup[_norm(alias)]
    return None


def result_enum(score_a: int, score_b: int) -> str:
    if score_a > score_b:
        return "team_a"
    if score_a < score_b:
        return "team_b"
    return "draw"


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Update wc_matches with finished results.")
    p.add_argument("--date", help="Match date to fetch, YYYY-MM-DD (default: today UTC).")
    p.add_argument("--match", type=int, help="Only update this FIFA match number.")
    p.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS certificate verification (TLS-intercepting networks).")
    p.add_argument("--force", action="store_true",
                   help="Re-write matches even if already marked completed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_env()
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (env or .env.local).")

    client = make_client(args.insecure)
    db = Supabase(client)
    matches = db.get_matches()
    lookup = build_team_lookup(matches)
    by_number = {m["match_number"]: m for m in matches}

    # Decide which date(s) to query.
    if args.match is not None:
        target = by_number.get(args.match)
        if not target:
            sys.exit(f"Match number {args.match} not found.")
        if args.date:
            dates = [datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)]
        elif target.get("kickoff_utc"):
            dt = datetime.fromisoformat(target["kickoff_utc"].replace("Z", "+00:00"))
            dates = [dt.astimezone(timezone.utc)]
        else:
            sys.exit("Match has no kickoff date; pass --date explicitly.")
    elif args.date:
        dates = [datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)]
    else:
        dates = [datetime.now(timezone.utc)]

    # Build an index of wc_matches keyed by the unordered pair of team ids.
    pair_index: dict[frozenset, dict] = {}
    for m in matches:
        if m.get("team_a_id") and m.get("team_b_id"):
            pair_index[frozenset((m["team_a_id"], m["team_b_id"]))] = m

    applied, skipped = [], []

    for date in dates:
        results = fetch_espn_results(client, date)
        for res in results:
            label = f"{res['home']} {res.get('home_score')}-{res.get('away_score')} {res['away']}"

            if not res["completed"] or res["home_score"] is None or res["away_score"] is None:
                skipped.append((label, f"not finished ({res.get('state')})"))
                continue

            home_id = resolve_team_id(res["home"], lookup)
            away_id = resolve_team_id(res["away"], lookup)
            if not home_id or not away_id:
                unknown = res["home"] if not home_id else res["away"]
                skipped.append((label, f"team not matched: {unknown!r}"))
                continue

            match = pair_index.get(frozenset((home_id, away_id)))
            if not match:
                skipped.append((label, "no wc_matches fixture for this pairing"))
                continue

            if args.match is not None and match["match_number"] != args.match:
                continue  # single-match mode: ignore everything else

            # Align ESPN home/away to our team_a/team_b ordering.
            if home_id == match["team_a_id"]:
                score_a, score_b = res["home_score"], res["away_score"]
            else:
                score_a, score_b = res["away_score"], res["home_score"]
            result = result_enum(score_a, score_b)

            already = (
                match.get("status") == "completed"
                and match.get("score_a") == score_a
                and match.get("score_b") == score_b
            )
            if already and not args.force:
                skipped.append((label, f"already up to date (match #{match['match_number']})"))
                continue

            payload = {
                "score_a": score_a,
                "score_b": score_b,
                "result": result,
                "status": "completed",
            }
            ta = (match.get("team_a") or {}).get("name", "team_a")
            tb = (match.get("team_b") or {}).get("name", "team_b")
            change = f"#{match['match_number']:>3}  {ta} {score_a}-{score_b} {tb}  [{result}]"
            if args.dry_run:
                applied.append(("DRY", change))
            else:
                db.patch_match(match["id"], payload)
                applied.append(("OK", change))

    # ── summary ──────────────────────────────────────────────────────────────
    out = sys.stdout.buffer
    head = "Dry run — no writes" if args.dry_run else "Updates applied"
    out.write(f"\n{head}: {len(applied)}\n".encode("utf-8"))
    for tag, change in applied:
        out.write(f"  ✓ [{tag}] {change}\n".encode("utf-8"))
    if skipped:
        out.write(f"\nSkipped: {len(skipped)}\n".encode("utf-8"))
        for label, why in skipped:
            out.write(f"  – {label}  ({why})\n".encode("utf-8"))
    out.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
