"""Create a public, static snapshot for the GitHub Pages World Cup site.

This is a one-way exporter: it reads the internal SQLite database but never
writes to it.  The generated JSON deliberately excludes squads, player
profiles, lineups, and match-detail events.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "video-kb"
OUTPUT_PATH = ROOT / "data" / "world-cup.json"
FLAGS_OUTPUT = ROOT / "assets" / "flags"


def read_matches(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT
            m.id,
            m.group_code,
            m.match_day,
            m.home_placeholder,
            m.away_placeholder,
            substr(m.kickoff_beijing, 1, 10) AS date,
            substr(m.kickoff_beijing, 12, 5) AS time,
            COALESCE(ht.name_zh, ht.name, m.home_placeholder, '待定') AS home,
            COALESCE(at.name_zh, at.name, m.away_placeholder, '待定') AS away,
            COALESCE(ht.name, m.home_placeholder, '待定') AS home_en,
            COALESCE(at.name, m.away_placeholder, '待定') AS away_en,
            CASE
                WHEN m.group_code IS NOT NULL AND trim(m.group_code) != ''
                    THEN m.group_code || '组'
                ELSE m.stage_name
            END AS stage,
            m.stage_code,
            m.status,
            m.status_detail,
            m.home_score AS score_home,
            m.away_score AS score_away,
            trim(
                COALESCE(m.city, '') ||
                CASE WHEN COALESCE(m.city, '') != '' AND COALESCE(m.stadium, '') != ''
                    THEN ' · ' ELSE '' END ||
                COALESCE(m.stadium, '')
            ) AS venue
        FROM matches m
        LEFT JOIN teams ht ON ht.id = m.home_team_id
        LEFT JOIN teams at ON at.id = m.away_team_id
        ORDER BY m.kickoff_beijing, m.id
        """
    )
    return [dict(row) for row in rows]


