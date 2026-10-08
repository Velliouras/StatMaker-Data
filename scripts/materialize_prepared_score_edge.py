#!/usr/bin/env python3
"""Materialize exact-score probabilities and bookmaker value, without resimulation.

Uses the SAME prepared_match_simulations 10,000-draw distribution shipped to
Match Simulation. Android then only applies user-selected filters to these rows.
Neither betting scoring nor recommendation decisions are modified.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

SCORE = re.compile(r"^\\s*(\\d+)\\s*[-:]\\s*(\\d+)\\s*$")
MIN_PROBABILITY = 0.05


def materialize(db_path: Path) -> tuple[int, int]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        con.executescript("""
            DROP TABLE IF EXISTS prepared_score_edge_values;
            CREATE TABLE prepared_score_edge_values (
                competition_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL,
                match_key TEXT NOT NULL,
                score_home INTEGER NOT NULL,
                score_away INTEGER NOT NULL,
                model_probability REAL NOT NULL,
                selection_odd REAL,
                market_probability REAL,
                market_edge REAL,
                expected_value REAL,
                market_probability_mode TEXT NOT NULL,
                simulation_runs INTEGER NOT NULL,
                expected_home_goals REAL NOT NULL,
                expected_away_goals REAL NOT NULL,
                history_league_sample INTEGER NOT NULL,
                PRIMARY KEY (
                    competition_id, snapshot_version, match_key, score_home, score_away
                )
            );
            CREATE INDEX idx_prepared_score_edge_match
                ON prepared_score_edge_values(
                    competition_id, snapshot_version, match_key
                );
        """)
        quotes: dict[tuple[str, str, str], dict[tuple[int, int], float]] = defaultdict(dict)
        for row in con.execute("""
            SELECT competition_id, snapshot_version, match_key,
                   selection_name, selection_odd
            FROM prepared_selections
            WHERE identity_sub_market_key='RESULT_CORRECT_SCORE'
              AND selection_odd > 1.0
        """):
            odds = float(row["selection_odd"])
            if not math.isfinite(odds) or odds <= 1.0:
                continue
            found = SCORE.fullmatch(str(row["selection_name"] or ""))
            if not found:
                continue
            score = (int(found.group(1)), int(found.group(2)))
            key = (row["competition_id"], row["snapshot_version"], row["match_key"])
            quotes[key][score] = max(odds, quotes[key].get(score, 0.0))

        total_rows = 0
        covered_matches = 0
        generated = int(time.time() * 1000)
        for row in con.execute("""
            SELECT s.competition_id, s.snapshot_version, s.match_key,
                   s.simulation_runs, s.expected_home_goals,
                   s.expected_away_goals, s.history_league_sample,
                   s.score_distribution_json
            FROM prepared_match_simulations s
            JOIN prepared_snapshot_meta m
              ON m.competition_id=s.competition_id
             AND m.snapshot_version=s.snapshot_version
            WHERE m.state='ready' AND s.simulation_runs >= 10000
        """):
            key = (row["competition_id"], row["snapshot_version"], row["match_key"])
            bookmaker_quotes = quotes.get(key)
            if not bookmaker_quotes:
                continue
            try:
                scores = json.loads(row["score_distribution_json"] or "[]")
            except (ValueError, TypeError):
                continue
            probabilities = {
                (int(x["homeGoals"]), int(x["awayGoals"])): float(x["probability"])
                for x in scores
                if isinstance(x, dict)
            }
            if not probabilities:
                continue
            # Same contract as ExactScoreProbabilityEngine.assessValue:
            # one repository bookmaker, >=12 scores and overround >1.
            overround = sum(1.0 / odd for odd in bookmaker_quotes.values())
            no_vig = len(bookmaker_quotes) >= 12 and overround > 1.0
            mode = "FULL_BOOK_NO_VIG" if no_vig else "RAW_IMPLIED"
            batch = []
            for (home, away), probability in probabilities.items():
                if probability < MIN_PROBABILITY or not math.isfinite(probability):
                    continue
                odd = bookmaker_quotes.get((home, away))
                market = ((1.0 / odd) / overround if no_vig else 1.0 / odd) if odd else None
                batch.append((
                    *key, home, away, probability, odd, market,
                    (probability - market) if market is not None else None,
                    (probability * odd - 1.0) if odd else None,
                    mode, int(row["simulation_runs"]),
                    float(row["expected_home_goals"]),
                    float(row["expected_away_goals"]),
                    int(row["history_league_sample"] or 0),
                ))
            if not batch:
                continue
            con.executemany("""
                INSERT INTO prepared_score_edge_values(
                    competition_id,snapshot_version,match_key,score_home,score_away,
                    model_probability,selection_odd,market_probability,market_edge,
                    expected_value,market_probability_mode,simulation_runs,
                    expected_home_goals,expected_away_goals,history_league_sample
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, batch)
            total_rows += len(batch)
            covered_matches += 1
        if covered_matches == 0:
            raise RuntimeError(
                "No exact-score markets overlap with 10k prepared simulations: "
                "do not publish an empty Score Edge contract."
            )
        con.commit()
        print(
            f"PREPARED_SCORE_EDGE_OK matches={covered_matches} "
            f"rows={total_rows} runs=10000", flush=True
        )
        return covered_matches, total_rows
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    args = parser.parse_args()
    materialize(args.db)
