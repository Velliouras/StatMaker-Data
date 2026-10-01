import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_live_settlements as live
import reconcile_canonical_settlement_gaps as gaps


def req(generation: str, match_key: str, fixture_id: int):
    return live.SettlementRequirement(
        generation_id=generation,
        competition_id="domestic",
        match_key=match_key,
        local_date="2026-09-30",
        league_code="BRA2",
        api_fixture_id=fixture_id,
        home_names=("Home",),
        away_names=("Away",),
        required_kind="shots",
        sub_market_key="TEAM_SHOTS",
    )


class HybridSettlementCoverageTest(unittest.TestCase):
    def test_live_requirements_union_prod_and_hybrid_ledgers(self):
        prod = req("prod-gen", "2026-09-30|Prod Home|Prod Away", 101)
        hybrid = req("hybrid-gen", "2026-09-30|Hybrid Home|Hybrid Away", 202)

        def ledger_rows(path, fallback_generation=""):
            if path == live.CANONICAL_LEDGER_PATH:
                return [prod]
            if path == live.HYBRID_UAT_LEDGER_PATH:
                return [hybrid]
            return []

        with (
            patch.object(live, "betting_bundle_paths", return_value=[]),
            patch.object(live, "requirements_from_canonical_ledger", side_effect=ledger_rows),
        ):
            rows = live.canonical_requirements()

        self.assertEqual({"prod-gen", "hybrid-gen"}, {row.generation_id for row in rows})

    def test_exact_gap_requirements_union_prod_and_hybrid_ledgers(self):
        def payload(match_key: str, fixture_id: int):
            return {
                "schemaVersion": 11,
                "invalidatedMatchKeys": [],
                "entries": [{
                    "generationId": f"gen-{fixture_id}",
                    "competitionId": "domestic",
                    "matchKey": match_key,
                    "localDate": "2026-09-30",
                    "leagueCode": "BRA2",
                    "apiFixtureId": fixture_id,
                    "homeNames": ["Home"],
                    "awayNames": ["Away"],
                    "subMarketKey": "TEAM_SHOTS",
                    "requiredKind": "shots",
                }],
            }

        def load(path, default):
            if path == gaps.LEDGER_PATH:
                return payload("2026-09-30|Prod Home|Prod Away", 101)
            if path == gaps.HYBRID_LEDGER_PATH:
                return payload("2026-09-30|Hybrid Home|Hybrid Away", 202)
            return default

        with patch.object(live, "load_json", side_effect=load):
            rows = gaps.ledger_requirements(dt.date(2026, 10, 1))

        self.assertEqual({101, 202}, {row.api_fixture_id for row in rows})


if __name__ == "__main__":
    unittest.main()