def read_teams(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT id, name, name_zh, group_code, flag_path
        FROM teams
        ORDER BY group_code, name
        """
    )
    teams = []
    for row in rows:
        item = dict(row)
        item["name_zh"] = item.get("name_zh") or item["name"]
        item["flag"] = ""
        teams.append(item)
    return teams


def read_leaderboards(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    """Return aggregate tables only; no individual event records leave the DB.

    ESPN event data has no ``card_type``. Event type itself is authoritative:
    yellow-card, red-card, and var---red-card-upgrade. Goal variants are
    prefixed with goal; scored penalties use penalty---scored.
    """
    rows = connection.execute(
        """
        WITH aggregate_events AS (
            SELECT
                e.player_id,
                e.player_name AS fallback_name,
                e.team_id,
                CASE WHEN e.event_type LIKE 'goal%'
                          OR e.event_type = 'penalty---scored' THEN 1 ELSE 0 END AS goals,
                0 AS assists,
                CASE WHEN e.event_type = 'yellow-card' THEN 1 ELSE 0 END AS yellows,
                CASE WHEN e.event_type IN ('red-card', 'var---red-card-upgrade')
                     THEN 1 ELSE 0 END AS reds
            FROM match_events e

            UNION ALL

            SELECT
                e.related_player_id,
                e.related_player_name AS fallback_name,
                e.team_id,
                0 AS goals,
                1 AS assists,
                0 AS yellows,
                0 AS reds
            FROM match_events e
            WHERE (e.event_type LIKE 'goal%' OR e.event_type = 'penalty---scored')
              AND (e.related_player_id IS NOT NULL OR trim(COALESCE(e.related_player_name, '')) != '')
        )
        SELECT
            COALESCE(p.name_zh, p.name, a.fallback_name) AS name,
            COALESCE(t.name_zh, t.name) AS team,
            SUM(a.goals) AS goals,
            SUM(a.assists) AS assists,
            SUM(a.yellows) AS yellows,
            SUM(a.reds) AS reds
        FROM aggregate_events a
        LEFT JOIN players p ON p.id = a.player_id
        LEFT JOIN teams t ON t.id = COALESCE(p.team_id, a.team_id)
        GROUP BY COALESCE(p.id, a.player_id), a.fallback_name, t.id, COALESCE(p.name_zh, p.name, a.fallback_name), COALESCE(t.name_zh, t.name)
        HAVING SUM(a.goals) > 0 OR SUM(a.assists) > 0 OR SUM(a.yellows) > 0 OR SUM(a.reds) > 0
        """
    )
    entries = [dict(row) for row in rows]
    return {
        "scorers": sorted(entries, key=lambda row: (-row["goals"], row["name"])),
        "assists": sorted(entries, key=lambda row: (-row["assists"], row["name"])),
        "cards": sorted(entries, key=lambda row: (-row["reds"], -row["yellows"], row["name"])),
    }

def copy_flags(teams: list[dict], source_root: Path) -> None:
    FLAGS_OUTPUT.mkdir(parents=True, exist_ok=True)
    flags_root = source_root / "data" / "world_cup" / "flags"
    for team in teams:
        source = Path(str(team.get("flag_path") or ""))
        if not source.is_absolute():
            source = flags_root / source.name
        if not source.exists():
            continue
        target = FLAGS_OUTPUT / source.name
        shutil.copy2(source, target)
        team["flag"] = f"assets/flags/{target.name}"
        team.pop("flag_path", None)


def read_daily_group_projections(source_matches: list[dict]) -> dict[str, list[dict]]:
    """Export daily group-table scenarios as public standings/progression data only."""
    from world_cup_page import (
        _scenario_standings,
        _third_rank_map,
        _today_group_status,
        _today_projection_matches,
    )

    def public_rows(rows: list[dict], standings: list[dict], matches: list[dict], third_ranks: dict[int, int]) -> list[dict]:
        output = []
        for row in rows:
            label, class_name = _today_group_status(row, standings, matches, third_ranks)
            output.append(
                {
                    "team_id": row["team_id"],
                    "team": row.get("team_name") or row.get("team_name_zh") or row.get("team_name_en"),
                    "team_en": row.get("team_name_en") or row.get("team_name"),
                    "position": row["position"],
                    "played": row["played"],
                    "goals_for": row["goals_for"],
                    "goals_against": row["goals_against"],
                    "goal_difference": row["goal_difference"],
                    "points": row["points"],
                    "status_label": label,
                    "status_class": class_name,
                }
            )
        return output

    group_dates = sorted(
        {
            str(match.get("kickoff_date"))
            for match in source_matches
            if match.get("stage_code") == "group" and match.get("group_code") and match.get("kickoff_date")
        }
    )
    projections: dict[str, list[dict]] = {}
    for day in group_dates:
        group_codes = sorted(
            {
                str(match["group_code"])
                for match in source_matches
                if match.get("stage_code") == "group" and match.get("kickoff_date") == day and match.get("group_code")
            }
        )
        pre_matches = _today_projection_matches(source_matches, day, include_today=False)
        current_matches = _today_projection_matches(source_matches, day, include_today=True)
        pre_standings = _scenario_standings(source_matches, day, include_today=False)
        current_standings = _scenario_standings(source_matches, day, include_today=True)
        pre_thirds = _third_rank_map(pre_standings)
        current_thirds = _third_rank_map(current_standings)
        groups = []
        for group_code in group_codes:
            day_matches = [
                match for match in source_matches
                if match.get("stage_code") == "group"
                and match.get("kickoff_date") == day
                and match.get("group_code") == group_code
            ]
            groups.append(
                {
                    "group_code": group_code,
                    "match_ids": [match["id"] for match in day_matches],
                    "all_finished": bool(day_matches) and all(match.get("status") == "finished" for match in day_matches),
                    "pre": public_rows(
                        [row for row in pre_standings if row["group_code"] == group_code],
                        pre_standings,
                        pre_matches,
                        pre_thirds,
                    ),
                    "current": public_rows(
                        [row for row in current_standings if row["group_code"] == group_code],
                        current_standings,
                        current_matches,
                        current_thirds,
                    ),
                }
            )
        if groups:
            projections[day] = groups
    return projections

def build_live_standings(standings: list[dict], source_matches: list[dict]) -> tuple[list[dict], list[dict]]:
    """Treat active scores as if the matches ended now, using the internal ranking rules."""
    from world_cup_page import _rank_simulated_group

    rows_by_group: dict[str, list[dict]] = defaultdict(list)
    results_by_group: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)
    resolved_matches = []
    for row in standings:
        rows_by_group[row["group_code"]].append(row)
    for match in source_matches:
        item = dict(match)
        if item.get("status") in {"live", "in_progress"}:
            item["status"] = "finished"
        resolved_matches.append(item)
        if (
            item.get("stage_code") == "group"
            and item.get("status") == "finished"
            and item.get("home_team_id")
            and item.get("away_team_id")
            and item.get("home_score") is not None
            and item.get("away_score") is not None
        ):
            results_by_group[str(item["group_code"])].append(
                (item["home_team_id"], item["away_team_id"], item["home_score"], item["away_score"])
            )
    live = []
    for group_code, rows in rows_by_group.items():
        live.extend(_rank_simulated_group(rows, results_by_group[group_code]))
    return live, resolved_matches

def build_snapshot(source_root: Path) -> dict:
    sys.path.insert(0, str(source_root))
    import world_cup_data  # Imported only by the local exporter, never by the site.
    from world_cup_pages.panorama import (
        _build_group_projection_map,
        _build_third_place_lock_map,
        _group_knockout_route_summary,
        _group_status_label,
        _third_place_slot_overrides,
    )

    database = source_root / "data" / "world_cup" / "world_cup.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        teams = read_teams(connection)
        copy_flags(teams, source_root)
        matches = read_matches(connection)
        standings = world_cup_data.get_standings()
        best_thirds = world_cup_data.get_best_thirds()
        slots = world_cup_data.get_qualification_slots()
        source_matches = world_cup_data.get_matches()
        daily_group_projections = read_daily_group_projections(source_matches)
        projection_map = _build_group_projection_map(standings, source_matches)
        best_thirds_map = {row["team_id"]: row for row in best_thirds}
        third_lock_map = _build_third_place_lock_map(standings, source_matches)
        group_statuses = []
        for row in standings:
            label, class_name = _group_status_label(row, best_thirds_map, projection_map, third_lock_map)
            group_statuses.append(
                {
                    "team_id": row["team_id"],
                    "group_code": row["group_code"],
                    "position": row["position"],
                    "label": label,
                    "class_name": class_name,
                }
            )
        live_standings, resolved_matches = build_live_standings(standings, source_matches)
        live_best_thirds = world_cup_data.best_third_rankings(live_standings)
        live_best_thirds_map = {row["team_id"]: row for row in live_best_thirds}
        live_projection_map = _build_group_projection_map(live_standings, resolved_matches)
        live_group_statuses = []
        for row in live_standings:
            label, class_name = _group_status_label(row, live_best_thirds_map, live_projection_map)
            live_group_statuses.append(
                {
                    "team_id": row["team_id"],
                    "group_code": row["group_code"],
                    "position": row["position"],
                    "label": label,
                    "class_name": class_name,
                }
            )
        group_routes = {
            code: _group_knockout_route_summary(code, source_matches)
            for code in sorted({row["group_code"] for row in standings})
        }
        third_place_slot_overrides = _third_place_slot_overrides(best_thirds)
        assignment_rules_path = source_root / "world_cup_pages" / "third_place_assignments_2026.json"
        assignment_rules = json.loads(assignment_rules_path.read_text(encoding="utf-8"))
        current_third_combination = "".join(
            sorted(row["group_code"] for row in best_thirds if row.get("qualifies_currently"))
        )
        third_place_mapping = {
            "source": assignment_rules.get("source"),
            "rule_count": len(assignment_rules.get("assignments", {})),
            "current_combination": current_third_combination,
        }
        leaderboards = read_leaderboards(connection)
    finally:
        connection.close()

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "internal world_cup.db snapshot",
        "teams": teams,
        "matches": matches,
        "standings": standings,
        "best_thirds": best_thirds,
        "qualification_slots": slots,
        "group_statuses": group_statuses,
        "live_standings": live_standings,
        "live_best_thirds": live_best_thirds,
        "live_group_statuses": live_group_statuses,
        "daily_group_projections": daily_group_projections,
        "group_routes": group_routes,
        "third_place_slot_overrides": third_place_slot_overrides,
        "third_place_mapping": third_place_mapping,
        "leaderboards": leaderboards,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()
    snapshot = build_snapshot(args.source.resolve())
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} with {len(snapshot['matches'])} matches.")


if __name__ == "__main__":
    main()
