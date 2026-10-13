"""Offline producer regression: reuse matchup work without dropping any real raw offers.

No provider calls, artifacts, GitHub Actions or Android writes are made.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from scripts.betting_v2 import produce_certified_app_ready as producer


class BettingV2ProducerFullUniverseCacheTests(TestCase):
    def test_matchup_reuse_preserves_every_selection_and_exact_quote(self):
        rows = []
        quotes = []
        for fixture_id, count in (("fx-a", 24), ("fx-b", 3)):
            for i in range(count):
                selection_id = f"{fixture_id}-selection-{i}"
                rows.append({
                    "competitionId": "domestic", "snapshotVersion": "snapshot",
                    "fixtureId": fixture_id, "leagueCode": "E0", "season": "2026",
                    "kickoffEpochMs": 900000, "homeTeam": f"home-{fixture_id}",
                    "awayTeam": f"away-{fixture_id}", "generationId": "generation",
                    "selectionId": selection_id, "marketModelKey": "RESULT_1X2",
                    "usableForStats": True,
                })
                quotes.append({
                    "generationId": "generation", "competitionId": "domestic",
                    "snapshotVersion": "snapshot", "fixtureId": fixture_id,
                    "selectionId": selection_id, "bookmaker": "verified-bookmaker",
                    "providerQuoteId": f"provider-{selection_id}", "observedAtMs": 1000,
                    "validUntilMs": 800000, "odd": 2.05,
                })
        source = {
            "generationId": "generation", "rawRows": rows,
            "schedule": [], "history": [], "unfilteredRawInventorySha256": "test-input",
            "rawSelectionCount": len(rows),
        }
        assessed = {
            "contract": "statmaker-betting-v2-evaluated-model-v1",
            "validation": {
                "competitionId": "domestic", "leagueCode": "E0", "season": "2026",
                "marketModelKey": "RESULT_1X2", "evidenceMode": "ELO_GOALS_FALLBACK_NO_XG",
                "outOfTimeValidated": True, "comparableMarketCalibrated": True,
                "independentModelErrorIncluded": True,
            },
            "holdoutResults": {"unitStakeRoiLowerBound": 0.05},
            "plan": {"family": "GOALS_FT", "mode": "ELO_GOALS_FALLBACK_NO_XG",
                     "contextColumns": []},
            "model": {"parameters": {"name": "frozen"}},
            "documents": {}, "bins": [], "adverseBins": [],
        }
        by_fixture = {
            "fx-a": {"fixture_id": "api-a"},
            "fx-b": {"fixture_id": "api-b"},
        }
        calls = Counter()
        quote_lookups = []
        evaluated_order = []
        published = []

        def resolve(row, schedule):
            calls["resolve_fixture"] += 1
            return by_fixture[row["fixtureId"]]

        def case(history, fixture, context, offer, parameters, plan, at):
            calls["case_for"] += 1
            evaluated_order.append(("matchup", fixture["fixture_id"]))
            self.assertEqual(offer, {})  # cached template is market-independent
            return {"features": [1.0], "offer": offer}

        def exact_quote(row, candidates, at):
            quote_lookups.append((row["selectionId"], len(candidates)))
            evaluated_order.append(("price", by_fixture[row["fixtureId"]]["fixture_id"]))
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["selectionId"], row["selectionId"])
            return candidates[0]

        def read_json(path):
            if Path(path).name == "quotes.json":
                return {"quotes": quotes}
            if Path(path).name == "evaluated.json":
                return assessed
            raise AssertionError(f"Unexpected source: {path}")

        args = SimpleNamespace(
            source=Path("source.json"), quotes=Path("quotes.json"),
            contexts=Path("contexts.json"), evaluated_models=[Path("evaluated.json")],
            output=Path("release.json"),
        )
        with (
            patch.object(producer, "input_source", return_value=source),
            patch.object(producer, "read_json", side_effect=read_json),
            patch.object(producer, "contexts_index", return_value={
                "api-a": {}, "api-b": {},
            }),
            patch.object(producer, "now_ms", return_value=5000),
            patch.object(producer, "resolve_fixture", side_effect=resolve),
            patch.object(producer, "case_for", side_effect=case),
            patch.object(producer, "quote_for", side_effect=exact_quote),
            patch.object(producer, "distribution", return_value=[[0.25, 0.25], [0.25, 0.25]]),
            patch.object(producer, "scenario_groups", side_effect=lambda offers, cells, threshold: {
                row["selectionId"]: row["selectionId"] for row in offers
            }),
            patch.object(producer, "forecast", side_effect=lambda row, *args, **kwargs: {
                "selectionId": row["selectionId"], "researchOnly": False
            }),
            patch.object(producer, "save_new", side_effect=lambda path, payload: published.append(payload)),
        ):
            # scenario_groups uses the frozen plan's threshold; the actual
            # numeric value is irrelevant to this cache/inventory contract.
            assessed["plan"]["correlationThreshold"] = 0.9
            producer.produce(args)

        self.assertEqual(calls["resolve_fixture"], 2)
        self.assertEqual(calls["case_for"], 2)
        self.assertEqual(len(quote_lookups), len(rows))
        self.assertTrue(all(count == 1 for _, count in quote_lookups))
        for fixture in ("api-a", "api-b"):
            self.assertLess(evaluated_order.index(("matchup", fixture)),
                            evaluated_order.index(("price", fixture)))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["rawInventoryRowCount"], len(rows))
        self.assertEqual(len(published[0]["forecasts"]), len(rows))
        self.assertEqual(len(published[0]["quotes"]), len(rows))
        self.assertEqual({row["selectionId"] for row in published[0]["forecasts"]},
                         {row["selectionId"] for row in rows})
        self.assertEqual(published[0]["rejected"], {})
