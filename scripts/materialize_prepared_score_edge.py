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

SCORE = re.compile(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$")
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
        # Exact score is deliberately excluded from the legacy prepared betting
        # selection table. The raw, already-published repository odds feed is
        # the canonical market source for Score Edge. Do not treat absent
        # prepared_selections as an absence of exact bookmaker odds.
        root = Path(__file__).resolve().parents[1]
        feeds = (
            ("domestic", root / "odds/odds_api_io/domestic_odds.json"),
            ("champions_league", root / "odds/odds_api_io/champions_league_odds.json"),
            ("europa_league", root / "odds/odds_api_io/europa_league_odds.json"),
        )
        odds_by_id: dict[tuple[str, str], dict[tuple[int, int], float]] = defaultdict(dict)
        odds_by_team: dict[tuple[str, str, str, str], dict[tuple[int, int], float]] = defaultdict(dict)
        for competition, path in feeds:
            if not path.is_file():
                continue
            feed = json.loads(path.read_text(encoding="utf-8"))
            all_matches = list(feed.get("matches") or [])
            for league in feed.get("leagues") or []:
                all_matches.extend(league.get("matches") or [])
            for fixture in all_matches:
                if not isinstance(fixture, dict):
                    continue
                fixture_id = str(fixture.get("id") or "").strip()
                fixture_date = str(fixture.get("date") or "")[:10]
                home = str(fixture.get("homeTeam") or "").strip().casefold()
                away = str(fixture.get("awayTeam") or "").strip().casefold()
                for market in fixture.get("markets") or []:
                    if str(market.get("market") or "").upper() != "CORRECT_SCORE":
                        continue
                    if market.get("exactBookmakerOdds") is False:
                        continue
                    found = SCORE.fullmatch(str(market.get("selection") or ""))
                    if not found:
                        continue
                    try:
                        odd = float(market.get("odds") or 0.0)
                    except (ValueError, TypeError):
                        continue
                    if not math.isfinite(odd) or odd <= 1.0:
                        continue
                    score = (int(found.group(1)), int(found.group(2)))
                    if fixture_id:
                        quotes = odds_by_id[(competition, fixture_id)]
                        quotes[score] = max(odd, quotes.get(score, 0.0))
                    if fixture_date and home and away:
                        quotes = odds_by_team[(competition, fixture_date, home, away)]
                        quotes[score] = max(odd, quotes.get(score, 0.0))

        match_headers = {}
        for row in con.execute(
            "SELECT competition_id,snapshot_version,match_key,payload "
            "FROM prepared_matches"
        ):
            try:
                header = json.loads(row["payload"])
                match_headers[(
                    row["competition_id"], row["snapshot_version"], row["match_key"]
                )] = header
            except (ValueError, TypeError):
                continue

        # Most current Simulation fixtures originate from the bookmaker-
        # independent fixture index, not from prepared_matches betting rows.
        # Join by scheduled teams/date when Odds-API.io IDs differ from
        # API-Football fixture IDs.
        for row in con.execute("""
            SELECT competition_id,snapshot_version,match_key,
                   id,local_date,home_team,away_team
            FROM prepared_fixture_matches
        """):
            key = (row["competition_id"], row["snapshot_version"], row["match_key"])
            match_headers.setdefault(key, {
                "id": row["id"],
                "date": row["local_date"],
                "homeTeam": row["home_team"],
                "awayTeam": row["away_team"],
            })

        total_rows = 0
        covered_matches = 0
        seen_simulations = 0
        matched_odds_count = 0
        examples = []
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
            seen_simulations += 1
            header = match_headers.get(key) or {}
            fixture_id = str(header.get("id") or "").strip()
            fixture_date = str(header.get("date") or "")[:10]
            home = str(header.get("homeTeam") or "").strip().casefold()
            away = str(header.get("awayTeam") or "").strip().casefold()
            bookmaker_quotes = (
                odds_by_id.get((row["competition_id"], fixture_id))
                or odds_by_team.get((row["competition_id"], fixture_date, home, away))
                or odds_by_id.get((row["competition_id"], row["match_key"]))
            )
            if not bookmaker_quotes:
                if len(examples) < 5:
                    examples.append((key, fixture_id, fixture_date, home, away))
                continue
            matched_odds_count += 1
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
            print(
                "SCORE_EDGE_DIAGNOSTIC",
                "simulations=", seen_simulations,
                "matched_odds=", matched_odds_count,
                "headers=", len(match_headers),
                "odds_ids=", len(odds_by_id),
                "odds_teams=", len(odds_by_team),
                "examples=", examples,
                flush=True,
            )
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
