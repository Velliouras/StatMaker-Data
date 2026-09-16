#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path


RESULT_KEYS = {
    "RESULT_1X2",
    "RESULT_DOUBLE_CHANCE",
    "HT_RESULT_1X2",
    "HT_RESULT_DOUBLE_CHANCE",
}


def norm(value: object) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.replace("_", " ").replace("-", " ").split())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    args = parser.parse_args()

    path = Path(args.database)
    if not path.is_file():
        raise SystemExit(f"Prepared DB not found: {path}")

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed: {quick}")

        generation = con.execute(
            """
            SELECT generation_id, rules_fingerprint, candidate_count
            FROM prepared_pattern_generation
            WHERE state='ready'
            ORDER BY built_at_ms DESC
            LIMIT 1
            """
        ).fetchone()
        if not generation:
            raise SystemExit("No ready prepared recommendation generation")
        generation_id, rules_fingerprint, candidate_count = generation
        expected_rules = "pattern-policy-v2-final-read-model-v7-result-context-v1"
        if str(rules_fingerprint) != expected_rules:
            raise SystemExit(
                f"Unexpected rules fingerprint: {rules_fingerprint!r} expected={expected_rules!r}"
            )

        structural_bad = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM prepared_pattern_candidates c
                JOIN prepared_selections s
                  ON s.competition_id=c.competition_id
                 AND s.snapshot_version=c.snapshot_version
                 AND s.selection_key=c.selection_key
                WHERE c.generation_id=?
                  AND c.recommendation_eligible=1
                  AND c.competition_id='domestic'
                  AND s.identity_sub_market_key IN (
                    'RESULT_1X2','RESULT_DOUBLE_CHANCE',
                    'HT_RESULT_1X2','HT_RESULT_DOUBLE_CHANCE'
                  )
                  AND (
                    COALESCE(s.opponent_adjusted_required,0)<>1
                    OR s.opponent_model_probability IS NULL
                  )
                """,
                (generation_id,),
            ).fetchone()[0]
        )
        if structural_bad:
            raise SystemExit(
                f"Domestic result recommendations missing matchup model context: {structural_bad}"
            )

        retired_bad = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM prepared_pattern_candidates c
                JOIN prepared_selections s
                  ON s.competition_id=c.competition_id
                 AND s.snapshot_version=c.snapshot_version
                 AND s.selection_key=c.selection_key
                WHERE c.generation_id=?
                  AND c.recommendation_eligible=1
                  AND (
                    UPPER(COALESCE(c.market_family,'')) LIKE '%ASIAN%'
                    OR UPPER(COALESCE(c.market_family,'')) LIKE '%HANDICAP%'
                    OR UPPER(COALESCE(s.identity_family,'')) LIKE '%ASIAN%'
                    OR UPPER(COALESCE(s.identity_family,'')) LIKE '%HANDICAP%'
                  )
                """,
                (generation_id,),
            ).fetchone()[0]
        )
        if retired_bad:
            raise SystemExit(f"Retired Asian/Handicap candidates leaked into generation: {retired_bad}")

        rows = con.execute(
            """
            SELECT c.market_family, COALESCE(c.value_tier,'')
            FROM prepared_pattern_candidates c
            WHERE c.generation_id=? AND c.recommendation_eligible=1
            """,
            (generation_id,),
        ).fetchall()
        market_counts = Counter(str(row[0]) for row in rows)
        value_counts = Counter(str(row[1] or "NONE") for row in rows)
        print(
            "RESULT_CONTEXT_DISTRIBUTION",
            "candidates="+str(candidate_count),
            "eligible="+str(len(rows)),
            "markets="+",".join(f"{k}:{v}" for k,v in sorted(market_counts.items())),
            "value="+",".join(f"{k}:{v}" for k,v in sorted(value_counts.items())),
        )

        match_rows = con.execute(
            """
            SELECT competition_id, snapshot_version, match_key, payload
            FROM prepared_matches
            WHERE competition_id='domestic'
            """
        ).fetchall()
        target = None
        for competition_id, snapshot_version, match_key, payload_raw in match_rows:
            try:
                payload = json.loads(payload_raw)
            except Exception:
                continue
            home = norm(payload.get("homeTeam") or payload.get("canonicalHomeTeam"))
            away = norm(payload.get("awayTeam") or payload.get("canonicalAwayTeam"))
            if "barcelona" in home and "racing santander" in away:
                target = (competition_id, snapshot_version, match_key, payload)
                break

        if target is None:
            print("RESULT_CONTEXT_REGRESSION_TARGET_NOT_IN_CURRENT_WINDOW Barcelona-Racing Santander")
            return

        competition_id, snapshot_version, match_key, payload = target
        print(
            "RESULT_CONTEXT_REGRESSION_TARGET",
            f"match_key={match_key}",
            f"home={payload.get('homeTeam') or payload.get('canonicalHomeTeam')}",
            f"away={payload.get('awayTeam') or payload.get('canonicalAwayTeam')}",
        )

        target_rows = con.execute(
            """
            SELECT
              s.selection_market,
              s.selection_name,
              s.selection_odd,
              s.identity_sub_market_key,
              s.identity_selection_side,
              s.bm_hits,
              s.bm_sample,
              s.bm_hit_rate,
              s.bm_market_probability,
              s.bm_posterior_probability,
              s.opponent_adjusted_required,
              s.opponent_model_probability,
              s.opponent_without_favorite_probability,
              s.value_signal_tier,
              s.value_signal_edge,
              s.value_signal_expected_value,
              s.value_signal_ranking_score,
              c.selection_score,
              c.recommendation_eligible
            FROM prepared_selections s
            LEFT JOIN prepared_pattern_candidates c
              ON c.generation_id=?
             AND c.competition_id=s.competition_id
             AND c.snapshot_version=s.snapshot_version
             AND c.selection_key=s.selection_key
            WHERE s.competition_id=?
              AND s.snapshot_version=?
              AND s.match_key=?
              AND s.identity_sub_market_key IN (
                'RESULT_1X2','RESULT_DOUBLE_CHANCE',
                'HT_RESULT_1X2','HT_RESULT_DOUBLE_CHANCE'
              )
            ORDER BY s.identity_sub_market_key, s.selection_odd
            """,
            (generation_id, competition_id, snapshot_version, match_key),
        ).fetchall()

        if not target_rows:
            raise SystemExit("Barcelona-Racing has no persisted result-market rows")

        for row in target_rows:
            (
                market, name, odd, sub_key, side,
                hits, sample, hit_rate, market_p, posterior_p,
                required, model_p, without_fav_p,
                value_tier, edge, ev, ranking, selection_score, eligible,
            ) = row
            print(
                "RESULT_CONTEXT_TARGET_ROW",
                f"market={market}",
                f"selection={name}",
                f"odd={odd}",
                f"sub={sub_key}",
                f"side={side}",
                f"history={hits}/{sample}",
                f"hit_rate={hit_rate}",
                f"market_p={market_p}",
                f"posterior_p={posterior_p}",
                f"required={required}",
                f"model_p={model_p}",
                f"without_favorite_p={without_fav_p}",
                f"value_tier={value_tier}",
                f"edge={edge}",
                f"ev={ev}",
                f"value_rank={ranking}",
                f"candidate_score={selection_score}",
                f"eligible={eligible}",
            )

        dc_rows = [
            row for row in target_rows
            if str(row[3]) == "RESULT_DOUBLE_CHANCE"
            and (
                "x2" in norm(row[1])
                or "x2" in norm(row[4])
                or ("away" in norm(row[4]) and "draw" in norm(row[4]))
            )
        ]
        for row in dc_rows:
            odd = float(row[2] or 0.0)
            model_p = row[11]
            without_fav_p = row[12]
            if odd >= 8.0:
                if model_p is None:
                    raise SystemExit("Barcelona-Racing X2 is missing matchup model probability")
                if without_fav_p is None:
                    raise SystemExit("Barcelona-Racing X2 is missing favorite counterfactual")
                if float(model_p) >= 0.50:
                    raise SystemExit(
                        f"Barcelona-Racing X2 still has implausible >=50% matchup probability: {model_p}"
                    )

        print("RESULT_CONTEXT_GENERATION_AUDIT_OK")
    finally:
        con.close()


if __name__ == "__main__":
    main()
