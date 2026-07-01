#!/usr/bin/env python3
"""
SavvyPlays — World Cup Pre-Tournament Prediction Grader
=======================================================

Grades the pre-tournament predictions in `wc_predictions` against actual
results so far. Three components per team:

  1. Group position (predicted_group_pos vs actual final rank, computed from
     wc_matches for completed groups only).
  2. Advance / eliminate call (did we put them through to the knockouts?).
  3. Top scorer (top_scorer_name vs the team's actual leading scorer from
     match_events: goal + penalty).

Only groups where ALL THREE matches are completed are graded. Third-place
teams stay TBD on the advance/eliminate call until the best-thirds tiebreak
resolves (after all 12 groups finish).

Usage
-----
    python wc_grade_predictions.py            # print summary + per-team detail
    python wc_grade_predictions.py --json     # machine-readable dump
    python wc_grade_predictions.py --group A  # restrict to one group

Environment is the same as the rest of the pipeline (uses wc_lib).
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import Counter, defaultdict
from typing import Any

import wc_lib

# Windows consoles default to cp1252; force UTF-8 so the box-drawing chars and
# accented player names render instead of throwing UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


# ── helpers ─────────────────────────────────────────────────────────────────

def _norm_name(s: str | None) -> str:
    """Normalise a player name for fuzzy comparison: strip diacritics, lower,
    collapse whitespace. Used only to compare a predicted name to actual goal
    scorers — never to overwrite stored data."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _last_token(s: str) -> str:
    parts = _norm_name(s).split()
    return parts[-1] if parts else ""


def _player_match(predicted: str, actual: str) -> bool:
    """Loose match: exact normalised match, or last-name match if the
    predicted name has a single token or the surnames line up. Avoids
    false positives by requiring at least 3 chars in the surname."""
    pn = _norm_name(predicted)
    an = _norm_name(actual)
    if not pn or not an:
        return False
    if pn == an:
        return True
    p_last = _last_token(predicted)
    a_last = _last_token(actual)
    if len(p_last) >= 3 and p_last == a_last:
        return True
    return False


# ── data fetch ──────────────────────────────────────────────────────────────

def fetch_data(sb) -> dict[str, Any]:
    teams = (
        sb.table("wc_teams")
        .select("id, name, slug, group_letter, flag_emoji")
        .execute()
        .data
    ) or []

    preds = (
        sb.table("wc_predictions")
        .select(
            "team_id, predicted_group_pos, predicted_exit_round, "
            "top_scorer_name, top_scorer_goals"
        )
        .execute()
        .data
    ) or []

    # All matches (group + knockout). Group standings filter by group_letter,
    # so knockouts pass through harmlessly there; the scorer tally needs the
    # full tournament view once knockouts start producing goals.
    matches = (
        sb.table("wc_matches")
        .select(
            "match_number, stage, group_letter, status, "
            "team_a_id, team_b_id, score_a, score_b, match_events"
        )
        .execute()
        .data
    ) or []

    teams_by_id = {t["id"]: t for t in teams}
    preds_by_team = {p["team_id"]: p for p in preds}
    return {
        "teams": teams,
        "teams_by_id": teams_by_id,
        "preds_by_team": preds_by_team,
        "matches": matches,
    }


# ── group standings ─────────────────────────────────────────────────────────

