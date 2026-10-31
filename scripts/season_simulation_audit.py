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


def protected_digest(path: Path) -> str:
    h = hashlib.sha256()
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as con:
        for name, schema in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            if name in SEASON_TABLES or name.startswith("sqlite_"):
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
