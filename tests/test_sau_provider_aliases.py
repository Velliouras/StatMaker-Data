import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canonical_team_identity as identity
import domestic_odds_expansion as expansion


class SaudiProviderAliasTest(unittest.TestCase):
    def index(self, code="SAU"):
        # Canonical spelling is present in the existing 2026 Pro League fixture
        # cache (provider league 307) and the 2026-2027 roster.
        pipeline = SimpleNamespace(
            ROSTER_PATH="roster",
            load_json=lambda path, default: (
                {"leagues": [{"leagueCode": code, "appSeason": "2026-2027",
                             "teams": ["Al Diriyah", "Al-Ettifaq"]}]}
                if path == "roster" else {}),
            stats_fetch=SimpleNamespace(cache_path_for=lambda league: "cache"))
        odds = SimpleNamespace(ALIASES_PATH="aliases")
        return identity.domestic_indexes(odds, pipeline, [
            {"leagueCode": code, "targetAppSeason": "2026-2027"}])[code]

    def test_diriyah_provider_spelling_resolves_in_strict_canonical_validator(self):
        canonical, policy, candidates = self.index().resolve("Diriyah Club")
        self.assertEqual("Al Diriyah", canonical)
        self.assertEqual("exact-verified", policy)
        self.assertEqual(["Al Diriyah"], candidates)

    def test_ingestion_and_validation_share_the_same_explicit_bridge(self):
        self.assertIn("Diriyah Club", expansion.VERIFIED_TEAM_ALIASES["SAU"]["Al Diriyah"])

    def test_unknown_and_other_league_names_still_fail_closed(self):
        self.assertIsNone(self.index().resolve("Unknown Saudi Club")[0])
        self.assertIsNone(self.index("OTHER").resolve("Diriyah Club")[0])

    def test_an_equal_priority_conflict_still_fails_closed(self):
        index = self.index()
        index.add_alias("Diriyah Club", "Different Club", priority=identity.VERIFIED_ALIAS_PRIORITY)
        self.assertIsNone(index.resolve("Diriyah Club")[0])


if __name__ == "__main__":
    unittest.main()

