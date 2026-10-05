#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "reports" / "elo_betting_ab_backtest.json"
REPORT_MD = ROOT / "reports" / "elo_betting_ab_backtest.md"
MANIFEST_REL = "data/statmaker/app_ready/update_manifest.json"
ATHENS = ZoneInfo("Europe/Athens")
SAFETY_MS = 60_000

sys.path.insert(0, str((ROOT / "scripts").resolve()))
import materialize_canonical_recommendation_ledger as ledger  # noqa: E402

ledger.MODE_LABEL = "uat-hybrid"

CURRENT_TEAM_ELO = ROOT / "scripts" / "materialize_prepared_team_elo.py"
CURRENT_SIMULATION = ROOT / "scripts" / "materialize_prepared_simulations.py"

COMPLETED = {"FT", "AET", "PEN"}


def run(cmd: list[str], cwd: Path = ROOT, capture: bool = False) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def git_bytes(commit: str, path: str) -> bytes | None:
    try:
        return subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
    except subprocess.CalledProcessError:
        return None


def git_file(commit: str, path: str, dest: Path) -> bool:
    raw = git_bytes(commit, path)
    if raw is None:
        return False
    dest.write_bytes(raw)
    return True


def manifest_commits_for_local_day(day: dt.date) -> list[str]:
    local_start = dt.datetime.combine(day, dt.time.min, tzinfo=ATHENS)
    local_end = local_start + dt.timedelta(days=1)
    start_utc = local_start.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = local_end.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    output = run(
        [
            "git", "log", "--reverse", "--format=%H",
            f"--since={start_utc}", f"--until={end_utc}",
            "--", MANIFEST_REL,
        ],
        capture=True,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def commit_epoch(commit: str) -> int:
    raw = run(["git", "show", "-s", "--format=%ct", commit], capture=True).strip()
    return int(raw or 0)


def choose_snapshot_commit(day: dt.date, commits: list[str], preferred_hour: int) -> str | None:
    if not commits:
        return None
    preferred = dt.datetime.combine(
        day, dt.time(hour=preferred_hour), tzinfo=ATHENS
    ).timestamp()
    eligible = [c for c in commits if commit_epoch(c) <= preferred]
    return eligible[-1] if eligible else commits[0]


def bundle_path_at_commit(commit: str) -> str | None:
    raw = git_bytes(commit, MANIFEST_REL)
    if not raw:
        return None
    try:
        manifest = json.loads(raw.decode("utf-8"))
        return next(
            str(item.get("path") or "")
            for item in manifest.get("artifacts", [])
            if item.get("id") == "app_ready_betting_bundle"
        )
    except Exception:
        return None


def extract_prepared_db(commit: str, bundle_path: str, dest: Path) -> bool:
    bundle = dest.with_suffix(".zip")
    if not git_file(commit, bundle_path, bundle):
        return False
    try:
        with zipfile.ZipFile(bundle) as z:
            with z.open("databases/statmaker_prepared_betting.db") as source:
                dest.write_bytes(source.read())
        return True
    except Exception:
        return False
    finally:
        bundle.unlink(missing_ok=True)


def has_target_candidates(db_path: Path, day: str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE local_date=?",
            (day,),
        ).fetchone()
        return bool(row and int(row[0]) > 0)
    except sqlite3.Error:
        return False
    finally:
        con.close()


def prune_db_to_day(db_path: Path, day: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM prepared_pattern_candidates WHERE local_date<>?", (day,))
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(prepared_selections)")
        }
        if "local_date" in columns:
            con.execute("DELETE FROM prepared_selections WHERE local_date<>?", (day,))
        con.execute(
            """
            DELETE FROM prepared_matches
            WHERE NOT EXISTS (
                SELECT 1
                FROM prepared_selections s
                WHERE s.competition_id=prepared_matches.competition_id
                  AND s.snapshot_version=prepared_matches.snapshot_version
                  AND s.match_key=prepared_matches.match_key
            )
            """
        )
        con.commit()
    finally:
        con.close()


def materialize_historical_simulations(
    commit: str,
    db_path: Path,
    work_root: Path,
    runs: int,
) -> tuple[bool, str]:
    worktree = work_root / "repo"
    try:
        run(["git", "worktree", "add", "--detach", str(worktree), commit])
        scripts = worktree / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CURRENT_SIMULATION, scripts / CURRENT_SIMULATION.name)
        shutil.copy2(CURRENT_TEAM_ELO, scripts / CURRENT_TEAM_ELO.name)

        run([sys.executable, str(scripts / CURRENT_TEAM_ELO.name), str(db_path)], cwd=worktree)
        run(
            [
                sys.executable,
                str(scripts / CURRENT_SIMULATION.name),
                str(db_path),
                "--runs",
                str(runs),
            ],
            cwd=worktree,
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        if worktree.exists():
            try:
                run(["git", "worktree", "remove", "--force", str(worktree)])
            except Exception:
                shutil.rmtree(worktree, ignore_errors=True)
        try:
            run(["git", "worktree", "prune"])
        except Exception:
            pass


def latest_generation(con: sqlite3.Connection) -> tuple[str, int] | None:
    row = con.execute(
        """
        SELECT generation_id,built_at_ms
        FROM prepared_pattern_generation
        WHERE state='ready'
        ORDER BY built_at_ms DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return str(row[0]), int(row[1] or 0)


def apply_elo_overlay(con: sqlite3.Connection) -> int:
    tables = {
        row[0] for row in con.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table'
              AND name IN ('prepared_simulations','prepared_match_explorer_simulations')
            """
        )
    }
    if tables != {"prepared_simulations", "prepared_match_explorer_simulations"}:
        return 0
    before = con.total_changes
    con.execute(
        """
        UPDATE prepared_simulations
        SET simulation_probability=(
                SELECT e.simulation_probability
                FROM prepared_match_explorer_simulations e
                WHERE e.competition_id=prepared_simulations.competition_id
                  AND e.snapshot_version=prepared_simulations.snapshot_version
                  AND e.selection_key=prepared_simulations.selection_key
            ),
            simulation_push_probability=COALESCE((
                SELECT e.simulation_push_probability
                FROM prepared_match_explorer_simulations e
                WHERE e.competition_id=prepared_simulations.competition_id
                  AND e.snapshot_version=prepared_simulations.snapshot_version
                  AND e.selection_key=prepared_simulations.selection_key
            ), simulation_push_probability),
            simulation_runs=COALESCE((
                SELECT e.simulation_runs
                FROM prepared_match_explorer_simulations e
                WHERE e.competition_id=prepared_simulations.competition_id
                  AND e.snapshot_version=prepared_simulations.snapshot_version
                  AND e.selection_key=prepared_simulations.selection_key
            ), simulation_runs),
            simulation_model=COALESCE((
                SELECT e.simulation_model
                FROM prepared_match_explorer_simulations e
                WHERE e.competition_id=prepared_simulations.competition_id
                  AND e.snapshot_version=prepared_simulations.snapshot_version
                  AND e.selection_key=prepared_simulations.selection_key
            ), simulation_model),
            expected_home_count=COALESCE((
                SELECT e.expected_home_goals
                FROM prepared_match_explorer_simulations e
                WHERE e.competition_id=prepared_simulations.competition_id
                  AND e.snapshot_version=prepared_simulations.snapshot_version
                  AND e.selection_key=prepared_simulations.selection_key
            ), expected_home_count),
            expected_away_count=COALESCE((
                SELECT e.expected_away_goals
                FROM prepared_match_explorer_simulations e
                WHERE e.competition_id=prepared_simulations.competition_id
                  AND e.snapshot_version=prepared_simulations.snapshot_version
                  AND e.selection_key=prepared_simulations.selection_key
            ), expected_away_count)
        WHERE EXISTS (
            SELECT 1
            FROM prepared_match_explorer_simulations e
            WHERE e.competition_id=prepared_simulations.competition_id
              AND e.snapshot_version=prepared_simulations.snapshot_version
              AND e.selection_key=prepared_simulations.selection_key
        )
        """
    )
    return con.total_changes - before


