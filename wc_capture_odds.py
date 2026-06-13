"""
wc_capture_odds.py
Capture the World Cup 2026 outright (tournament winner) odds and append a
time-series row per team to wc_odds_history.

Calls The Odds API (soccer_fifa_world_cup_winner, regions=au,us, market=outrights),
maps each bookmaker outcome name to a wc_teams row via the same FIFA name-alias
logic used in the site's src/lib/odds-api.ts, then inserts one row per team with
the best (longest) decimal price across all books plus every book's price in
all_prices.

Deduplication: if a team's best_price is unchanged since its last capture, the
insert is skipped, so the history only records actual movement.

Designed to run every 30 minutes via Task Scheduler / cron:
    python wc_capture_odds.py

Env (E:\\OneDrive\\World_Cup\\.env): SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
ODDS_API_KEY. Optional overrides: ODDS_WINNER_SPORT, ODDS_REGIONS.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata

from wc_lib import env, get_supabase

ODDS_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_SPORT = "soccer_fifa_world_cup_winner"
DEFAULT_REGIONS = "au,us"
MARKET_TYPE = "outright_winner"  # value stored in wc_odds_history.market_type
PRICE_EPS = 1e-9


# ── FIFA official <-> bookmaker name matching (ported from odds-api.ts) ───────

def _norm(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


# Our (FIFA) name, normalised -> bookmaker spellings, normalised.
ALIASES: dict[str, list[str]] = {
    "turkiye": ["turkey"],
    "korearepublic": ["southkorea", "korea", "republicofkorea"],
    "cotedivoire": ["ivorycoast", "cotedivoire"],
    "caboverde": ["capeverde"],
    "drcongo": [
        "democraticrepublicofthecongo",
        "democraticrepublicofcongo",
        "congodr",
        "drcongo",
    ],
    "unitedstates": ["usa", "unitedstates", "unitedstatesofamerica"],
    "bosniaandherzegovina": ["bosnia", "bosniaherzegovina", "bosniaandherzegovina"],
    "czechia": ["czechrepublic", "czechia"],
}


def team_name_matches(our_name: str, api_name: str) -> bool:
    o = _norm(our_name)
    a = _norm(api_name)
    if not o or not a:
        return False
    if o == a:
        return True
    return a in ALIASES.get(o, [])


# ── Odds API fetch (TLS-tolerant, mirrors wc_update_results.py) ───────────────

def fetch_outright_events(api_key: str, sport: str, regions: str) -> list[dict]:
    import httpx

    url = f"{ODDS_BASE}/sports/{sport}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": "outrights",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        resp = httpx.get(url, params=params, timeout=30)
    except (httpx.ConnectError, httpx.TransportError) as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        print(
            "  ! TLS verification failed - retrying without certificate verification.",
            file=sys.stderr,
        )
        resp = httpx.get(url, params=params, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.json()


# ── aggregation ───────────────────────────────────────────────────────────--

def aggregate(events: list[dict], teams: list[dict]):
    """Return (per_team, unmatched).

    per_team: team_id -> {"best_price", "bookmaker", "all_prices": {book: price}}
    unmatched: sorted list of outcome names that matched no team.
    """
    per_team: dict[str, dict] = {}
    unmatched: set[str] = set()

    def find_team(outcome_name: str) -> dict | None:
        for t in teams:
            if team_name_matches(t["name"], outcome_name):
                return t
        return None

    for ev in events:
        for book in ev.get("bookmakers", []) or []:
            title = book.get("title") or book.get("key") or "unknown"
            for market in book.get("markets", []) or []:
                if market.get("key") != "outrights":
                    continue
                for outcome in market.get("outcomes", []) or []:
                    name = outcome.get("name")
                    price = outcome.get("price")
                    if not name or not isinstance(price, (int, float)):
                        continue
                    team = find_team(name)
                    if team is None:
                        unmatched.add(name)
                        continue
                    slot = per_team.setdefault(
                        team["id"],
                        {"best_price": None, "bookmaker": None, "all_prices": {}},
                    )
                    # keep the longest price each book shows for this team
                    prev = slot["all_prices"].get(title)
                    if prev is None or price > prev:
                        slot["all_prices"][title] = float(price)
                    if slot["best_price"] is None or price > slot["best_price"]:
                        slot["best_price"] = float(price)
                        slot["bookmaker"] = title

    return per_team, sorted(unmatched)


def last_best_price(sb, team_id: str) -> float | None:
    res = (
        sb.table("wc_odds_history")
        .select("best_price")
        .eq("team_id", team_id)
        .eq("market_type", MARKET_TYPE)
        .order("captured_at", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return float(res.data[0]["best_price"])
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Capture WC outright odds into wc_odds_history."
    )
    ap.add_argument("--sport", default=os.environ.get("ODDS_WINNER_SPORT", DEFAULT_SPORT))
    ap.add_argument("--regions", default=os.environ.get("ODDS_REGIONS", DEFAULT_REGIONS))
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and map, but do not write to Supabase",
    )
    args = ap.parse_args()

    api_key = env("ODDS_API_KEY")
    sb = get_supabase()

    teams = sb.table("wc_teams").select("id,name,slug").execute().data or []
    if not teams:
        sys.exit("No teams in wc_teams - run wc_populate.py first.")

    print(f"Fetching {args.sport} outrights (regions={args.regions}) ...", flush=True)
    events = fetch_outright_events(api_key, args.sport, args.regions)
    per_team, unmatched = aggregate(events, teams)

    if not per_team:
        print("No team prices returned by the API. Nothing to capture.")
        if unmatched:
            print(f"Unmatched outcome names: {', '.join(unmatched)}")
        return

    captured = skipped = 0
    for team_id, info in per_team.items():
        best = info["best_price"]
        prev = last_best_price(sb, team_id)
        if prev is not None and abs(prev - best) < PRICE_EPS:
            skipped += 1
            continue
        if args.dry_run:
            captured += 1
            continue
        sb.table("wc_odds_history").insert(
            {
                "team_id": team_id,
                "market_type": MARKET_TYPE,
                "bookmaker": info["bookmaker"],
                "best_price": best,
                "all_prices": info["all_prices"],
            }
        ).execute()
        captured += 1

    suffix = " (dry-run, no writes)" if args.dry_run else ""
    print(
        f"Done. teams_with_prices={len(per_team)} captured={captured} "
        f"skipped_unchanged={skipped}{suffix}"
    )
    if unmatched:
        print(f"Mapping failures ({len(unmatched)}): {', '.join(unmatched)}")
    else:
        print("Mapping failures: none")


if __name__ == "__main__":
    main()