def compute_group_standings(matches: list[dict], teams_by_id: dict) -> dict[str, list[dict]]:
    """Return {group_letter: [ranked team rows]} only for groups where all
    three matches per team (6 total) are completed. Ranking: points, then
    goal difference, then goals for. (Head-to-head tiebreakers and the FIFA
    fair-play / drawing-of-lots edge cases are not implemented — at this
    stage of the bracket, GD/GF resolves nearly every group, and any actual
    tie is flagged separately.)
    """
    by_group: dict[str, list[dict]] = defaultdict(list)
    for m in matches:
        if m.get("group_letter"):
            by_group[m["group_letter"]].append(m)

    standings: dict[str, list[dict]] = {}
    for letter, ms in by_group.items():
        if len(ms) < 6:
            continue
        if any(m["status"] != "completed" or m["score_a"] is None or m["score_b"] is None for m in ms):
            continue

        tbl: dict[str, dict] = {}
        for m in ms:
            for tid in (m["team_a_id"], m["team_b_id"]):
                if tid and tid not in tbl:
                    tbl[tid] = {
                        "team_id": tid,
                        "p": 0, "w": 0, "d": 0, "l": 0,
                        "gf": 0, "ga": 0, "pts": 0,
                    }

            a, b = m["team_a_id"], m["team_b_id"]
            sa, sb = m["score_a"], m["score_b"]
            if a not in tbl or b not in tbl:
                continue
            tbl[a]["p"] += 1
            tbl[b]["p"] += 1
            tbl[a]["gf"] += sa
            tbl[a]["ga"] += sb
            tbl[b]["gf"] += sb
            tbl[b]["ga"] += sa
            if sa > sb:
                tbl[a]["w"] += 1
                tbl[a]["pts"] += 3
                tbl[b]["l"] += 1
            elif sa < sb:
                tbl[b]["w"] += 1
                tbl[b]["pts"] += 3
                tbl[a]["l"] += 1
            else:
                tbl[a]["d"] += 1
                tbl[b]["d"] += 1
                tbl[a]["pts"] += 1
                tbl[b]["pts"] += 1

        rows = list(tbl.values())
        rows.sort(key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"]))
        for i, r in enumerate(rows):
            r["pos"] = i + 1
            r["gd"] = r["gf"] - r["ga"]
            t = teams_by_id.get(r["team_id"], {})
            r["team_name"] = t.get("name", "?")
            r["flag"] = t.get("flag_emoji", "")
        standings[letter] = rows

    return standings


# ── top scorers ─────────────────────────────────────────────────────────────

def compute_team_scorers(matches: list[dict]) -> dict[str, dict]:
    """For every team that appears in a completed group-stage match, tally
    goals (goal + penalty) per player. Returns {team_id: {
      'tally': Counter, 'top_player': str|None, 'top_goals': int,
      'all_top': [names tied for top]
    }}.

    Limited to the group stage for now — that's all the predictions page
    needs to grade pre-tournament Golden Boot calls during the group phase.
    Knockout goals can be folded in later by removing the stage filter on
    the fetch."""
    out: dict[str, dict] = defaultdict(lambda: {"tally": Counter()})
    for m in matches:
        if m["status"] != "completed":
            continue
        events = m.get("match_events") or []
        for ev in events:
            etype = ev.get("event_type")
            if etype not in {"goal", "penalty"}:
                continue
            side = ev.get("team")
            tid = m["team_a_id"] if side == "a" else m["team_b_id"] if side == "b" else None
            player = (ev.get("player") or "").strip()
            if not tid or not player:
                continue
            out[tid]["tally"][player] += 1

    for tid, rec in out.items():
        tally = rec["tally"]
        if not tally:
            rec["top_player"] = None
            rec["top_goals"] = 0
            rec["all_top"] = []
            continue
        top_goals = max(tally.values())
        all_top = sorted(p for p, n in tally.items() if n == top_goals)
        rec["top_player"] = all_top[0]
        rec["top_goals"] = top_goals
        rec["all_top"] = all_top

    return out


# ── grading ─────────────────────────────────────────────────────────────────

EXIT_KNOCKOUT_ROUNDS = {
    "Round of 32", "Round of 16", "Quarter-finals",
    "Semi-finals", "Runner-up", "Winners",
}

# Standard 48-team format: top 8 of 12 third-place teams advance.
BEST_THIRDS_SLOTS = 8


def compute_best_thirds(
    standings: dict[str, list[dict]], total_groups: int
) -> dict[str, bool] | None:
    """When every group is complete, rank the twelve third-place teams and
    return {team_id: in_best_8}. Returns None while groups remain — the
    ranking can still shift, so a 3rd-place verdict can't be finalised."""
    if total_groups <= 0 or len(standings) < total_groups:
        return None
    thirds: list[dict] = []
    for rows in standings.values():
        for r in rows:
            if r["pos"] == 3:
                thirds.append(r)
    thirds.sort(
        key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"], r.get("team_name", ""))
    )
    return {r["team_id"]: i < BEST_THIRDS_SLOTS for i, r in enumerate(thirds)}


def is_eliminated(pos: int, team_id: str, best_thirds: dict[str, bool] | None) -> bool:
    """A team's tournament fate is final only if they're out: 4th in their
    group, or 3rd-and-not-in-best-8 once the third-place ranking has resolved."""
    if pos == 4:
        return True
    if pos == 3 and best_thirds is not None and not best_thirds.get(team_id, False):
        return True
    return False


def grade_team(
    team: dict,
    pred: dict | None,
    standing: dict,
    scorers: dict,
    best_thirds: dict[str, bool] | None,
) -> dict:
    """Grade one team in a completed group. `standing` is the team's row in
    its final standings table. `scorers` is the team's goal tally record."""
    result: dict[str, Any] = {
        "team_id": team["id"],
        "team_name": team["name"],
        "flag": team.get("flag_emoji"),
        "group": team["group_letter"],
        "actual_pos": standing["pos"],
        "actual_pts": standing["pts"],
        "actual_gd": standing["gd"],
        "actual_gf": standing["gf"],
    }

    # ── group position ──
    pred_pos = pred.get("predicted_group_pos") if pred else None
    result["predicted_pos"] = pred_pos
    result["pos_correct"] = pred_pos is not None and pred_pos == standing["pos"]

    # ── advance / eliminate ──
    pred_exit = pred.get("predicted_exit_round") if pred else None
    result["predicted_exit"] = pred_exit
    actual_pos = standing["pos"]
    if actual_pos in (1, 2):
        actual_status = "advanced"
    elif actual_pos == 4:
        actual_status = "eliminated"
    else:
        actual_status = "tbd"  # 3rd-place: best-thirds decides
    result["actual_status"] = actual_status

    if pred_exit is None or actual_status == "tbd":
        result["advance_correct"] = None
    elif actual_status == "advanced":
        result["advance_correct"] = pred_exit in EXIT_KNOCKOUT_ROUNDS
    else:  # eliminated
        result["advance_correct"] = pred_exit == "Group Stage"

    # ── top scorer ──
    pred_scorer = pred.get("top_scorer_name") if pred else None
    pred_scorer_goals = pred.get("top_scorer_goals") if pred else None
    result["predicted_top_scorer"] = pred_scorer
    result["predicted_top_scorer_goals"] = pred_scorer_goals
    actual_top = scorers.get("top_player")
    actual_top_goals = scorers.get("top_goals", 0)
    tally = scorers.get("tally", Counter())
    result["actual_top_scorer"] = actual_top
    result["actual_top_scorer_goals"] = actual_top_goals
    result["actual_tally"] = dict(tally)

    if not pred_scorer:
        ts_grade = "ungraded"
    elif actual_top and any(_player_match(pred_scorer, name) for name in scorers.get("all_top", [])):
        ts_grade = "exact"
    elif any(_player_match(pred_scorer, name) for name in tally.keys()):
        ts_grade = "scored"
    else:
        ts_grade = "miss"
    result["top_scorer_grade"] = ts_grade

    # Tournament fate: only count top-scorer grade when the team is OUT.
    # 1st/2nd, 3rd-pending, or 3rd-in-best-8 → still in the hunt, label IN PROGRESS.
    eliminated = is_eliminated(standing["pos"], team["id"], best_thirds)
    result["tournament_status"] = "eliminated" if eliminated else "alive"
    result["top_scorer_final"] = eliminated

    return result


# ── reporting ───────────────────────────────────────────────────────────────

def print_summary(grades: list[dict], group_count: int, total_groups: int) -> None:
    pos_total = sum(1 for g in grades if g["predicted_pos"] is not None)
    pos_correct = sum(1 for g in grades if g["pos_correct"])

    adv_graded = [g for g in grades if g["advance_correct"] is not None]
    adv_correct = sum(1 for g in adv_graded if g["advance_correct"])

    # Top scorer splits: only count teams that are out of the tournament. The
    # grade for a still-alive team can change every match, so showing it in the
    # rollup misleads — surface it separately as "still active".
    final_grades = [g for g in grades if g["top_scorer_final"]]
    active_grades = [g for g in grades if not g["top_scorer_final"]]
    ts_final = [g["top_scorer_grade"] for g in final_grades]
    ts_exact = ts_final.count("exact")
    ts_scored = ts_final.count("scored")
    ts_miss = ts_final.count("miss")
    ts_ungraded = ts_final.count("ungraded")
    ts_final_denom = ts_exact + ts_scored + ts_miss  # exclude ungraded from rate

    print("=" * 64)
    print(f"PREDICTION GRADING SUMMARY  ({group_count}/{total_groups} groups completed)")
    print("=" * 64)
    print(f"Teams graded:              {len(grades)}/48")
    print(f"Exact group position:      {pos_correct}/{pos_total}"
          f"  ({pct(pos_correct, pos_total)})")
    print(f"Advance/eliminate calls:   {adv_correct}/{len(adv_graded)}"
          f"  ({pct(adv_correct, len(adv_graded))})"
          f"   [3rd-place TBD: {len(grades) - len(adv_graded)}]")
    print()
    print(f"Top scorer (finalised — eliminated teams, {len(final_grades)} team"
          f"{'' if len(final_grades) == 1 else 's'}):")
    if ts_final_denom == 0 and ts_ungraded == 0:
        print(f"  (no eliminated teams to grade yet)")
    else:
        print(f"  exact:    {ts_exact}/{ts_final_denom}  ({pct(ts_exact, ts_final_denom)})")
        print(f"  scored:   {ts_scored}/{ts_final_denom}  ({pct(ts_scored, ts_final_denom)})")
        print(f"  miss:     {ts_miss}/{ts_final_denom}  ({pct(ts_miss, ts_final_denom)})")
        if ts_ungraded:
            print(f"  ungraded: {ts_ungraded}  (no prediction stored)")
    print()
    print(f"Top scorer (in progress — teams still alive): {len(active_grades)} still active")
    print()


def pct(n: int, d: int) -> str:
    if d == 0:
        return "—"
    return f"{round(100 * n / d)}%"


def print_group_detail(grades: list[dict]) -> None:
    by_group: dict[str, list[dict]] = defaultdict(list)
    for g in grades:
        by_group[g["group"]].append(g)

    for letter in sorted(by_group):
        rows = sorted(by_group[letter], key=lambda r: r["actual_pos"])
        print(f"── Group {letter} " + "─" * (64 - len(f"── Group {letter} ")))
        header = (
            f"{'Team':<22} {'PredPos':>7} {'Pos':>4}  {'Pts':>3} {'GD':>3} {'GF':>3}"
            f"  {'Exit pred':<14} {'Status':<10}  TopScorer"
        )
        print(header)
        for g in rows:
            pos_mark = "✓" if g["pos_correct"] else "✗" if g["predicted_pos"] else "·"
            adv_mark = (
                "✓" if g["advance_correct"] is True
                else "✗" if g["advance_correct"] is False
                else "·"
            )
            ts_goals = g["actual_top_scorer_goals"]
            ts_pred = g["predicted_top_scorer"] or "—"
            ts_pred_goals = g.get("predicted_top_scorer_goals")
            ts_grade = g["top_scorer_grade"]
            # When multiple players tie for top, list them all so a "tied for
            # top" exact-match call isn't presented as a miss in the display.
            all_top = [p for p, n in g["actual_tally"].items() if n == ts_goals] if ts_goals else []
            ts_actual = " / ".join(sorted(all_top)) if all_top else "—"
            # In-progress teams: ⏳ instead of ✓/~/✗, show predicted goals
            # alongside the predicted name so the comparison is legible.
            if g["top_scorer_final"]:
                ts_mark = {"exact": "✓", "scored": "~", "miss": "✗", "ungraded": "·"}[ts_grade]
                ts_str = (
                    f"{ts_pred} → {ts_actual}"
                    + (f" ({ts_goals})" if ts_goals else "")
                )
            else:
                ts_mark = "⏳"
                pred_part = ts_pred + (f" ({ts_pred_goals})" if ts_pred_goals else "")
                actual_part = ts_actual + (f" ({ts_goals})" if ts_goals else "")
                ts_str = f"Pred: {pred_part} | Current: {actual_part}"
            print(
                f"{g['team_name']:<22} "
                f"{str(g['predicted_pos'] or '—'):>5}{pos_mark:>2} "
                f"{g['actual_pos']:>4}  "
                f"{g['actual_pts']:>3} {g['actual_gd']:>+3} {g['actual_gf']:>3}  "
                f"{(g['predicted_exit'] or '—'):<14} "
                f"{g['actual_status']:<8}{adv_mark:>2}  "
                f"{ts_str}  {ts_mark}"
            )
        print()


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Grade pre-tournament predictions vs results.")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a human-readable report")
    ap.add_argument("--group", help="restrict to a single group letter (A–L)")
    args = ap.parse_args()

    sb = wc_lib.get_supabase()
    data = fetch_data(sb)

    standings = compute_group_standings(data["matches"], data["teams_by_id"])
    scorers = compute_team_scorers(data["matches"])

    total_groups = len({m["group_letter"] for m in data["matches"] if m.get("group_letter")})
    # Best-thirds is computed across ALL completed standings; it stays None
    # until every group has finished. A --group filter would otherwise hide
    # the data needed to determine 3rd-place fate.
    best_thirds = compute_best_thirds(standings, total_groups)

    if args.group:
        if args.group not in standings:
            print(f"Group {args.group} not yet fully completed (or doesn't exist).",
                  file=sys.stderr)
            return 1
        standings = {args.group: standings[args.group]}

    grades: list[dict] = []
    for letter, rows in standings.items():
        for row in rows:
            team = data["teams_by_id"].get(row["team_id"])
            if not team:
                continue
            pred = data["preds_by_team"].get(team["id"])
            grades.append(grade_team(team, pred, row, scorers.get(team["id"], {}), best_thirds))

    grades.sort(key=lambda g: (g["group"], g["actual_pos"]))

    if args.json:
        json.dump(
            {
                "groups_completed": sorted(standings.keys()),
                "grades": grades,
            },
            sys.stdout, indent=2, default=str,
        )
        print()
        return 0

    print_summary(grades, len(standings), total_groups)
    print_group_detail(grades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