def candidate_records(
    con: sqlite3.Connection,
    generation_id: str,
    built_at_ms: int,
    day: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    candidates = ledger.final_candidates_hybrid(con, generation_id, day)
    for candidate in candidates:
        if str(candidate.get("local_date") or "")[:10] != day:
            continue
        comp = str(candidate.get("competition_id") or "")
        snap = str(candidate.get("snapshot_version") or "")
        selection_key = str(candidate.get("selection_key") or "")
        selection = ledger.first(
            con,
            """
            SELECT * FROM prepared_selections
            WHERE competition_id=? AND snapshot_version=? AND selection_key=?
            LIMIT 1
            """,
            (comp, snap, selection_key),
        )
        if not selection:
            continue
        prepared_match_key = str(selection.get("match_key") or "")
        match_row = ledger.first(
            con,
            """
            SELECT payload FROM prepared_matches
            WHERE competition_id=? AND snapshot_version=? AND match_key=?
            LIMIT 1
            """,
            (comp, snap, prepared_match_key),
        )
        if not match_row:
            continue
        try:
            match = json.loads(str(match_row.get("payload") or "{}"))
        except Exception:
            continue

        candidate_key = str(candidate.get("match_key") or "").strip()
        if not candidate_key or candidate_key != ledger.runtime_match_key(match):
            continue
        kickoff = ledger.kickoff_ms(match)
        if kickoff is not None and built_at_ms >= kickoff - SAFETY_MS:
            continue
        if kickoff is None:
            generation_day = dt.datetime.fromtimestamp(
                built_at_ms / 1000, tz=dt.timezone.utc
            ).astimezone(ATHENS).date().isoformat()
            if day <= generation_day:
                continue

        out.append(
            {
                "date": day,
                "competitionId": comp,
                "snapshotVersion": snap,
                "selectionKey": selection_key,
                "matchKey": candidate_key,
                "leagueCode": str(candidate.get("league_code") or match.get("leagueCode") or "").upper(),
                "season": str(match.get("season") or ""),
                "homeTeam": str(match.get("homeTeam") or ""),
                "awayTeam": str(match.get("awayTeam") or ""),
                "apiFixtureId": ledger.live._fixture_id_from_match_payload(match),
                "subMarketKey": str(selection.get("identity_sub_market_key") or ""),
                "selectionSide": str(selection.get("identity_selection_side") or ""),
                "teamSide": str(selection.get("identity_team_side") or ""),
                "selectionToken": str(selection.get("identity_selection_token") or ""),
                "selectionName": str(selection.get("selection_name") or ""),
                "team": selection.get("selection_team"),
                "line": ledger.nullable(selection.get("identity_line")),
                "odd": ledger.nullable(selection.get("selection_odd")),
                "modelProbability": ledger.nullable(candidate.get("_hybrid_probability")),
                "baseProbability": ledger.nullable(candidate.get("_hybrid_base_probability")),
                "simulationProbability": ledger.nullable(candidate.get("_hybrid_simulation_probability")),
                "simulationModel": candidate.get("simulation_model"),
            }
        )
    return out


def evaluate_db(db_path: Path, day: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        generation = latest_generation(con)
        if generation is None:
            return [], [], 0
        gid, built = generation
        legacy = candidate_records(con, gid, built, day)

        con.execute("BEGIN")
        overlay_rows = apply_elo_overlay(con)
        elo = candidate_records(con, gid, built, day)
        con.rollback()
        return legacy, elo, overlay_rows
    finally:
        con.close()


def normalize_team(value: Any) -> str:
    return ledger.live.normalize_team(value)


class SettlementIndex:
    def __init__(self) -> None:
        self.by_id: dict[int, dict[str, Any]] = {}
        self.by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.enriched_index = self._load_enriched_index()
        self.enriched_cache: dict[str, list[dict[str, Any]]] = {}
        self._load_live()

    def _load_live(self) -> None:
        path = ROOT / "data" / "statmaker" / "live_settlements.json"
        try:
            root = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            root = {}
        for row in root.get("fixtures", []) if isinstance(root, dict) else []:
            if not isinstance(row, dict):
                continue
            fid = row.get("fixtureId")
            if isinstance(fid, int):
                self.by_id[fid] = self._from_live(row)
            date = str(row.get("dateUtc") or "")[:10]
            home = normalize_team(row.get("homeTeam"))
            away = normalize_team(row.get("awayTeam"))
            if date and home and away:
                self.by_identity[(date, home, away)] = self._from_live(row)

    def _load_enriched_index(self) -> list[dict[str, Any]]:
        try:
            root = json.loads(
                (ROOT / "data/statmaker/domestic_enriched/index.json").read_text(encoding="utf-8")
            )
        except Exception:
            return []
        return [x for x in root.get("leagues", []) if isinstance(x, dict)]

    @staticmethod
    def _from_live(row: dict[str, Any]) -> dict[str, Any]:
        stats = row.get("normalizedStats") if isinstance(row.get("normalizedStats"), dict) else {}
        return {
            "status": str(row.get("status") or ""),
            "homeGoals": row.get("homeGoals"),
            "awayGoals": row.get("awayGoals"),
            "homeHalfGoals": row.get("homeHalfGoals"),
            "awayHalfGoals": row.get("awayHalfGoals"),
            "HS": stats.get("HS"), "AS": stats.get("AS"),
            "HST": stats.get("HST"), "AST": stats.get("AST"),
            "HC": stats.get("HC"), "AC": stats.get("AC"),
            "HY": stats.get("HY"), "AY": stats.get("AY"),
            "HR": 0 if stats.get("HR") is None and stats.get("HY") is not None else stats.get("HR"),
            "AR": 0 if stats.get("AR") is None and stats.get("AY") is not None else stats.get("AR"),
        }

    def _enriched_rows(self, league_code: str, season: str) -> list[dict[str, Any]]:
        candidates = [
            x for x in self.enriched_index
            if str(x.get("league_code") or "").upper() == league_code.upper()
            and (
                str(x.get("app_season") or "") == season
                or str(x.get("season") or "") == season
                or str(x.get("target_app_season") or "") == season
            )
        ]
        if not candidates:
            candidates = [
                x for x in self.enriched_index
                if str(x.get("league_code") or "").upper() == league_code.upper()
                and str(x.get("stats_role") or "") == "current_target"
            ]
        if not candidates:
            return []
        entry = max(candidates, key=lambda x: int(x.get("completed_fixtures") or 0))
        path = str(entry.get("cache_path") or "")
        if not path:
            return []
        cached = self.enriched_cache.get(path)
        if cached is not None:
            return cached
        try:
            root = json.loads((ROOT / path).read_text(encoding="utf-8"))
            rows = [x for x in root.get("matches", []) if isinstance(x, dict)]
        except Exception:
            rows = []
        self.enriched_cache[path] = rows
        return rows

    @staticmethod
    def _from_enriched(row: dict[str, Any]) -> dict[str, Any]:
        stats = row.get("normalized_stats") if isinstance(row.get("normalized_stats"), dict) else {}
        return {
            "status": str(row.get("status") or ""),
            "homeGoals": row.get("home_goals", row.get("fthg")),
            "awayGoals": row.get("away_goals", row.get("ftag")),
            "homeHalfGoals": row.get("hthg"),
            "awayHalfGoals": row.get("htag"),
            "HS": stats.get("HS"), "AS": stats.get("AS"),
            "HST": stats.get("HST"), "AST": stats.get("AST"),
            "HC": stats.get("HC"), "AC": stats.get("AC"),
            "HY": stats.get("HY"), "AY": stats.get("AY"),
            "HR": 0 if stats.get("HR") is None and stats.get("HY") is not None else stats.get("HR"),
            "AR": 0 if stats.get("AR") is None and stats.get("AY") is not None else stats.get("AR"),
        }

    def lookup(self, pick: dict[str, Any]) -> dict[str, Any] | None:
        fid = pick.get("apiFixtureId")
        if isinstance(fid, int) and fid in self.by_id:
            return self.by_id[fid]
        ident = (
            str(pick.get("date") or "")[:10],
            normalize_team(pick.get("homeTeam")),
            normalize_team(pick.get("awayTeam")),
        )
        if all(ident) and ident in self.by_identity:
            return self.by_identity[ident]

        for row in self._enriched_rows(
            str(pick.get("leagueCode") or ""),
            str(pick.get("season") or ""),
        ):
            if isinstance(fid, int) and int(row.get("fixture_id") or 0) == fid:
                return self._from_enriched(row)
            date = str(row.get("date_utc") or "")[:10]
            if (
                date == ident[0]
                and normalize_team(row.get("home_team")) == ident[1]
                and normalize_team(row.get("away_team")) == ident[2]
            ):
                return self._from_enriched(row)
        return None


def number(value: Any) -> float | None:
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def int_value(value: Any) -> int | None:
    v = number(value)
    return int(v) if v is not None else None


def result_side(home: int, away: int) -> str:
    return "HOME" if home > away else "AWAY" if away > home else "DRAW"


def line_outcome(value: int | None, line: float | None, side: str) -> str | None:
    if value is None or line is None:
        return None
    if side == "OVER":
        if value > line:
            return "WON"
        if abs(value - line) < 1e-9:
            return "VOID"
        return "LOST"
    if side == "UNDER":
        if value < line:
            return "WON"
        if abs(value - line) < 1e-9:
            return "VOID"
        return "LOST"
    return None


def team_metric(pick: dict[str, Any], home: int | None, away: int | None) -> int | None:
    side = str(pick.get("teamSide") or "")
    if side == "HOME":
        return home
    if side == "AWAY":
        return away
    team = normalize_team(pick.get("team"))
    if team and team == normalize_team(pick.get("homeTeam")):
        return home
    if team and team == normalize_team(pick.get("awayTeam")):
        return away
    return None


def grade_pick(pick: dict[str, Any], stats: dict[str, Any] | None) -> tuple[str | None, float | None]:
    if not stats or str(stats.get("status") or "").upper() not in COMPLETED:
        return None, None
    hg = int_value(stats.get("homeGoals"))
    ag = int_value(stats.get("awayGoals"))
    if hg is None or ag is None:
        return None, None
    hh = int_value(stats.get("homeHalfGoals"))
    ah = int_value(stats.get("awayHalfGoals"))
    side = str(pick.get("selectionSide") or "")
    sub = str(pick.get("subMarketKey") or "")
    line = number(pick.get("line"))
    odd = number(pick.get("odd"))
    if odd is None:
        return None, None

    full = result_side(hg, ag)
    half = result_side(hh, ah) if hh is not None and ah is not None else None
    total = hg + ag
    half_total = hh + ah if hh is not None and ah is not None else None
    home_second = hg - hh if hh is not None else None
    away_second = ag - ah if ah is not None else None
    second_total = (
        home_second + away_second
        if home_second is not None and away_second is not None
        else None
    )

    outcome: str | None = None
    if sub == "RESULT_1X2":
        outcome = "WON" if side == full else "LOST"
    elif sub == "RESULT_DOUBLE_CHANCE":
        hit = (
            (side == "HOME_OR_DRAW" and full != "AWAY")
            or (side == "AWAY_OR_DRAW" and full != "HOME")
            or (side == "HOME_OR_AWAY" and full != "DRAW")
        )
        outcome = "WON" if hit else "LOST"
    elif sub == "RESULT_DNB":
        outcome = "VOID" if full == "DRAW" else ("WON" if side == full else "LOST")
    elif sub == "HT_RESULT_1X2" and half is not None:
        outcome = "WON" if side == half else "LOST"
    elif sub == "HT_RESULT_DOUBLE_CHANCE" and half is not None:
        hit = (
            (side == "HOME_OR_DRAW" and half != "AWAY")
            or (side == "AWAY_OR_DRAW" and half != "HOME")
            or (side == "HOME_OR_AWAY" and half != "DRAW")
        )
        outcome = "WON" if hit else "LOST"
    elif sub == "BTTS":
        actual = hg > 0 and ag > 0
        if side == "YES":
            outcome = "WON" if actual else "LOST"
        elif side == "NO":
            outcome = "WON" if not actual else "LOST"
    elif sub == "FULL_TIME_MATCH_TOTAL":
        outcome = line_outcome(total, line, side)
    elif sub in {"HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL", "TEAM_TOTAL"}:
        outcome = line_outcome(team_metric(pick, hg, ag), line, side)
    elif sub == "FIRST_HALF_MATCH_TOTAL":
        outcome = line_outcome(half_total, line, side)
    elif sub in {"HOME_TEAM_1H_TOTAL", "AWAY_TEAM_1H_TOTAL", "TEAM_1H_TOTAL"}:
        outcome = line_outcome(team_metric(pick, hh, ah), line, side)
    elif sub == "SECOND_HALF_MATCH_TOTAL":
        outcome = line_outcome(second_total, line, side)
    elif sub in {"HOME_TEAM_2H_TOTAL", "AWAY_TEAM_2H_TOTAL", "TEAM_2H_TOTAL"}:
        outcome = line_outcome(team_metric(pick, home_second, away_second), line, side)
    elif sub == "MATCH_CORNERS_TOTAL":
        hc, ac = int_value(stats.get("HC")), int_value(stats.get("AC"))
        outcome = line_outcome(hc + ac if hc is not None and ac is not None else None, line, side)
    elif sub in {"HOME_TEAM_CORNERS", "AWAY_TEAM_CORNERS", "TEAM_CORNERS"}:
        outcome = line_outcome(
            team_metric(pick, int_value(stats.get("HC")), int_value(stats.get("AC"))),
            line, side,
        )
    elif sub == "CORNER_RESULT_1X2":
        hc, ac = int_value(stats.get("HC")), int_value(stats.get("AC"))
        if hc is not None and ac is not None:
            outcome = "WON" if side == result_side(hc, ac) else "LOST"
    elif sub == "MATCH_YELLOW_CARDS_TOTAL":
        hy, ay = int_value(stats.get("HY")), int_value(stats.get("AY"))
        outcome = line_outcome(hy + ay if hy is not None and ay is not None else None, line, side)
    elif sub in {"HOME_TEAM_YELLOW_CARDS", "AWAY_TEAM_YELLOW_CARDS", "TEAM_YELLOW_CARDS"}:
        outcome = line_outcome(
            team_metric(pick, int_value(stats.get("HY")), int_value(stats.get("AY"))),
            line, side,
        )
    elif sub == "MATCH_CARDS_TOTAL":
        hy, ay = int_value(stats.get("HY")), int_value(stats.get("AY"))
        hr, ar = int_value(stats.get("HR")), int_value(stats.get("AR"))
        value = hy + ay + hr + ar if None not in {hy, ay, hr, ar} else None
        outcome = line_outcome(value, line, side)
    elif sub in {"HOME_TEAM_CARDS", "AWAY_TEAM_CARDS", "TEAM_CARDS"}:
        hy, ay = int_value(stats.get("HY")), int_value(stats.get("AY"))
        hr, ar = int_value(stats.get("HR")), int_value(stats.get("AR"))
        home_cards = hy + hr if hy is not None and hr is not None else None
        away_cards = ay + ar if ay is not None and ar is not None else None
        outcome = line_outcome(team_metric(pick, home_cards, away_cards), line, side)
    elif sub == "MATCH_RED_CARD":
        hr, ar = int_value(stats.get("HR")), int_value(stats.get("AR"))
        if hr is not None and ar is not None:
            actual = hr + ar > 0
            if side == "YES":
                outcome = "WON" if actual else "LOST"
            elif side == "NO":
                outcome = "WON" if not actual else "LOST"
    elif sub == "MATCH_SHOTS_TOTAL":
        hs, av = int_value(stats.get("HS")), int_value(stats.get("AS"))
        outcome = line_outcome(hs + av if hs is not None and av is not None else None, line, side)
    elif sub in {"HOME_TEAM_SHOTS", "AWAY_TEAM_SHOTS", "TEAM_SHOTS"}:
        outcome = line_outcome(
            team_metric(pick, int_value(stats.get("HS")), int_value(stats.get("AS"))),
            line, side,
        )
    elif sub == "SHOTS_RESULT_1X2":
        hs, av = int_value(stats.get("HS")), int_value(stats.get("AS"))
        if hs is not None and av is not None:
            outcome = "WON" if side == result_side(hs, av) else "LOST"
    elif sub == "MATCH_SOT_TOTAL":
        hs, av = int_value(stats.get("HST")), int_value(stats.get("AST"))
        outcome = line_outcome(hs + av if hs is not None and av is not None else None, line, side)
    elif sub in {"HOME_TEAM_SOT", "AWAY_TEAM_SOT", "TEAM_SOT"}:
        outcome = line_outcome(
            team_metric(pick, int_value(stats.get("HST")), int_value(stats.get("AST"))),
            line, side,
        )
    elif sub == "SOT_RESULT_1X2":
        hs, av = int_value(stats.get("HST")), int_value(stats.get("AST"))
        if hs is not None and av is not None:
            outcome = "WON" if side == result_side(hs, av) else "LOST"

    if outcome is None:
        return None, None
    gross = odd if outcome == "WON" else 1.0 if outcome == "VOID" else 0.0
    return outcome, gross


def settle_picks(picks: list[dict[str, Any]], index: SettlementIndex) -> list[dict[str, Any]]:
    out = []
    for pick in picks:
        row = dict(pick)
        outcome, gross = grade_pick(row, index.lookup(row))
        row["outcome"] = outcome
        row["grossReturn"] = gross
        out.append(row)
    return out


def summarize(picks: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [p for p in picks if p.get("outcome") in {"WON", "LOST", "VOID"}]
    won = sum(p.get("outcome") == "WON" for p in graded)
    lost = sum(p.get("outcome") == "LOST" for p in graded)
    void = sum(p.get("outcome") == "VOID" for p in graded)
    decisive = won + lost
    returns = sum(float(p.get("grossReturn") or 0.0) for p in graded)
    odds = [float(p["odd"]) for p in graded if number(p.get("odd")) is not None]
    probs = [
        float(p["modelProbability"])
        for p in graded
        if number(p.get("modelProbability")) is not None
    ]
    return {
        "picks": len(picks),
        "graded": len(graded),
        "unresolved": len(picks) - len(graded),
        "won": won,
        "lost": lost,
        "void": void,
        "hitRate": (won / decisive) if decisive else None,
        "roi": ((returns - len(graded)) / len(graded)) if graded else None,
        "averageOdds": (sum(odds) / len(odds)) if odds else None,
        "averageModelProbability": (sum(probs) / len(probs)) if probs else None,
        "settlementCoverage": (len(graded) / len(picks)) if picks else None,
    }


def pct(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def dec(value: Any, places: int = 2) -> str:
    return "-" if value is None else f"{float(value):.{places}f}"


def compare(legacy: list[dict[str, Any]], elo: list[dict[str, Any]]) -> dict[str, Any]:
    lmap = {(p["date"], p["matchKey"]): p for p in legacy}
    emap = {(p["date"], p["matchKey"]): p for p in elo}
    keys = sorted(set(lmap) | set(emap))
    changes = []
    counts = {
        "samePick": 0,
        "changedSelection": 0,
        "legacyOnly": 0,
        "eloOnly": 0,
        "legacyLostRemovedOrChanged": 0,
        "legacyWonRemovedOrChanged": 0,
        "eloAddedOrChangedWon": 0,
        "eloAddedOrChangedLost": 0,
    }
    for key in keys:
        l = lmap.get(key)
        e = emap.get(key)
        if l and e and l.get("selectionKey") == e.get("selectionKey"):
            counts["samePick"] += 1
            continue
        if l and e:
            counts["changedSelection"] += 1
        elif l:
            counts["legacyOnly"] += 1
        else:
            counts["eloOnly"] += 1

        if l and l.get("outcome") == "LOST":
            counts["legacyLostRemovedOrChanged"] += 1
        if l and l.get("outcome") == "WON":
            counts["legacyWonRemovedOrChanged"] += 1
        if e and e.get("outcome") == "WON":
            counts["eloAddedOrChangedWon"] += 1
        if e and e.get("outcome") == "LOST":
            counts["eloAddedOrChangedLost"] += 1

        changes.append(
            {
                "date": key[0],
                "matchKey": key[1],
                "legacy": l,
                "elo": e,
            }
        )
    return {"counts": counts, "changes": changes}


def render_markdown(report: dict[str, Any]) -> str:
    legacy = report["summary"]["legacy"]
    elo = report["summary"]["elo"]
    comp = report["comparison"]["counts"]
    lines = [
        "# Elo Betting A/B Backtest",
        "",
        f"- Window: **{report['window']['from']} → {report['window']['to']}**",
        f"- Historical snapshot policy: **latest App-Ready manifest at/before {report['snapshotLocalHour']:02d}:00 Europe/Athens per day**",
        f"- Monte Carlo runs per historical snapshot: **{report['simulationRuns']}**",
        f"- Days processed: **{report['processedDays']}** / {report['requestedDays']}",
        f"- Days skipped: **{len(report['skippedDays'])}**",
        "",
        "## Result",
        "",
        "| Metric | Legacy simulation | Elo-backed simulation |",
        "|---|---:|---:|",
        f"| Strong picks | {legacy['picks']} | {elo['picks']} |",
        f"| Graded | {legacy['graded']} | {elo['graded']} |",
        f"| Won | {legacy['won']} | {elo['won']} |",
        f"| Lost | {legacy['lost']} | {elo['lost']} |",
        f"| Void | {legacy['void']} | {elo['void']} |",
        f"| Hit rate | {pct(legacy['hitRate'])} | {pct(elo['hitRate'])} |",
        f"| Average odds | {dec(legacy['averageOdds'])} | {dec(elo['averageOdds'])} |",
        f"| ROI (1 unit/pick) | {pct(legacy['roi'])} | {pct(elo['roi'])} |",
        f"| Avg final probability | {pct(legacy['averageModelProbability'])} | {pct(elo['averageModelProbability'])} |",
        f"| Settlement coverage | {pct(legacy['settlementCoverage'])} | {pct(elo['settlementCoverage'])} |",
        "",
        "## Decision changes",
        "",
        f"- Same pick: **{comp['samePick']}**",
        f"- Different selection on same match: **{comp['changedSelection']}**",
        f"- Legacy-only pick: **{comp['legacyOnly']}**",
        f"- Elo-only pick: **{comp['eloOnly']}**",
        f"- Legacy losing picks removed/changed by Elo: **{comp['legacyLostRemovedOrChanged']}**",
        f"- Legacy winning picks removed/changed by Elo: **{comp['legacyWonRemovedOrChanged']}**",
        f"- Elo added/changed picks that won: **{comp['eloAddedOrChangedWon']}**",
        f"- Elo added/changed picks that lost: **{comp['eloAddedOrChangedLost']}**",
        "",
        "## Notes",
        "",
        "- Both variants use the **same current Hybrid Strong thresholds and 70/30 base/simulation weight**.",
        "- The only A/B difference is the simulation input: legacy prepared_simulations versus the Elo-backed Match Explorer overlay where available.",
        "- Each historical simulation is rebuilt from the repository state at that historical manifest commit, then filtered by the existing pre-kickoff safety cutoff.",
        "- ROI assumes one unit staked on every graded pick. VOID returns one unit.",
        "- Markets that cannot be deterministically graded from retained score/stat artifacts remain unresolved and are excluded from hit-rate/ROI.",
        "",
    ]
    changed = report["comparison"]["changes"][:30]
    if changed:
        lines.extend([
            "## First changed decisions",
            "",
            "| Date | Match | Legacy | Elo |",
            "|---|---|---|---|",
        ])
        for row in changed:
            def fmt(p: dict[str, Any] | None) -> str:
                if not p:
                    return "—"
                name = str(p.get("selectionName") or p.get("subMarketKey") or "")
                odd = dec(p.get("odd"))
                outcome = str(p.get("outcome") or "?")
                return f"{name} @{odd} ({outcome})"
            match = row["matchKey"].replace("|", " / ")
            lines.append(
                f"| {row['date']} | {match} | {fmt(row.get('legacy'))} | {fmt(row.get('elo'))} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Historical A/B test: legacy simulation vs Elo overlay")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--runs", type=int, default=10_000)
    parser.add_argument("--snapshot-local-hour", type=int, default=11)
    args = parser.parse_args()

    days = max(1, min(30, args.days))
    runs_count = max(1_000, min(10_000, args.runs))
    snapshot_hour = max(0, min(23, args.snapshot_local_hour))

    today = dt.datetime.now(dt.timezone.utc).astimezone(ATHENS).date()
    requested_dates = [today - dt.timedelta(days=i) for i in range(1, days + 1)]
    settlement_index = SettlementIndex()

    all_legacy: list[dict[str, Any]] = []
    all_elo: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    processed: list[str] = []
    overlay_total = 0

    for target in reversed(requested_dates):
        day = target.isoformat()
        print(f"BACKTEST_DAY start={day}", flush=True)
        commits = manifest_commits_for_local_day(target)
        commit = choose_snapshot_commit(target, commits, snapshot_hour)
        if not commit:
            skipped.append({"date": day, "reason": "NO_MANIFEST_COMMIT"})
            print(f"BACKTEST_DAY skip={day} reason=NO_MANIFEST_COMMIT", flush=True)
            continue
        bundle_path = bundle_path_at_commit(commit)
        if not bundle_path:
            skipped.append({"date": day, "reason": "NO_BETTING_BUNDLE"})
            print(f"BACKTEST_DAY skip={day} reason=NO_BETTING_BUNDLE", flush=True)
            continue

        with tempfile.TemporaryDirectory(prefix="statmaker-elo-ab-") as td:
            root = Path(td)
            db_path = root / "prepared.db"
            if not extract_prepared_db(commit, bundle_path, db_path):
                skipped.append({"date": day, "reason": "BUNDLE_EXTRACT_FAILED"})
                continue
            if not has_target_candidates(db_path, day):
                skipped.append({"date": day, "reason": "NO_TARGET_CANDIDATES"})
                continue

            work_root = root / "work"
            work_root.mkdir(parents=True, exist_ok=True)
            prune_db_to_day(db_path, day)
            ok, error = materialize_historical_simulations(commit, db_path, work_root, runs_count)
            if not ok:
                skipped.append({"date": day, "reason": "SIMULATION_FAILED", "detail": error[:300]})
                print(f"BACKTEST_DAY skip={day} reason=SIMULATION_FAILED detail={error[:160]}", flush=True)
                continue

            legacy_picks, elo_picks, overlay_rows = evaluate_db(db_path, day)
            overlay_total += overlay_rows
            legacy_picks = settle_picks(legacy_picks, settlement_index)
            elo_picks = settle_picks(elo_picks, settlement_index)
            all_legacy.extend(legacy_picks)
            all_elo.extend(elo_picks)
            processed.append(day)
            print(
                f"BACKTEST_DAY done={day} legacy={len(legacy_picks)} elo={len(elo_picks)} overlay={overlay_rows}",
                flush=True,
            )

    legacy_summary = summarize(all_legacy)
    elo_summary = summarize(all_elo)
    comparison = compare(all_legacy, all_elo)

    report = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "requestedDays": days,
        "processedDays": len(processed),
        "processedDates": processed,
        "skippedDays": skipped,
        "window": {
            "from": min((d.isoformat() for d in requested_dates), default=""),
            "to": max((d.isoformat() for d in requested_dates), default=""),
        },
        "snapshotLocalHour": snapshot_hour,
        "simulationRuns": runs_count,
        "eloOverlayRowsApplied": overlay_total,
        "contract": {
            "baseWeight": 0.70,
            "simulationWeight": 0.30,
            "legacySimulation": "monte-carlo-v1",
            "eloSimulation": "match-monte-carlo-v2-elo",
            "eloModel": "team-elo-v1",
            "strongLogic": "current Hybrid Strong contract; thresholds unchanged",
        },
        "summary": {
            "legacy": legacy_summary,
            "elo": elo_summary,
        },
        "comparison": comparison,
    }

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")

    print("ELO_AB_BACKTEST_OK")
    print(json.dumps({"legacy": legacy_summary, "elo": elo_summary, "changes": comparison["counts"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
