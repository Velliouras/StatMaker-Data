"""Content checks for a season-only precision refresh; no network access."""
import hashlib
import json
import sqlite3
from pathlib import Path

SEASON_TABLES = {
    "prepared_league_simulation_meta", "prepared_league_team_simulations",
    "prepared_league_standing_zones", "prepared_uefa_simulation_meta",
    "prepared_uefa_team_simulations", "prepared_uefa_strength_evidence",
}


MATCH_TABLES = {
    "prepared_simulations", "prepared_match_simulations",
    "prepared_match_explorer_simulations", "prepared_match_metric_simulations",
    "prepared_score_edge_values",
}


def protected_digest(path: Path, allowed_tables: set[str] | None = None) -> str:
    allowed_tables = SEASON_TABLES if allowed_tables is None else allowed_tables
    h = hashlib.sha256()
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as con:
        for name, schema in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            if name in allowed_tables or name.startswith("sqlite_"):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            pk = [r[1] for r in sorted(con.execute("PRAGMA table_info(" + quoted + ")"),
                key=lambda r: r[5]) if r[5]]
            order = ",".join('"' + k.replace('"', '""') + '"' for k in pk) if pk else "rowid"
            h.update(repr((name, schema)).encode())
            for row in con.execute("SELECT * FROM " + quoted + " ORDER BY " + order):
                h.update(repr(row).encode())
                h.update(b"\n")
    return h.hexdigest()


def validate_precision(path: Path, runs: int) -> dict:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as con:
        for table in ("prepared_league_simulation_meta", "prepared_uefa_simulation_meta"):
            low, high, count = con.execute(
                f"SELECT MIN(simulation_runs),MAX(simulation_runs),COUNT(*) FROM {table}"
            ).fetchone()
            if not count or low != runs or high != runs:
                raise ValueError(f"Incomplete season precision: {table} min={low} max={high} count={count}")
        domestic = con.execute("SELECT COUNT(*) FROM prepared_league_simulation_meta").fetchone()[0]
        for code, season, size in con.execute("SELECT league_code,season,team_count FROM prepared_league_simulation_meta"):
            rows = con.execute("SELECT title_probability,position_probabilities_json FROM prepared_league_team_simulations WHERE league_code=? AND season=?", (code, season)).fetchall()
            matrices = [json.loads(r[1]) for r in rows]
            if len(rows) != size or any(len(p) != size for p in matrices):
                raise ValueError(f"Incomplete league positions: {code}")
            if abs(sum(r[0] for r in rows) - 1) > 1e-7:
                raise ValueError(f"Invalid title probability mass: {code}")
            if any(any(not 0 <= p <= 1 for p in ps) or abs(sum(ps)-1) > 1e-7 for ps in matrices):
                raise ValueError(f"Invalid team position probability mass: {code}")
            if any(abs(sum(ps[pos] for ps in matrices)-1) > 1e-7 for pos in range(size)):
                raise ValueError(f"Invalid position probability mass: {code}")
        stages = con.execute("SELECT competition_id,COUNT(*),SUM(top8_probability),SUM(playoff_probability),SUM(round16_probability),SUM(quarterfinal_probability),SUM(semifinal_probability),SUM(final_probability),SUM(winner_probability) FROM prepared_uefa_team_simulations GROUP BY competition_id").fetchall()
        if len(stages) != 3 or any(r[1] != 36 or any(abs(a-b)>1e-7 for a,b in zip(r[2:], (8,16,16,8,4,2,1))) for r in stages):
            raise ValueError("Invalid UEFA stage probability mass")
        return {"domesticLeagues": domestic, "uefaCompetitions": len(stages), "seasonRuns": runs}


def validate_match_precision(path: Path, runs: int) -> dict:
    """Check the actual summary, metric and market distributions before publication."""
    def probability_mass(values, label):
        values = list(values)
        if not values or any(not 0 <= p <= 1 for p in values) or abs(sum(values)-1) > 1e-7:
            raise ValueError(f"Invalid match probability mass: {label}")

    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as con:
        counts = {}
        for table in sorted(MATCH_TABLES):
            low, high, count = con.execute(
                f"SELECT MIN(simulation_runs),MAX(simulation_runs),COUNT(*) FROM {table}"
            ).fetchone()
            if not count or low != runs or high != runs:
                raise ValueError(f"Incomplete match precision: {table} min={low} max={high} count={count}")
            counts[table] = count
        for home, draw, away, raw in con.execute(
            "SELECT home_win_probability,draw_probability,away_win_probability,score_distribution_json "
            "FROM prepared_match_simulations"
        ):
            probability_mass((home, draw, away), "1X2")
            scores = json.loads(raw)
            probability_mass((s["probability"] for s in scores), "exact score")
            expected = [sum(s["probability"] for s in scores if (s["homeGoals"] > s["awayGoals"])),
                        sum(s["probability"] for s in scores if (s["homeGoals"] == s["awayGoals"])),
                        sum(s["probability"] for s in scores if (s["homeGoals"] < s["awayGoals"]))]
            if any(abs(a-b) > 1e-7 for a,b in zip((home,draw,away), expected)):
                raise ValueError("Match 1X2 and exact-score distribution disagree")
        for row in con.execute(
            "SELECT home_histogram_json,away_histogram_json,total_histogram_json "
            "FROM prepared_match_metric_simulations"
        ):
            for raw in row:
                probability_mass((p[1] for p in json.loads(raw)), "metric histogram")
        for table in ("prepared_simulations", "prepared_match_explorer_simulations"):
            for win, push in con.execute(f"SELECT simulation_probability,simulation_push_probability FROM {table}"):
                if not 0 <= win <= 1 or not 0 <= push <= 1 or win+push > 1+1e-7:
                    raise ValueError(f"Invalid market probability: {table}")
        uefa = con.execute("SELECT COUNT(*) FROM prepared_match_simulations WHERE simulation_model='uefa-match-monte-carlo-v1'").fetchone()[0]
        if not uefa:
            raise ValueError("UEFA match simulations are missing")
        return {"matchRuns": runs, "matches": counts["prepared_match_simulations"],
                "uefaMatches": uefa, "metricHistograms": counts["prepared_match_metric_simulations"]}
