"""Identity must survive a league leaving the rotating refresh registry."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canonical_team_identity as identity
import domestic_betting_identity as betting


# Exact names in the cached NB I 2026-2027 roster and the six failed fixtures.
CASES = {
    "Budapest Honved FC": "Budapest Honved",
    "Kisvarda FC": "Kisvarda FC",
    "Zalaegerszeg TE": "Zalaegerszegi TE",
    "MTK Budapest": "MTK Budapest",
    "WKW ETO FC Gyor": "Gyori ETO FC",
    "Vasas FC": "Vasas",
    "Puskas Akademia FC Felcsut": "Puskas Academy",
    "Debreceni VSC": "Debreceni VSC",
    "Ferencvarosi Budapest": "Ferencvarosi TC",
    "Paksi FC": "Paks",
    "Ujpest FC Budapest": "Ujpest",
    "Nyiregyhaza Spartacus FC": "Nyiregyhaza",
}
ALIASES = {
    "Zalaegerszegi TE": ["Zalaegerszeg TE"],
    "Gyori ETO FC": ["WKW ETO FC Gyor"],
    "Paks": ["Paksi FC"],
    "Nyiregyhaza": ["Nyiregyhaza Spartacus FC"],
}


def context(registry):
    payloads = {
        "aliases": {"aliases": {"HUN": ALIASES}},
        "registry": {"leagues": registry},
        "roster": {"leagues": [
            {"leagueCode": "HUN", "appSeason": "2026-2027", "teams": list(CASES.values())},
            {"leagueCode": "OTHER", "appSeason": "2026", "teams": ["Other United"]},
        ]},
    }
    pipeline = SimpleNamespace(
        REGISTRY_PATH="registry", ROSTER_PATH="roster",
        load_json=lambda path, default: payloads.get(path, default),
        stats_fetch=SimpleNamespace(cache_path_for=lambda league: "empty-cache"),
    )
    odds = SimpleNamespace(ALIASES_PATH="aliases", record_unmatched_team=lambda *args: None)
    return pipeline, odds


def indexes(registry=()):
    pipeline, odds = context(list(registry))
    return identity.domestic_indexes(odds, pipeline, registry)


def feed():
    rows = list(CASES.items())
    return {"leagues": [{"leagueCode": "HUN", "matches": [
        {"id": str(72302660 + n), "providerHomeTeam": home[0], "providerAwayTeam": away[0],
         "homeTeam": home[1], "awayTeam": away[1],
         "canonicalHomeTeam": home[1], "canonicalAwayTeam": away[1],
         "teamMappingStatus": "matched", "usableForStats": True, "scheduleOnly": False,
         "markets": [{"market": "RESULT_1X2", "selection": "Home", "odds": 1.91,
                      "bookmaker": "Unibet", "providerMarketName": "Match Result"}]}
        for n, (home, away) in enumerate(zip(rows[::2], rows[1::2]))
    ]}]}


class CanonicalIdentityUniverseTest(unittest.TestCase):
    def test_all_twelve_clubs_resolve_before_and_after_registry_removal(self):
        for registry in ([], [{"leagueCode": "HUN", "targetAppSeason": "2026-2027"}]):
            mapping = indexes(registry)
            for provider, expected in CASES.items():
                with self.subTest(registry=bool(registry), provider=provider):
                    self.assertEqual(expected, mapping["HUN"].resolve(provider)[0])
                    self.assertIsNone(mapping["OTHER"].resolve(provider)[0])

    def test_ingestion_uses_the_same_complete_roster_without_registry_entry(self):
        pipeline, odds = context([])
        identity.install_domestic(odds, pipeline)
        for provider, expected in CASES.items():
            self.assertEqual((expected, expected), odds.canonical_team_info(provider, "HUN", {}, {}))

    def test_valid_prices_survive_reconciliation_and_read_only_publication_gate(self):
        payload = feed()
        before = copy.deepcopy(payload)
        self.assertEqual(0, betting.reconcile_feed(payload, indexes())["suppressedQuotes"])
        report = betting.validate_feed_identity(payload, indexes())
        self.assertEqual({"bettingMatches": 6, "scheduleOnlySkipped": 0, "extraApiCalls": 0}, report)
        self.assertEqual(before, payload)

    def test_matched_flag_does_not_override_unknown_or_wrong_provider_identity(self):
        for provider in ("Unknown Hungary Club", "Paksi FC", "Other United"):
            with self.subTest(provider=provider):
                payload = feed()
                payload["leagues"][0]["matches"][0]["providerHomeTeam"] = provider
                before = copy.deepcopy(payload)
                with self.assertRaisesRegex(RuntimeError, "DOMESTIC_BETTING_IDENTITY_INVALID"):
                    betting.validate_feed_identity(payload, indexes())
                self.assertEqual(before, payload)

    def test_ambiguous_configured_alias_still_blocks_publication(self):
        mapping = indexes()
        mapping["HUN"].add_alias("Zalaegerszeg TE", "Different Club")
        self.assertIsNone(mapping["HUN"].resolve("Zalaegerszeg TE")[0])
        with self.assertRaisesRegex(RuntimeError, "ambiguous-exact"):
            betting.validate_feed_identity(feed(), mapping)

    def test_unmapped_calendar_row_without_prices_remains_allowed_and_unchanged(self):
        payload = feed()
        row = payload["leagues"][0]["matches"][0]
        row.update(providerHomeTeam="Unknown Club", teamMappingStatus="partial",
                   usableForStats=False, scheduleOnly=True, markets=[])
        before = copy.deepcopy(payload)
        self.assertEqual(1, betting.validate_feed_identity(payload, indexes())["scheduleOnlySkipped"])
        self.assertEqual(before, payload)

    def test_active_league_does_not_gain_teams_from_an_unselected_roster_season(self):
        pipeline, odds = context([])
        mapping = identity.domestic_indexes(odds, pipeline, [
            {"leagueCode": "OTHER", "targetAppSeason": "2027"}])
        self.assertIsNone(mapping["OTHER"].resolve("Other United")[0])


if __name__ == "__main__":
    unittest.main()
