import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canonical_team_identity as identity
import domestic_odds_expansion as expansion
import update_domestic_odds_api_io as odds


class CurrentProviderAliasesTest(unittest.TestCase):
    CASES = {
        "DNK": {"Broendby IF": "Brondby"},
        "EGY": {"Asyut Petroleum": "Asyut Petrol"},
        "HUN": {
            "Puskas Akademia FC Felcsut": "Puskas Academy",
            "Ferencvarosi Budapest": "Ferencvarosi TC",
        },
        "UKR": {"FC Kryvbas Kriviy Rih": "Kryvbas KR"},
        "SC1": {
            "Queens Park FC": "Queen's Park",
            "Inverness Caledonian Thistle FC": "Inverness CT",
        },
        "ISR": {"Hapoel Jerusalem FC": "Hapoel Katamon"},
    }

    def index(self, code):
        pipeline = SimpleNamespace(
            ROSTER_PATH="roster",
            load_json=lambda path, default: (
                {"leagues": [{"leagueCode": code, "appSeason": "2026-2027",
                             "teams": list(self.CASES.get(code, {}).values())}]}
                if path == "roster" else {}),
            stats_fetch=SimpleNamespace(cache_path_for=lambda league: "cache"))
        return identity.domestic_indexes(
            SimpleNamespace(ALIASES_PATH="aliases"), pipeline,
            [{"leagueCode": code, "targetAppSeason": "2026-2027"}])[code]

    def test_verified_provider_names_resolve_in_the_strict_validator(self):
        for code, names in self.CASES.items():
            for provider, expected in names.items():
                with self.subTest(code=code, provider=provider):
                    canonical, policy, candidates = self.index(code).resolve(provider)
                    self.assertEqual(expected, canonical)
                    self.assertEqual("exact-verified", policy)
                    self.assertEqual([expected], list(candidates))

    def test_ingestion_uses_the_same_explicit_bridges(self):
        for code, names in self.CASES.items():
            for provider, expected in names.items():
                with self.subTest(code=code, provider=provider):
                    self.assertIn(provider, expansion.VERIFIED_TEAM_ALIASES[code][expected])
                    mapped, canonical = expansion.canonical_team_info(odds, provider, code, {}, {})
                    self.assertEqual((expected, expected), (mapped, canonical))

    def test_mapping_is_league_scoped_and_does_not_confuse_qpr(self):
        index = self.index("SC1")
        self.assertIsNone(index.resolve("Queens Park Rangers")[0])
        self.assertIsNone(index.resolve("Queens Park Rangers FC")[0])
        for names in self.CASES.values():
            for provider in names:
                self.assertIsNone(self.index("OTHER").resolve(provider)[0])

    def test_unknown_and_equal_priority_ambiguous_names_still_fail(self):
        index = self.index("SC1")
        self.assertIsNone(index.resolve("Unknown Scotland Club")[0])
        index.add_alias("Queens Park FC", "Different Club", priority=identity.VERIFIED_ALIAS_PRIORITY)
        self.assertIsNone(index.resolve("Queens Park FC")[0])


if __name__ == "__main__":
    unittest.main()
